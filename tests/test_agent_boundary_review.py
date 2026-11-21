"""Fail-closed, disposable qualification cases for the agent work-copy boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_backend.agent.tools import files as file_tools
from cortex_backend.agent import workcopy as workcopy_module
import tools.artifact_boundary_review as review


_PATH_CASES = (
    "agent_file_backslash_parent",
    "agent_file_absolute",
    "agent_file_unc",
    "agent_file_drive_relative",
    "agent_file_device_con",
    "agent_file_device_nul",
    "agent_file_device_com1",
    "agent_file_device_aux",
    "agent_file_ads",
)
_LINK_CASES = ("agent_file_symlink", "agent_file_junction")
_ACCEPT_CASES = ("agent_accept_root_binding", "agent_accept_link_swap", "agent_undo_exact_bytes")
_AGENT_CASES = _PATH_CASES + _LINK_CASES + _ACCEPT_CASES


def _run_one(monkeypatch: pytest.MonkeyPatch, case_name: str) -> dict[str, object]:
    case = dict(review._CASES)[case_name]
    monkeypatch.setattr(review, "_CASES", ((case_name, case),))
    return review.run_review()


def _assert_mutation_detected(monkeypatch: pytest.MonkeyPatch, case_name: str) -> None:
    result = _run_one(monkeypatch, case_name)
    assert result["status"] == "failed"
    assert result["failed"] == 1
    assert result["blocked"] == 0
    assert result["outcomes"][0]["case"] == case_name
    assert result["outcomes"][0]["status"] == "failed"


def test_new_cases_are_named_and_only_add_to_existing_corpus() -> None:
    names = [name for name, _case in review._CASES]
    assert len(names) == 12 + len(_AGENT_CASES)
    assert names[12:] == list(_AGENT_CASES)
    assert len(names) == len(set(names))


@pytest.mark.parametrize("case_name", _PATH_CASES)
def test_invalid_paths_fail_when_file_path_guard_is_removed(monkeypatch: pytest.MonkeyPatch, case_name: str) -> None:
    # Redirect each unsafe spelling to a known file in the disposable copy. In
    # particular, neither the UNC name nor a reserved Windows device is opened.
    monkeypatch.setattr(file_tools, "_file_name", lambda _path: "source.py")
    _assert_mutation_detected(monkeypatch, case_name)


@pytest.mark.parametrize("case_name", _PATH_CASES[:4] + ("agent_file_ads",))
def test_invalid_paths_fail_when_workcopy_validator_is_removed(
    monkeypatch: pytest.MonkeyPatch, case_name: str,
) -> None:
    monkeypatch.setattr(file_tools, "_valid_path", lambda _path: "source.py")
    _assert_mutation_detected(monkeypatch, case_name)


@pytest.mark.parametrize("case_name", _PATH_CASES[4:8])
def test_device_cases_fail_without_reserved_name_guard(monkeypatch: pytest.MonkeyPatch, case_name: str) -> None:
    monkeypatch.setattr(file_tools, "_checked_parts", lambda _relative: None)
    # Do not even attempt to open a Windows device name in this mutation test.
    monkeypatch.setattr(file_tools._Files, "_write", lambda _self, path, _contents: path)
    _assert_mutation_detected(monkeypatch, case_name)


def _unguarded_file(self, path, *, create_parents=False, required=True):
    target = self.root / path
    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = target.stat()  # Deliberately follows a symlink or junction.
    except FileNotFoundError:
        if required:
            raise file_tools.FileToolError("not_found") from None
        info = None
    return path, target, info


@pytest.mark.parametrize("case_name", _LINK_CASES)
def test_link_cases_fail_without_no_follow_resolution(monkeypatch: pytest.MonkeyPatch, case_name: str) -> None:
    initial = _run_one(monkeypatch, case_name)
    if initial["status"] == "blocked":
        pytest.skip("link primitive unavailable on this host")
    assert initial["status"] == "passed"
    monkeypatch.setattr(file_tools._Files, "_file", _unguarded_file)
    _assert_mutation_detected(monkeypatch, case_name)


def test_root_binding_fails_when_identity_guard_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workcopy_module, "_check_root", lambda _root, _identity: None)
    _assert_mutation_detected(monkeypatch, "agent_accept_root_binding")


def test_swapped_target_fails_without_link_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def follow_file(root: Path, relative: str):
        path = root / relative
        if not path.exists():
            return None
        info = path.stat()  # Deliberately follows the swapped target link.
        return workcopy_module._File(path.read_bytes(), info.st_mode)

    initial = _run_one(monkeypatch, "agent_accept_link_swap")
    if initial["status"] == "blocked":
        pytest.skip("symlink primitive unavailable on this host")
    assert initial["status"] == "passed"
    monkeypatch.setattr(workcopy_module, "_file_at", follow_file)
    _assert_mutation_detected(monkeypatch, "agent_accept_link_swap")


def test_undo_case_fails_if_restore_changes_newline_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    original_write = workcopy_module._write

    def corrupt_restore(root: Path, relative: str, contents: bytes, mode: int | None) -> None:
        if root.name == "agent-project" and relative == "source.py" and contents == review._AGENT_ORIGINAL:
            contents = contents.replace(b"\r\n", b"\n")
        original_write(root, relative, contents, mode)

    monkeypatch.setattr(workcopy_module, "_write", corrupt_restore)
    _assert_mutation_detected(monkeypatch, "agent_undo_exact_bytes")


def test_missing_junction_primitive_blocks_not_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_link: Path, _target: Path) -> None:
        raise OSError("unavailable")

    monkeypatch.setattr(review, "_junction_primitive", missing)
    result = _run_one(monkeypatch, "agent_file_junction")
    assert result["status"] == "blocked"
    assert result["outcomes"] == [
        {"case": "agent_file_junction", "status": "blocked", "reason": "junction_unavailable"},
    ]
    assert review._EXIT_CODES[result["status"]] == 2


def test_missing_symlink_primitive_blocks_not_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_path: Path, _target: Path, *, target_is_directory: bool = False) -> None:
        raise OSError("unavailable")

    monkeypatch.setattr(Path, "symlink_to", missing)
    result = _run_one(monkeypatch, "agent_file_symlink")
    assert result["status"] == "blocked"
    assert result["outcomes"] == [
        {"case": "agent_file_symlink", "status": "blocked", "reason": "symlink_unavailable"},
    ]
    assert review._EXIT_CODES[result["status"]] == 2
