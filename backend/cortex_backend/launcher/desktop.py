"""Native pywebview shell for the loopback Cortex frontend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import importlib
import inspect
import os
from pathlib import Path
import sys
import time
from typing import Any


class DesktopWindowError(RuntimeError):
    """Raised when the owned native webview cannot be created safely."""


@dataclass(frozen=True, slots=True)
class DesktopWindowConfig:
    url: str
    storage_path: Path
    title: str = "Cortex"
    icon_path: Path | None = None
    width: int = 1440
    height: int = 960
    min_width: int = 960
    min_height: int = 640
    debug: bool = False


_WINDOW_ICON_HANDLES: list[int] = []


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
    webview.settings["ALLOW_DOWNLOADS"] = False
    # External links are only opened after an explicit click. The Cortex UI itself
    # always remains in this owned window and never uses a browser profile.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False

    icon_path = config.icon_path if config.icon_path and config.icon_path.is_file() else None
    start_accepts_icon = _start_accepts_icon(webview)

    window = webview.create_window(
        config.title,
        config.url,
        width=config.width,
        height=config.height,
        min_size=(config.min_width, config.min_height),
        resizable=True,
        background_color="#2d2d2d",
        text_select=True,
        zoomable=True,
    )
    startup_errors: list[Exception] = []

    def after_start() -> None:
        try:
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
                _apply_windows_dark_title_bar(pid=os.getpid(), title=config.title)
            if icon_path and not start_accepts_icon:
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
    while True:
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


def _apply_windows_dark_title_bar(*, pid: int, title: str) -> bool:
    """Opt Cortex's native title bar into Windows immersive dark mode."""
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
        enabled = ctypes.c_int(1)
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


def activate_process_window(pid: int, *, timeout: float = 3.0) -> bool:
    """Restore and focus a top-level Windows window owned by ``pid``."""
    hwnd = _find_process_window(pid, "Cortex", timeout=timeout)
    if hwnd is None:
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True
