"""Process-owned launcher primitives for the Windows desktop web runtime."""

from .desktop import (
    DesktopWindowConfig,
    DesktopWindowError,
    activate_process_window,
    run_desktop_window,
)
from .frontend import FrontendBuildError, ensure_frontend
from .instance import InstanceLock, InstanceRecord
from .webview_runtime import WebViewRuntimeError, ensure_webview2_runtime

__all__ = [
    "DesktopWindowConfig",
    "DesktopWindowError",
    "FrontendBuildError",
    "InstanceLock",
    "InstanceRecord",
    "WebViewRuntimeError",
    "activate_process_window",
    "ensure_frontend",
    "ensure_webview2_runtime",
    "run_desktop_window",
]
