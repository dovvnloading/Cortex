"""Native pywebview shell for the loopback Cortex frontend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import enum
import importlib
import inspect
import os
from pathlib import Path
import sys
import time
from typing import Any


WINDOW_TITLE = "Cortex"


class DesktopWindowError(RuntimeError):
    """Raised when the owned native webview cannot be created safely."""


@dataclass(frozen=True, slots=True)
class DesktopWindowConfig:
    url: str
    storage_path: Path
    title: str = WINDOW_TITLE
    icon_path: Path | None = None
    width: int = 1440
    height: int = 960
    min_width: int = 960
    min_height: int = 640
    debug: bool = False


_WINDOW_ICON_HANDLES: list[int] = []

# The native window's ground before the page has painted, and behind it if the
# page is ever slow. These are the page's own `--bg` for each theme
# (frontend/src/styles/tokens.css, also THEME_BACKGROUNDS in lib/theme.ts); a
# test pins all three so the window and the page never disagree.
WINDOW_BACKGROUND_DARK = "#101112"
WINDOW_BACKGROUND_LIGHT = "#f3f1ec"

_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_APPS_USE_LIGHT_THEME = "AppsUseLightTheme"


def _read_apps_use_light_theme() -> int | None:
    """Read the per-user "app mode" flag from the Personalize registry key.

    ``1`` is Windows' light app mode and ``0`` its dark one. Anything that
    stops a clean read -- a non-Windows host, a missing key or value (older
    Windows), a value that is not a DWORD -- is ``None``.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, _APPS_USE_LIGHT_THEME)
    except (ImportError, OSError):
        return None
    if value_type != winreg.REG_DWORD or not isinstance(value, int):
        return None
    return value


def system_prefers_dark_apps() -> bool:
    """Whether the native window should start dark.

    Follows Windows' app mode. When that cannot be read cleanly the answer is
    dark, which is Cortex's own default theme and what the native window
    always used before it followed the system.
    """
    return _read_apps_use_light_theme() != 1


def _start_accepts_icon(webview: Any) -> bool:
    """Return whether this pywebview build accepts ``start(icon=...)``."""
    try:
        return "icon" in inspect.signature(webview.start).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def run_desktop_window(
    config: DesktopWindowConfig,
    *,
    monitor: Callable[[Any], None] | None = None,
) -> None:
    """Run the native GUI loop on the main thread until its window closes."""
    try:
        webview = importlib.import_module("webview")
    except (ImportError, OSError) as exc:
        raise DesktopWindowError(
            "Cortex's native window dependency is unavailable. Reinstall the "
            "Python dependencies or rebuild the packaged application."
        ) from exc

    config.storage_path.mkdir(parents=True, exist_ok=True)
    # Cortex's own "Download artifact" button saves through an anchor with the
    # ``download`` attribute. pywebview's Edge backend cancels every download
    # unless this is true, which made that button do nothing at all. Enabling
    # it puts a native Save As dialog in front of every download, so nothing is
    # written without the user choosing a destination. Nothing model-generated
    # can start one: the Markdown renderer drops the ``download`` attribute and
    # opens links in the system browser.
    webview.settings["ALLOW_DOWNLOADS"] = True
    # pywebview defaults this to true, which starts WebView2 with
    # ``--allow-file-access-from-files``. Cortex is served from the loopback
    # backend and reads no ``file:`` URL, so leave that access off.
    webview.settings["ALLOW_FILE_URLS"] = False
    # External links are only opened after an explicit click. The Cortex UI itself
    # always remains in this owned window and never uses a browser profile.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False

    icon_path = config.icon_path if config.icon_path and config.icon_path.is_file() else None
    start_accepts_icon = _start_accepts_icon(webview)
    # The native chrome follows Windows' app mode until the page says otherwise
    # (see ``set_title_bar_dark``), so a light-mode user is not shown a dark
    # window and title bar on every launch.
    start_dark = system_prefers_dark_apps()

    window = webview.create_window(
        config.title,
        config.url,
        width=config.width,
        height=config.height,
        min_size=(config.min_width, config.min_height),
        resizable=True,
        background_color=WINDOW_BACKGROUND_DARK if start_dark else WINDOW_BACKGROUND_LIGHT,
        text_select=True,
        zoomable=True,
    )

    def set_title_bar_dark(dark: bool) -> bool:
        """Switch this window's title bar between dark and light.

        Exposed to the page as ``window.pywebview.api.set_title_bar_dark`` so it
        can follow a theme the person pinned that differs from Windows' own.
        Cosmetic, and the only thing the page can do to the native window
        through it: anything but a real boolean is refused, and a window that
        cannot be found reports ``False`` instead of raising.
        """
        if not isinstance(dark, bool) or sys.platform != "win32":
            return False
        return _apply_windows_title_bar_theme(pid=os.getpid(), title=config.title, dark=dark)

    expose = getattr(window, "expose", None)
    if callable(expose):
        expose(set_title_bar_dark)
    startup_errors: list[Exception] = []

    def after_start() -> None:
        try:
            # WebView2 can briefly restore the last in-memory surface before it
            # processes the URL supplied to ``create_window``. Re-issue Cortex's
            # one-time loopback URL after the owned window is initialized so a
            # native launch never depends on a manual browser refresh.
            load_url = getattr(window, "load_url", None)
            if callable(load_url):
                load_url(config.url)
            # pywebview 6 exposes ``renderer`` on its module. Older compatible
            # installations used by Visual Studio do not, but still select
            # EdgeChromium when the checked WebView2 Runtime is present.
            renderer = getattr(webview, "renderer", None)
            if sys.platform == "win32" and renderer not in (None, "edgechromium"):
                raise DesktopWindowError(
                    "Cortex requires the Microsoft Edge WebView2 Runtime; the legacy "
                    "browser engine is intentionally disabled."
                )
            if sys.platform == "win32":
                _apply_windows_title_bar_theme(
                    pid=os.getpid(), title=config.title, dark=start_dark
                )
            # Keep the native window and taskbar identity aligned even when
            # pywebview accepts ``start(icon=...)``. Some WebView2 builds use
            # the Python host icon for the top-level window until WM_SETICON
            # is applied explicitly after the HWND exists.
            if icon_path:
                _apply_windows_window_icon(
                    pid=os.getpid(),
                    title=config.title,
                    icon_path=icon_path,
                )
            if monitor is not None:
                monitor(window)
        except Exception as exc:  # surfaced after the GUI loop exits
            startup_errors.append(exc)
            try:
                window.destroy()
            except Exception:
                pass

    try:
        start_options: dict[str, Any] = {
            "func": after_start,
            "gui": "edgechromium" if sys.platform == "win32" else None,
            "debug": config.debug,
            "private_mode": True,
            "storage_path": str(config.storage_path),
        }
        if icon_path and start_accepts_icon:
            start_options["icon"] = str(icon_path)
        webview.start(**start_options)
    except Exception as exc:
        raise DesktopWindowError(f"Cortex could not start its native window: {exc}") from exc

    if startup_errors:
        error = startup_errors[0]
        if isinstance(error, DesktopWindowError):
            raise error
        raise DesktopWindowError(
            f"Cortex's native window monitor failed: {error}"
        ) from error


def _find_process_window(pid: int, title: str, *, timeout: float) -> int | None:
    """Find a visible top-level Windows window by owning process and title."""
    if sys.platform != "win32" or pid <= 0:
        return None

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    deadline = time.monotonic() + max(timeout, 0.0)
    matches: list[int] = []

    @enum_callback_type
    def collect(hwnd: int, _lparam: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        title_length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
        if (
            owner.value == pid
            and user32.IsWindowVisible(hwnd)
            and title_buffer.value == title
        ):
            matches.append(hwnd)
            return False
        return True

    while True:
        matches.clear()
        user32.EnumWindows(collect, 0)
        if matches:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.1)


def _apply_windows_window_icon(*, pid: int, title: str, icon_path: Path) -> bool:
    """Apply Cortex's icon for older pywebview builds without ``start(icon=...)``."""
    if sys.platform != "win32" or not icon_path.is_file():
        return False

    try:
        hwnd = _find_process_window(pid, title, timeout=3.0)
        if hwnd is None:
            return False

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = wintypes.LPARAM

        icon_handle = user32.LoadImageW(
            None,
            str(icon_path),
            1,  # IMAGE_ICON
            0,
            0,
            0x0010 | 0x0040,  # LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
        if not icon_handle:
            return False

        handle_value = getattr(icon_handle, "value", icon_handle)
        if handle_value is None:
            return False
        user32.SendMessageW(hwnd, 0x0080, 0, handle_value)  # WM_SETICON, ICON_SMALL
        user32.SendMessageW(hwnd, 0x0080, 1, handle_value)  # WM_SETICON, ICON_BIG
        _WINDOW_ICON_HANDLES.append(int(handle_value))
        return True
    except Exception:
        # A decorative icon must never prevent Cortex from starting.
        return False


def _apply_windows_title_bar_theme(*, pid: int, title: str, dark: bool) -> bool:
    """Set Cortex's native title bar to Windows immersive dark or light mode."""
    if sys.platform != "win32":
        return False

    try:
        hwnd = _find_process_window(pid, title, timeout=3.0)
        if hwnd is None:
            return False

        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        enabled = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # Win10 2004+ / older Win10 dark-mode IDs
            result = dwmapi.DwmSetWindowAttribute(
                hwnd,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                return True
    except Exception:
        # Native chrome is cosmetic; never fail the owned UI because of it.
        return False
    return False


class WindowActivation(enum.Enum):
    """What ``activate_process_window`` found and did."""

    ACTIVATED = "activated"
    # No visible window with that title yet -- the instance may still be
    # starting, or its window may be in another session or desktop.
    NO_WINDOW = "no_window"
    # The window exists but Windows would not give it the foreground, which
    # is what an instance running elevated looks like from here.
    NOT_FOREGROUND = "not_foreground"


def activate_process_window(
    pid: int, *, title: str = WINDOW_TITLE, timeout: float = 3.0
) -> WindowActivation:
    """Restore and focus a top-level Windows window owned by ``pid``."""
    hwnd = _find_process_window(pid, title, timeout=timeout)
    if hwnd is None:
        return WindowActivation.NO_WINDOW

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    if user32.SetForegroundWindow(hwnd):
        return WindowActivation.ACTIVATED
    return WindowActivation.NOT_FOREGROUND


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5


def process_is_alive(pid: int) -> bool:
    """Report whether the process ``pid`` is still running.

    A process that exists but cannot be opened for lack of permission (an
    instance running elevated) is alive. Anything else that cannot be opened,
    and any process that has exited, counts as gone: the caller answers a
    wrong "gone" by trying the instance lock, which arbitrates safely, and a
    wrong "alive" would only make it wait.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":  # pragma: no cover - Windows is the supported launcher target
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)
