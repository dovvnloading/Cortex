"""Detection and bounded installation of the Windows WebView2 Runtime."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_BOOTSTRAPPER = "MicrosoftEdgeWebview2Setup.exe"


class WebViewRuntimeError(RuntimeError):
    """Raised when the native Chromium runtime cannot be prepared."""


def webview2_version() -> str | None:
    """Return the installed Evergreen WebView2 version, if registered."""
    if sys.platform != "win32":
        return None

    import winreg

    locations = (
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
        ),
    )
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return version
    return None


def ensure_webview2_runtime(resource_root: Path) -> str | None:
    """Install the bundled Evergreen bootstrapper only when WebView2 is absent."""
    if sys.platform != "win32":
        return None

    installed = webview2_version()
    if installed:
        return installed

    bootstrapper = resource_root / "webview2" / WEBVIEW2_BOOTSTRAPPER
    if not bootstrapper.is_file():
        raise WebViewRuntimeError(
            "Microsoft Edge WebView2 Runtime is not installed and Cortex's signed "
            "runtime bootstrapper is missing. Rebuild the package with "
            "packaging/build_windows.ps1."
        )

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [str(bootstrapper), "/silent", "/install"],
            check=False,
            timeout=10 * 60,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WebViewRuntimeError(
            "Cortex could not complete the bundled WebView2 Runtime bootstrap."
        ) from exc

    installed = webview2_version()
    if not installed:
        raise WebViewRuntimeError(
            "The WebView2 Runtime bootstrapper finished without making the runtime "
            f"available (installer exit code {result.returncode})."
        )
    return installed
