"""Detection and bounded installation of the Windows WebView2 Runtime.

The runtime is the renderer for Cortex's window, so it has to be present before
any window can exist. When it is missing the person is asked before anything is
installed, and every failure says what to do next. The installer itself is the
bundled Microsoft Evergreen bootstrapper, run silently: whether it asks Windows
for elevation when started unelevated has not been verified here, so the prompt
only says that Windows may ask.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import subprocess
import sys


WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_BOOTSTRAPPER = "MicrosoftEdgeWebview2Setup.exe"
# The Evergreen bootstrapper's own download link, the same one
# packaging/prepare_webview2.ps1 fetches; a test keeps the two in step.
WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_PREPARE_SCRIPT = "packaging/prepare_webview2.ps1"
INSTALL_TIMEOUT_SECONDS = 10 * 60
_PROMPT_TITLE = "Cortex needs the WebView2 Runtime"
_INSTALL_PROMPT = (
    "Cortex needs the Microsoft Edge WebView2 Runtime, a one-time download from "
    "Microsoft. It is not installed on this computer.\n\n"
    "Install now? This needs an internet connection, can take a few minutes, and "
    "Windows may ask you to approve it. Cortex opens when it is done.\n\n"
    "Choose OK to install, or Cancel to close Cortex."
)

_MB_OKCANCEL = 0x00000001
_MB_ICONINFORMATION = 0x00000040
_MB_SETFOREGROUND = 0x00010000
_IDOK = 1


class WebViewRuntimeError(RuntimeError):
    """Raised when the native Chromium runtime cannot be prepared.

    The message is fixed text with something the person can do next, so the
    launcher may show it as it stands.
    """


class WebViewInstallDeclined(Exception):
    """The person chose not to install the runtime. Not a failure: Cortex just closes."""


def _manual_install_advice(packaged: bool) -> str:
    advice = (
        f"Download the Microsoft Edge WebView2 Runtime from {WEBVIEW2_DOWNLOAD_URL}, "
        "install it, then start Cortex again."
    )
    if not packaged:
        advice += (
            f" When running from source, {WEBVIEW2_PREPARE_SCRIPT} fetches the installer "
            "Cortex looks for in packaging/.runtime/webview2."
        )
    return advice


def _confirm(title: str, text: str) -> bool:
    """Ask an OK/Cancel question in a native message box; only OK is a yes.

    Fails closed: with no way to show the box there is no consent, so nothing is
    installed.
    """
    try:
        import ctypes

        answer = ctypes.windll.user32.MessageBoxW(
            None, text, title, _MB_OKCANCEL | _MB_ICONINFORMATION | _MB_SETFOREGROUND
        )
    except (AttributeError, OSError):
        return False
    return bool(answer == _IDOK)


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


def ensure_webview2_runtime(
    resource_root: Path,
    *,
    packaged: bool = True,
    confirm: Callable[[str, str], bool] | None = None,
    report: Callable[[str], None] | None = None,
) -> str | None:
    """Install the bundled Evergreen bootstrapper only when WebView2 is absent.

    The person is asked first (``confirm`` defaults to a native message box);
    declining raises ``WebViewInstallDeclined``. ``report`` receives one short,
    non-sensitive note per step -- notably the installer's exit code -- for the
    launcher's startup log. ``packaged`` only changes which advice a failure
    gives: a source checkout is pointed at the script that fetches the installer.
    """
    if sys.platform != "win32":
        return None

    installed = webview2_version()
    if installed:
        return installed

    advice = _manual_install_advice(packaged)
    bootstrapper = resource_root / "webview2" / WEBVIEW2_BOOTSTRAPPER
    if not bootstrapper.is_file():
        raise WebViewRuntimeError(
            "Microsoft Edge WebView2 Runtime is not installed and Cortex's runtime "
            f"bootstrapper is missing. {advice}"
        )

    try:
        _verify_microsoft_signature(bootstrapper)
    except WebViewRuntimeError as exc:
        raise WebViewRuntimeError(f"{exc} {advice}") from exc

    if not (confirm or _confirm)(_PROMPT_TITLE, _INSTALL_PROMPT):
        if report is not None:
            report("WebView2 install declined")
        raise WebViewInstallDeclined()

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [str(bootstrapper), "/silent", "/install"],
            check=False,
            timeout=INSTALL_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        if report is not None:
            report(f"WebView2 bootstrapper timed out after {INSTALL_TIMEOUT_SECONDS} seconds")
        raise WebViewRuntimeError(
            "The WebView2 Runtime installer did not finish within "
            f"{INSTALL_TIMEOUT_SECONDS // 60} minutes. {advice}"
        ) from exc
    except OSError as exc:
        if report is not None:
            report(f"WebView2 bootstrapper could not be started ({type(exc).__name__})")
        raise WebViewRuntimeError(
            f"Cortex could not start the WebView2 Runtime installer. {advice}"
        ) from exc

    code = result.returncode
    if report is not None:
        report(f"WebView2 bootstrapper exit code {code} (0x{code & 0xFFFFFFFF:08X})")
    installed = webview2_version()
    if not installed:
        raise WebViewRuntimeError(
            "The WebView2 Runtime installer finished without installing the runtime "
            f"(exit code {code}). If this computer is offline, connect it to the "
            f"internet and try again. {advice}"
        )
    return installed
