"""The AppContainer environment allowlist admits the Agent's Git isolation (issue #614).

The launcher accepts a fixed set of environment variables and nothing else.
The Agent sandbox adds an isolated HOME and Git's no-system/no-global
configuration variables to every Git command, and the launcher used to reject
the first of them -- so no Git command could start in an AppContainer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_backend.agent.sandbox.appcontainer import _git_environment
from cortex_backend.core.win_appcontainer import (
    GIT_ISOLATION_ENVIRONMENT,
    AppContainerError,
    _unicode_environment_block,
)


def _required(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": r"C:\Program Files\Git\cmd",
        "PYTHONUTF8": "1",
        "SystemRoot": r"C:\Windows",
        "SystemDrive": "C:",
        "TEMP": str(tmp_path / "temp"),
        "TMP": str(tmp_path / "temp"),
        "LOCALAPPDATA": str(tmp_path / "localappdata"),
    }


def _block_text(environment: dict[str, str]) -> str:
    return _unicode_environment_block(environment)[:]


def test_the_real_git_environment_is_accepted(tmp_path: Path) -> None:
    environment = _required(tmp_path) | _git_environment(tmp_path)

    entries = _block_text(environment).rstrip("\0").split("\0")

    assert f"HOME={tmp_path / '.cortex-agent-git-home'}" in entries
    assert "GIT_CONFIG_NOSYSTEM=1" in entries
    assert len(entries) == len(environment)


def test_the_allowlist_matches_what_the_sandbox_sends(tmp_path: Path) -> None:
    assert {name.casefold() for name in _git_environment(tmp_path)} == GIT_ISOLATION_ENVIRONMENT


def test_a_variable_outside_the_allowlist_is_still_refused(tmp_path: Path) -> None:
    environment = _required(tmp_path) | {"GIT_SSH_COMMAND": "ssh -o ProxyCommand=evil"}

    with pytest.raises(AppContainerError, match="unexpected or duplicated"):
        _unicode_environment_block(environment)


def test_git_variables_do_not_stand_in_for_required_ones(tmp_path: Path) -> None:
    environment = _required(tmp_path) | _git_environment(tmp_path)
    del environment["LOCALAPPDATA"]

    with pytest.raises(AppContainerError, match="missing required"):
        _unicode_environment_block(environment)


def test_a_name_repeated_in_another_case_is_refused(tmp_path: Path) -> None:
    environment = _required(tmp_path) | _git_environment(tmp_path) | {"home": r"C:\elsewhere"}

    with pytest.raises(AppContainerError, match="unexpected or duplicated"):
        _unicode_environment_block(environment)
