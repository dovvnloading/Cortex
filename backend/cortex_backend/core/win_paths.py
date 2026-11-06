"""Absolute paths for the Windows tools Cortex runs.

Cortex starts a few of Windows' own executables: PowerShell (to check the
bundled WebView2 installer's signature before running it), ``icacls`` (to make
its data folders private) and ``taskkill`` (to end a development server's
process tree). Started by bare name they are whatever comes first on ``PATH``,
which the user, or anything they install, can change. No privilege boundary is
crossed either way, but the signature check is the gate before an installer
runs and should not depend on that, so these are resolved under the Windows
system directory instead.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import sys


# Tools that live in a folder of their own under System32.
_SUBDIRECTORIES = {
    "powershell.exe": ("WindowsPowerShell", "v1.0"),
}
_MAX_PATH = 260


def _system_directory_from_api() -> Path | None:
    """``GetSystemDirectoryW``: the real System32, whatever the environment says."""
    try:
        buffer = ctypes.create_unicode_buffer(_MAX_PATH)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, _MAX_PATH)
    except (AttributeError, OSError):
        return None
    if not 0 < length < _MAX_PATH:
        return None
    return Path(buffer.value)


def system_directory() -> Path | None:
    """Resolve the Windows system directory from the OS, never environment variables.

    A modified SystemRoot/windir must not redirect executables used for
    signature checks or private-folder permissions. If Windows does not
    identify its system directory, callers fail closed rather than using PATH.
    """
    if sys.platform != "win32":
        return None
    directory = _system_directory_from_api()
    return directory if directory is not None and directory.is_absolute() else None


def system32(name: str) -> str:
    """Return an absolute Windows system-tool path or fail without searching PATH.

    Off Windows there is no system directory; only development/test callers
    there retain the historical bare-name behavior.
    """
    if name not in ("powershell.exe", "icacls.exe", "taskkill.exe"):
        raise ValueError("Unknown Windows system tool.")
    directory = system_directory()
    if directory is None:
        if sys.platform != "win32":
            return name
        raise OSError("Windows system directory is unavailable.")
    candidate = directory.joinpath(*_SUBDIRECTORIES.get(name.lower(), ()), name)
    try:
        if candidate.is_file():
            return str(candidate)
    except OSError:
        pass
    raise OSError("Required Windows system tool is unavailable.")
