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
import os
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
    """The Windows system directory (normally ``C:\\Windows\\System32``).

    Asked of Windows first, because ``%SystemRoot%`` is an environment variable
    like ``PATH``; the variable (or ``%windir%``) is only the fallback. ``None``
    off Windows, or when neither answers with an absolute path.
    """
    if sys.platform != "win32":
        return None
    directory = _system_directory_from_api()
    if directory is not None and directory.is_absolute():
        return directory
    for variable in ("SystemRoot", "windir"):
        root = os.environ.get(variable, "").strip()
        if root and Path(root).is_absolute():
            return Path(root) / "System32"
    return None


def system32(name: str) -> str:
    """The absolute path of the Windows tool ``name`` (for example ``"icacls.exe"``).

    PowerShell is found in its ``WindowsPowerShell\\v1.0`` folder. When the
    absolute path does not exist (off Windows, or an unusual installation) the
    bare ``name`` comes back, so the tool is still found the way it always was
    rather than not at all.
    """
    directory = system_directory()
    if directory is None:
        return name
    candidate = directory.joinpath(*_SUBDIRECTORIES.get(name.lower(), ()), name)
    try:
        if candidate.is_file():
            return str(candidate)
    except OSError:
        pass
    return name
