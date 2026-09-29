"""Process-owned launcher primitives for the Windows desktop web runtime."""

from .desktop import (
    WINDOW_TITLE,
    DesktopWindowConfig,
    DesktopWindowError,
    WindowActivation,
    activate_process_window,
    process_is_alive,
    run_desktop_window,
)
from .frontend import FrontendBuildError, ensure_frontend
from .instance import InstanceLock, InstanceRecord
from .webview_runtime import WebViewRuntimeError, ensure_webview2_runtime

__all__ = [
    "WINDOW_TITLE",
    "DesktopWindowConfig",
    "DesktopWindowError",
    "FrontendBuildError",
    "InstanceLock",
    "InstanceRecord",
    "WebViewRuntimeError",
    "WindowActivation",
    "activate_process_window",
    "ensure_frontend",
    "ensure_webview2_runtime",
    "process_is_alive",
    "run_desktop_window",
]
