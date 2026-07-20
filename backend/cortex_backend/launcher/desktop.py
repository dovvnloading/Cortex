"""Native pywebview shell for the loopback Cortex frontend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import importlib
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
    width: int = 1440
    height: int = 960
    min_width: int = 960
    min_height: int = 640
    debug: bool = False


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

    window = webview.create_window(
        config.title,
        config.url,
        width=config.width,
        height=config.height,
        min_size=(config.min_width, config.min_height),
        resizable=True,
        background_color="#10131a",
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
            if monitor is not None:
                monitor(window)
        except Exception as exc:  # surfaced after the GUI loop exits
            startup_errors.append(exc)
            try:
                window.destroy()
            except Exception:
                pass

    try:
        webview.start(
            func=after_start,
            gui="edgechromium" if sys.platform == "win32" else None,
            debug=config.debug,
            private_mode=True,
            storage_path=str(config.storage_path),
        )
    except Exception as exc:
        raise DesktopWindowError(f"Cortex could not start its native window: {exc}") from exc

    if startup_errors:
        error = startup_errors[0]
        if isinstance(error, DesktopWindowError):
            raise error
        raise DesktopWindowError(
            f"Cortex's native window monitor failed: {error}"
        ) from error


def activate_process_window(pid: int, *, timeout: float = 3.0) -> bool:
    """Restore and focus a top-level Windows window owned by ``pid``."""
    if sys.platform != "win32" or pid <= 0:
        return False

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
                and title_buffer.value == "Cortex"
            ):
                matches.append(hwnd)
                return False
            return True

        user32.EnumWindows(collect, 0)
        if matches:
            hwnd = matches[0]
            user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
