"""Synthetic-byte tests for the opt-in, work-copy-only agent file capabilities."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import monotonic
from typing import Any

import pytest

from cortex_backend.agent.protocol import ToolCall
from cortex_backend.agent.tool_registry import NeedsApproval, ToolContext, ToolInterrupted, ToolRegistry, ToolResult
from cortex_backend.agent.tools import FILE_TOOLS, register_file_tools
from cortex_backend.agent.tools import files
from cortex_backend.agent.workcopy import WorkCopyError


@pytest.fixture
def workcopy(tmp_path: Path) -> tuple[Path, ToolRegistry]:
    root = tmp_path / "copy"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "sample.py").write_bytes(b"first\r\nsecond\r\nthird\n")
    (root / "readme.md").write_bytes(b"readme\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_bytes(b"SECRET GIT CONFIG")
    registry = ToolRegistry()
    register_file_tools(registry, root)
    return root, registry


def _call(registry: ToolRegistry, name: str, **arguments: Any) -> tuple[ToolResult, dict[str, Any]]:
    outcome = registry.execute(
        ToolCall("test-call", name, arguments),
        ToolContext(Event(), monotonic() + 60),
    )
    assert isinstance(outcome, ToolResult)
    data: dict[str, Any] = json.loads(outcome.content)
    return outcome, data


def _denied(registry: ToolRegistry, name: str, **arguments: Any) -> str:
    result, data = _call(registry, name, **arguments)
    assert result.success is False
    assert set(data) == {"error"}
    assert "SECRET" not in result.content
    return str(data["error"])


@pytest.mark.parametrize(("name", "arguments"), [
    ("write_file", {"path": "new.py", "content": "x"}),
    ("edit_file", {"path": "src/sample.py", "old": "first", "new": "1st"}),
    ("delete_file", {"path": "readme.md"}),
    ("move_file", {"source": "readme.md", "destination": "docs.md"}),
])
def test_gated_file_changes_say_they_only_touch_the_work_copy(
    tmp_path: Path, name: str, arguments: dict[str, Any],
) -> None:
    # The approval dialog reads "scope" to show a file change as work-copy
    # only; a file tool never sends a command or a sandbox level.
    root = tmp_path / "copy"
    root.mkdir()
    registry = ToolRegistry()
    register_file_tools(registry, root, require_approval=True)
    outcome = registry.execute(ToolCall("gate", name, arguments), ToolContext(Event(), monotonic() + 60))
    assert isinstance(outcome, NeedsApproval)
    assert outcome.request["scope"] == "workcopy"
    assert outcome.request["tool"] == name
    assert "command" not in outcome.request and "level" not in outcome.request
    assert list(root.iterdir()) == []


def test_export_and_registration_are_explicit(workcopy: tuple[Path, ToolRegistry]) -> None:
    _, registry = workcopy
    expected = {
        "list_dir", "glob", "grep", "read_file", "write_file", "edit_file",
        "delete_file", "move_file",
    }
    assert {spec.name for spec in FILE_TOOLS} == expected
    assert {spec.name for spec in registry.specs()} == expected
    assert all(spec.json_schema["additionalProperties"] is False for spec in registry.specs())
    with pytest.raises(ValueError, match="unique"):
        register_file_tools(registry, workcopy[0])


def test_list_dir_and_glob_ignore_git_and_return_bounded_paths(
    workcopy: tuple[Path, ToolRegistry],
) -> None:
    _, registry = workcopy
    result, root_list = _call(registry, "list_dir")
    assert result.success
    assert {(item["name"], item["type"]) for item in root_list["entries"]} == {
        ("readme.md", "file"), ("src", "directory"),
    }
    assert root_list["truncated"] is False
    _, listing = _call(registry, "list_dir", path="src")
    assert listing["entries"] == [{"name": "sample.py", "path": "src/sample.py", "type": "file"}]
    _, matched = _call(registry, "glob", pattern="**/*.py")
    assert matched == {"paths": ["src/sample.py"], "truncated": False}
    _, matched = _call(registry, "glob", pattern="*.py", path="src")
    assert matched["paths"] == ["src/sample.py"]
    _, matched = _call(registry, "glob", pattern="*.md")
    assert matched["paths"] == ["readme.md"]


def test_grep_python_re_and_line_numbers(workcopy: tuple[Path, ToolRegistry]) -> None:
    _, registry = workcopy
    result, searched = _call(registry, "grep", pattern=r"sec.nd", glob="**/*.py")
    assert result.success
    assert searched == {
        "hits": [{"path": "src/sample.py", "line": 2, "text": "second"}],
        "truncated": False,
    }
    _, single = _call(registry, "grep", pattern=r"^third$", path="src/sample.py")
    assert single["hits"] == [{"path": "src/sample.py", "line": 3, "text": "third"}]
    assert _denied(registry, "grep", pattern="(") == "invalid_regex"


def test_read_file_range_preserves_bytes_and_full_sha(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    contents = (root / "src" / "sample.py").read_bytes()
    _, excerpt = _call(registry, "read_file", path="src/sample.py", start_line=2, end_line=3)
    assert excerpt == {
        "path": "src/sample.py",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 3,
        "sha256": sha256(contents).hexdigest(),
        "content": "second\r\nthird\n",
    }
    assert _denied(registry, "read_file", path="src/sample.py", start_line=3, end_line=2) == "line_limit"
    assert _denied(registry, "read_file", path="src/sample.py", end_line=401) == "line_limit"
    (root / "many.txt").write_bytes(b"x\n" * 401)
    _, bounded = _call(registry, "read_file", path="many.txt")
    assert bounded["total_lines"] == 401
    assert bounded["end_line"] == 400
    assert bounded["content"] == "x\n" * 400


def test_write_create_overwrite_delete_and_move(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    _, created = _call(registry, "write_file", path="new/nested.py", content="created\n")
    assert (root / "new" / "nested.py").read_bytes() == b"created\n"
    assert created["sha256"] == sha256(b"created\n").hexdigest()
    _, overwritten = _call(registry, "write_file", path="new/nested.py", content="overwritten\r\n")
    assert overwritten["bytes"] == len(b"overwritten\r\n")
    assert (root / "new" / "nested.py").read_bytes() == b"overwritten\r\n"
    _, moved = _call(registry, "move_file", source="new/nested.py", destination="other/moved.py")
    assert moved == {"source": "new/nested.py", "destination": "other/moved.py"}
    assert not (root / "new" / "nested.py").exists()
    assert (root / "other" / "moved.py").read_bytes() == b"overwritten\r\n"
    assert _denied(registry, "move_file", source="other/moved.py", destination="src/sample.py") == "destination_exists"
    _, deleted = _call(registry, "delete_file", path="other/moved.py")
    assert deleted["deleted"] is True
    assert not (root / "other" / "moved.py").exists()
    assert _denied(registry, "delete_file", path="other/moved.py") == "not_found"


def test_edit_exactly_once_or_replace_all(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    target = root / "edit.py"
    target.write_bytes(b"alpha alpha\r\n")
    assert _denied(registry, "edit_file", path="edit.py", old="alpha", new="beta") == "ambiguous_edit"
    assert target.read_bytes() == b"alpha alpha\r\n"
    _, changed = _call(registry, "edit_file", path="edit.py", old="alpha", new="beta", replace_all=True)
    assert changed["replacements"] == 2
    assert target.read_bytes() == b"beta beta\r\n"
    _, changed = _call(registry, "edit_file", path="edit.py", old="beta ", new="B ")
    assert changed["replacements"] == 1
    assert target.read_bytes() == b"B beta\r\n"
    assert _denied(registry, "edit_file", path="edit.py", old="missing", new="nope") == "no_match"
    assert _denied(registry, "edit_file", path="edit.py", old="", new="nope") == "invalid_arguments"
    target.write_bytes(b"aaa")
    assert _denied(registry, "edit_file", path="edit.py", old="aa", new="b") == "ambiguous_edit"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.py",
        "src/../escape.py",
        "/tmp/outside.py",
        "//server/share/file.py",
        r"\\server\share\file.py",
        r"\\?\C:\Windows\file.py",
        "C:/Windows/file.py",
        r"C:\Windows\file.py",
        "C:relative.py",
        r"src\sample.py",
        "src//sample.py",
        "src/./sample.py",
        "file.py:stream",
        ".git/config",
        "src/.git/config",
        ".GIT/config",
        "PROGRA~1/file.py",
        "LONGFI~1.py",
        "CON.py",
        "src/folder./sample.py",
        "src/\u202efile.py",
    ],
)
@pytest.mark.parametrize(
    "tool",
    ["list_dir", "glob", "grep", "read_file", "write_file", "edit_file", "delete_file", "move_file"],
)
def test_every_tool_rejects_unsafe_path_forms(
    workcopy: tuple[Path, ToolRegistry], bad_path: str, tool: str,
) -> None:
    root, registry = workcopy
    arguments: dict[str, Any] = {
        "list_dir": {"path": bad_path},
        "glob": {"pattern": "**", "path": bad_path},
        "grep": {"pattern": "first", "path": bad_path},
        "read_file": {"path": bad_path},
        "write_file": {"path": bad_path, "content": "changed"},
        "edit_file": {"path": bad_path, "old": "first", "new": "changed"},
        "delete_file": {"path": bad_path},
        "move_file": {"source": "src/sample.py", "destination": bad_path},
    }[tool]
    _denied(registry, tool, **arguments)
    assert (root / "src" / "sample.py").read_bytes() == b"first\r\nsecond\r\nthird\n"
    assert (root / ".git" / "config").read_bytes() == b"SECRET GIT CONFIG"


@pytest.mark.parametrize(
    "pattern", ["../*.py", "/tmp/*.py", r"\\server\share\*.py", r"\\?\C:\*.py",
                "C:*.py", "src/../*.py", ".git/**", "file.py:stream", "PROGRA~1/*.py"],
)
def test_glob_rejects_unsafe_pattern(workcopy: tuple[Path, ToolRegistry], pattern: str) -> None:
    _, registry = workcopy
    _denied(registry, "glob", pattern=pattern)
    _denied(registry, "grep", pattern="anything", glob=pattern)


def test_limits_are_checked_before_mutation(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    exactly = "x" * files.MAX_FILE_BYTES
    result, _ = _call(registry, "write_file", path="size.py", content=exactly)
    assert result.success
    assert (root / "size.py").read_bytes() == b"x" * files.MAX_FILE_BYTES
    assert _denied(registry, "write_file", path="size.py", content=exactly + "x") == "file_too_large"
    assert (root / "size.py").stat().st_size == files.MAX_FILE_BYTES
    assert _denied(registry, "edit_file", path="size.py", old="x", new="xx",
                   replace_all=True) == "file_too_large"
    assert (root / "size.py").stat().st_size == files.MAX_FILE_BYTES
    (root / "oversized.py").write_bytes(b"z" * (files.MAX_FILE_BYTES + 1))
    assert _denied(registry, "read_file", path="oversized.py") == "file_too_large"
    assert _denied(registry, "edit_file", path="oversized.py", old="z", new="y") == "file_too_large"
    assert _denied(registry, "write_file", path="new.py", content="a\x00b") == "binary_file"
    assert not (root / "new.py").exists()


@pytest.mark.parametrize("data", [b"a\x00b", b"\xffbroken", b"\xf0\x28\x8c\x28"])
def test_binary_files_are_not_read_or_edited(
    workcopy: tuple[Path, ToolRegistry], data: bytes,
) -> None:
    root, registry = workcopy
    (root / "binary.py").write_bytes(data)
    assert _denied(registry, "read_file", path="binary.py") == "binary_file"
    assert _denied(registry, "edit_file", path="binary.py", old="a", new="b") == "binary_file"
    _, searched = _call(registry, "grep", pattern="a", path="binary.py")
    assert searched["hits"] == []
    assert (root / "binary.py").read_bytes() == data


def test_grep_is_capped_at_200_hits(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    (root / "hits.py").write_bytes(b"ping\n" * 201)
    _, searched = _call(registry, "grep", pattern="^ping$", path="hits.py")
    assert len(searched["hits"]) == files.MAX_GREP_HITS
    assert searched["truncated"] is True
    assert searched["hits"][-1]["line"] == 200


def test_grep_pathological_regex_is_preempted(
    workcopy: tuple[Path, ToolRegistry], monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, registry = workcopy
    (root / "redos.py").write_bytes(b"a" * 8000 + b"!\n")
    monkeypatch.setattr(files, "GREP_FILE_TIMEOUT_SECONDS", 0.1)
    assert _denied(registry, "grep", pattern=r"(a+)+$", path="redos.py") == "regex_timeout"


def test_links_and_junctions_are_never_followed(
    workcopy: tuple[Path, ToolRegistry], tmp_path: Path,
) -> None:
    root, registry = workcopy
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"OUTSIDE PRIVATE BYTES")
    linked_file = root / "linked.py"
    try:
        linked_file.symlink_to(outside / "secret.py")
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            pytest.skip("file links not available")
    else:
        _denied(registry, "read_file", path="linked.py")
        _denied(registry, "write_file", path="linked.py", content="changed")
        _denied(registry, "edit_file", path="linked.py", old="OUTSIDE", new="changed")
        _denied(registry, "delete_file", path="linked.py")
        _denied(registry, "move_file", source="linked.py", destination="moved.py")
    linked_dir = root / "linked"
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked_dir), str(outside)],
                check=True, capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("junction creation unavailable")
    else:
        try:
            linked_dir.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("directory links not available")
    _denied(registry, "read_file", path="linked/secret.py")
    _denied(registry, "write_file", path="linked/new.py", content="changed")
    _denied(registry, "list_dir", path="linked")
    _, matched = _call(registry, "glob", pattern="**/*.py")
    assert "linked/secret.py" not in matched["paths"]
    assert "linked.py" not in matched["paths"]
    assert (outside / "secret.py").read_bytes() == b"OUTSIDE PRIVATE BYTES"


def test_root_rename_refuses_stale_identity(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    old = root.with_name("old-copy")
    root.rename(old)
    root.mkdir()
    (root / "test.py").write_bytes(b"new root")
    assert _denied(registry, "write_file", path="test.py", content="bad") == "stale_root"
    assert (root / "test.py").read_bytes() == b"new root"
    assert (old / "src" / "sample.py").read_bytes() == b"first\r\nsecond\r\nthird\n"


def test_linked_root_is_refused_at_registration(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("root links not available")
    with pytest.raises(WorkCopyError):
        register_file_tools(ToolRegistry(), linked)


def test_no_source_text_in_failed_results_or_logs(
    workcopy: tuple[Path, ToolRegistry], caplog: pytest.LogCaptureFixture,
) -> None:
    root, registry = workcopy
    (root / "private.py").write_bytes(b"CONFIDENTIAL-SYNTHETIC-CONTENTS")
    error = _denied(registry, "edit_file", path="private.py", old="missing", new="value")
    assert error == "no_match"
    assert "CONFIDENTIAL-SYNTHETIC-CONTENTS" not in caplog.text
    assert _denied(registry, "read_file", path="never-here.py") == "not_found"


def test_cancelled_call_never_changes_bytes(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    stop = Event()
    stop.set()
    with pytest.raises(ToolInterrupted):
        registry.execute(
            ToolCall("cancelled", "delete_file", {"path": "src/sample.py"}),
            ToolContext(stop, monotonic() + 30),
        )
    assert (root / "src" / "sample.py").read_bytes() == b"first\r\nsecond\r\nthird\n"


def test_directory_enumeration_result_cap(workcopy: tuple[Path, ToolRegistry]) -> None:
    root, registry = workcopy
    many = root / "many"
    many.mkdir()
    for number in range(files.MAX_RESULTS + 1):
        (many / f"file_{number:03}.py").write_bytes(b"fixture\n")
    _, listing = _call(registry, "list_dir", path="many")
    assert len(listing["entries"]) == files.MAX_RESULTS
    assert listing["truncated"] is True
    _, matched = _call(registry, "glob", path="many", pattern="*.py")
    assert len(matched["paths"]) == files.MAX_RESULTS
    assert matched["truncated"] is True


def test_gated_edits_show_the_proposed_text_bounded_for_display(tmp_path: Path) -> None:
    root = tmp_path / "copy"
    root.mkdir()
    registry = ToolRegistry()
    register_file_tools(registry, root, require_approval=True)
    context = ToolContext(Event(), monotonic() + 60)

    edit = registry.execute(ToolCall("gate", "edit_file", {
        "path": "calc.py", "old": "return left - right", "new": "return left + right",
    }), context)
    assert isinstance(edit, NeedsApproval)
    assert edit.request["preview"] == {
        "old": "return left - right", "new": "return left + right", "replace_all": False, "truncated": False,
    }

    long_text = "x" * 5000
    write = registry.execute(ToolCall("gate", "write_file", {"path": "big.txt", "content": long_text}), context)
    assert isinstance(write, NeedsApproval)
    assert write.request["preview"] == {"content": "x" * 4000, "truncated": True}

    every = registry.execute(ToolCall("gate", "edit_file", {
        "path": "calc.py", "old": "a", "new": "b", "replace_all": True,
    }), context)
    assert isinstance(every, NeedsApproval)
    assert every.request["preview"]["replace_all"] is True  # type: ignore[index]

    for name, arguments in (("delete_file", {"path": "calc.py"}),
                            ("move_file", {"source": "calc.py", "destination": "new.py"})):
        outcome = registry.execute(ToolCall("gate", name, arguments), context)
        assert isinstance(outcome, NeedsApproval) and "preview" not in outcome.request
    assert list(root.iterdir()) == []
