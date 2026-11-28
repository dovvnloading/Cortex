"""An AppContainer environment may hold any Unicode path (issue #615).

The block used to be sized with ``len()``, which counts a character outside
the Basic Multilingual Plane once although Windows stores it as two UTF-16
units, so a work copy named with an emoji could start no command at all.
"""

from __future__ import annotations

import sys

import pytest

from cortex_backend.core.win_appcontainer import _unicode_environment_block

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="UTF-16 wchar_t sizing is the Windows case"
)

EMOJI_FOLDER = "C:\\work\\project-\U0001F600"


def _environment() -> dict[str, str]:
    return {
        "PATH": "C:\\Windows\\System32",
        "PYTHONUTF8": "1",
        "SystemRoot": "C:\\Windows",
        "SystemDrive": "C:",
        "TEMP": EMOJI_FOLDER + "\\.cortex-agent-tmp",
        "TMP": EMOJI_FOLDER + "\\.cortex-agent-tmp",
        "LOCALAPPDATA": EMOJI_FOLDER + "\\localappdata",
    }


def test_a_path_with_an_emoji_survives_in_the_environment_block() -> None:
    block = _unicode_environment_block(_environment())

    entries = block[:].split("\0")
    text_entries = [entry for entry in entries if entry]

    assert f"TEMP={EMOJI_FOLDER}\\.cortex-agent-tmp" in text_entries
    assert f"LOCALAPPDATA={EMOJI_FOLDER}\\localappdata" in text_entries
    assert len(text_entries) == 7
    # Double-NUL terminated, as CreateProcessW requires.
    assert block[:].endswith("\0\0")
