"""Detection and bounded installation of the Windows WebView2 Runtime."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_BOOTSTRAPPER = "MicrosoftEdgeWebview2Setup.exe"


class WebViewRuntimeError(RuntimeError):
    """Raised when the native Chromium runtime cannot be prepared."""


def _verify_microsoft_signature(bootstrapper: Path) -> None:
    """Recheck the bundled installer's Authenticode signature before launch.

    The package build performs the same check, but the one-folder payload is
    mutable after extraction.  PowerShell is part of supported Windows
    installations and gives us the platform's trust-chain result without
    logging the path or certificate details.
    """

    script = (
        "$signature = Get-AuthenticodeSignature -LiteralPath "
        "$env:CORTEX_WEBVIEW_BOOTSTRAPPER; "
        "if ($signature.Status -ne 'Valid' -or "
        "$signature.SignerCertificate.Subject -notmatch "
        "'(?i)(^|, )O=Microsoft Corporation(,|$)') { exit 1 }"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    environment = os.environ.copy()
    environment["CORTEX_WEBVIEW_BOOTSTRAPPER"] = str(bootstrapper)
    # Ignore user-provided module roots: a stale or tampered module can make
    # the signature cmdlet unavailable or change what it executes.
    system_root = environment.get("SystemRoot", r"C:\Windows")
    program_files = environment.get("ProgramFiles", r"C:\Program Files")
    environment["PSModulePath"] = os.pathsep.join(
        (
            os.path.join(program_files, "WindowsPowerShell", "Modules"),
            os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "Modules"),
        )
    )
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=False,
            timeout=30,
            creationflags=creationflags,
            capture_output=True,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WebViewRuntimeError(
            "Cortex could not verify the bundled WebView2 Runtime bootstrapper."
        ) from exc
    if result.returncode != 0:
        raise WebViewRuntimeError(
            "The bundled WebView2 Runtime bootstrapper failed Microsoft signature verification."
        )


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

    _verify_microsoft_signature(bootstrapper)

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
