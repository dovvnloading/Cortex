"""Synthetic, consent-gated agent work copies and durable recovery."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from cortex_backend.agent.workcopy import WorkCopyError, create
from cortex_backend.services.workspace_checkpoint import WorkspaceCheckpointStore


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_bytes(b"print('original')\r\n")
    (root / "delete.py").write_text("old file\n", encoding="utf-8")
    (root / "empty.py").write_bytes(b"")
    (root / ".gitignore").write_text("ignored.py\nlogs/\n*.tmp\n", encoding="utf-8")
    (root / "ignored.py").write_text("ignore me", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "private.py").write_text("ignore me", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dependency.py").write_text("ignored", encoding="utf-8")
    (root / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (root / "credentials.json").write_text("secret", encoding="utf-8")
    return root


def _copy(tmp_path: Path, root: Path, name: str = "run-1"):
    return create(root, name, data_dir=tmp_path / "cortex-data", confirmed=True)


def test_non_git_copy_excludes_secrets_dependencies_and_gitignore(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    assert work.path.is_relative_to(tmp_path / "cortex-data")
    assert (work.path / "src" / "app.py").read_bytes() == b"print('original')\r\n"
    assert not (work.path / ".env").exists()
    assert not (work.path / "credentials.json").exists()
    assert not (work.path / "node_modules").exists()
    assert not (work.path / "ignored.py").exists()
    assert not (work.path / "logs").exists()
    assert work.changeset() == {}
    work.discard()
    assert not work.path.exists()
    assert (root / "src" / "app.py").is_file()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_git_worktree_includes_dirty_and_untracked_files_without_hooks_or_filters(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitattributes").write_text("*.py filter=sentinel\n", encoding="utf-8")
    _git(root, "add", "src/app.py", "delete.py", ".gitignore", ".gitattributes")
    _git(root, "commit", "-qm", "baseline")
    head = _git(root, "rev-parse", "HEAD")
    marker = tmp_path / "executed-hook.txt"
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nprintf triggered > '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(root, "config", "filter.sentinel.smudge", f"sh -c 'echo filter > {marker}; cat'")
    (root / "src" / "app.py").write_bytes(b"print('dirty')\r\n")
    (root / "delete.py").unlink()
    (root / "new.py").write_text("untracked = True\n", encoding="utf-8")
    work = _copy(tmp_path, root)
    assert _git(work.path, "rev-parse", "HEAD") == head
    assert (work.path / ".git").is_file()
    assert (work.path / "src" / "app.py").read_bytes() == b"print('dirty')\r\n"
    assert not (work.path / "delete.py").exists()
    assert (work.path / "new.py").is_file()
    assert not marker.exists()
    assert work.changeset() == {}
    work.discard()
    assert not marker.exists()
    assert not work.path.exists()


def test_add_modify_delete_accept_and_durable_undo(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "src" / "app.py").write_bytes(b"print('changed')\r\n")
    (work.path / "delete.py").unlink()
    (work.path / "new.py").write_text("new = 1\n", encoding="utf-8")
    (work.path / "empty.py").write_text("not empty", encoding="utf-8")
    changes = work.changeset()
    assert {path: change.kind for path, change in changes.items()} == {
        "delete.py": "delete", "empty.py": "modify",
        "new.py": "add", "src/app.py": "modify",
    }
    assert changes["new.py"].baseline_sha256 is None
    assert changes["new.py"].new_sha256 == sha256(b"new = 1\n").hexdigest()
    assert changes["delete.py"].new_sha256 is None
    assert changes["delete.py"].baseline_sha256 == sha256(b"old file\n").hexdigest()
    assert "-print('original')" in changes["src/app.py"].diff
    assert "+print('changed')" in changes["src/app.py"].diff
    assert not (root / "new.py").exists()
    with pytest.raises(WorkCopyError, match="work copy") as denied:
        work.accept(changes.keys(), confirmed=False)
    assert denied.value.code == "consent_required"
    assert work.accept(changes.keys(), confirmed=True) == tuple(sorted(changes))
    assert not (root / "delete.py").exists()
    assert (root / "new.py").read_bytes() == b"new = 1\n"
    assert (root / "src" / "app.py").read_bytes() == b"print('changed')\r\n"
    assert work.changeset() == {}
    journal = WorkspaceCheckpointStore(work.data_dir / "workspace_checkpoints.sqlite")
    saved = journal.get(owner="agent-workcopy", checkpoint_id=work.checkpoint_id)
    assert {entry.path for entry in saved.files} == set(changes)
    assert next(entry for entry in saved.files if entry.path == "new.py").original == b""
    with pytest.raises(WorkCopyError) as denied_undo:
        work.undo(confirmed=False)
    assert denied_undo.value.code == "consent_required"
    assert work.undo(confirmed=True) == tuple(sorted(changes))
    assert not (root / "new.py").exists()
    assert (root / "delete.py").read_bytes() == b"old file\n"
    assert (root / "empty.py").read_bytes() == b""
    assert (root / "src" / "app.py").read_bytes() == b"print('original')\r\n"
    work.discard()


def test_external_edit_refuses_whole_accept_before_any_write(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "new.py").write_text("addition", encoding="utf-8")
    (work.path / "src" / "app.py").write_text("agent edit", encoding="utf-8")
    (root / "src" / "app.py").write_text("external edit", encoding="utf-8")
    with pytest.raises(WorkCopyError) as failure:
        work.accept(["new.py", "src/app.py"], confirmed=True)
    assert failure.value.code == "stale_source"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "external edit"
    assert not (root / "new.py").exists()
    work.discard()


def test_external_edit_after_accept_refuses_undo(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "src" / "app.py").write_text("agent edit", encoding="utf-8")
    work.accept(["src/app.py"], confirmed=True)
    (root / "src" / "app.py").write_text("external edit", encoding="utf-8")
    with pytest.raises(WorkCopyError) as failure:
        work.undo(confirmed=True)
    assert failure.value.code == "stale_source"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "external edit"
    work.discard()


def test_sequential_edit_then_delete_can_undo_original(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    path = work.path / "src" / "app.py"
    path.write_text("first\n", encoding="utf-8")
    work.accept(["src/app.py"], confirmed=True)
    path.unlink()
    work.accept(["src/app.py"], confirmed=True)
    assert not (root / "src" / "app.py").exists()
    work.undo(confirmed=True)
    assert (root / "src" / "app.py").read_bytes() == b"print('original')\r\n"
    work.discard()


def test_junction_or_directory_link_is_never_followed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.py").write_text("private", encoding="utf-8")
    link = root / "linked"
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)], check=True, capture_output=True)
    else:
        try:
            link.symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("directory links are unavailable")
    work = _copy(tmp_path, root)
    assert not (work.path / "linked").exists()
    (work.path / "new.py").write_text("ok", encoding="utf-8")
    with pytest.raises(WorkCopyError):
        work.accept(["linked/secret.py", "new.py"], confirmed=True)
    assert not (root / "new.py").exists()
    assert (external / "secret.py").read_text(encoding="utf-8") == "private"
    work.discard()


def test_discard_unlinks_links_in_copy_without_touching_destination(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    outside = tmp_path / "valuable.py"
    outside.write_text("leave me", encoding="utf-8")
    try:
        (work.path / "shortcut.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    work.discard()
    assert outside.read_text(encoding="utf-8") == "leave me"
    assert not work.path.exists()


def test_consent_and_path_traversal_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    with pytest.raises(WorkCopyError) as denied:
        create(root, "unapproved", confirmed=False, data_dir=tmp_path / "cortex-data")
    assert denied.value.code == "consent_required"
    assert not (tmp_path / "cortex-data").exists()
    work = _copy(tmp_path, root)
    (work.path / "new.py").write_text("content", encoding="utf-8")
    for path in ("../escape.py", "/tmp/escape.py", "src/../escape.py", "C:/Windows/win.ini", ".env"):
        with pytest.raises(WorkCopyError):
            work.accept([path], confirmed=True)
    assert not (root / "new.py").exists()
    work.discard()


def test_refuse_every_protected_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    data = tmp_path / "cortex-data"
    data.mkdir()
    (data / "nested").mkdir()
    windir = tmp_path / "windows"
    windir.mkdir()
    user = tmp_path / "profile"
    user.mkdir()
    monkeypatch.setenv("WINDIR", str(windir))
    monkeypatch.setenv("USERPROFILE", str(user))
    choices = [
        "relative/project", "\\\\server\\share\\project", "C:\\", "/",
        str(windir), str(user), str(data), str(data / "nested"),
    ]
    for index, value in enumerate(choices):
        with pytest.raises(WorkCopyError) as failure:
            create(value, f"bad-{index}", data_dir=data, confirmed=True)
        assert failure.value.code in {"invalid_root", "protected_root"}
    linked = tmp_path / "linked-project"
    try:
        linked.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(WorkCopyError):
            create(linked, "linked", data_dir=data, confirmed=True)
    assert not (data / "agent_workcopies").exists()


def test_originally_added_file_still_disappears_after_multiple_accepts(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    added = work.path / "new.py"
    added.write_text("first", encoding="utf-8")
    work.accept(["new.py"], confirmed=True)
    added.write_text("second", encoding="utf-8")
    work.accept(["new.py"], confirmed=True)
    assert (root / "new.py").read_text(encoding="utf-8") == "second"
    work.undo(confirmed=True)
    assert not (root / "new.py").exists()
    work.discard()


def test_external_creation_refuses_added_file(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "new.py").write_text("agent edit", encoding="utf-8")
    (root / "new.py").write_text("external edit", encoding="utf-8")
    with pytest.raises(WorkCopyError) as failure:
        work.accept(["new.py"], confirmed=True)
    assert failure.value.code == "stale_source"
    assert (root / "new.py").read_text(encoding="utf-8") == "external edit"
    work.discard()


def test_accept_rejects_replaced_directory_link(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "src" / "app.py").write_text("agent edit", encoding="utf-8")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "app.py").write_text("private", encoding="utf-8")
    (root / "src" / "app.py").unlink()
    (root / "src").rmdir()
    try:
        (root / "src").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory links unavailable")
    with pytest.raises(WorkCopyError) as failure:
        work.accept(["src/app.py"], confirmed=True)
    assert failure.value.code == "unsafe_path"
    assert (external / "app.py").read_text(encoding="utf-8") == "private"
    work.discard()


def test_nested_gitignore_and_anchored_negation(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / ".gitignore").write_text("/top.py\nnested/*.py\n!nested/keep.py\n", encoding="utf-8")
    (root / "top.py").write_text("root ignored", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "top.py").write_text("still included", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "skip.py").write_text("ignored", encoding="utf-8")
    (root / "nested" / "keep.py").write_text("included", encoding="utf-8")
    (root / "nested" / "deep").mkdir()
    (root / "nested" / "deep" / "not_ignored.py").write_text("included", encoding="utf-8")
    (root / "nested" / ".gitignore").write_text("nested_only.txt\n", encoding="utf-8")
    (root / "nested" / "nested_only.txt").write_text("ignore", encoding="utf-8")
    work = _copy(tmp_path, root)
    assert not (work.path / "top.py").exists()
    assert (work.path / "sub" / "top.py").is_file()
    assert not (work.path / "nested" / "skip.py").exists()
    assert (work.path / "nested" / "keep.py").is_file()
    assert (work.path / "nested" / "deep" / "not_ignored.py").is_file()
    assert not (work.path / "nested" / "nested_only.txt").exists()
    assert (work.path / "nested" / ".gitignore").is_file()
    work.discard()


def test_empty_added_and_deleted_files_are_distinguished(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "empty.py").unlink()
    (work.path / "new.py").write_bytes(b"")
    changes = work.changeset()
    assert changes["empty.py"].kind == "delete"
    assert changes["new.py"].kind == "add"
    assert changes["empty.py"].baseline_sha256 == sha256(b"").hexdigest()
    assert changes["new.py"].new_sha256 == sha256(b"").hexdigest()
    work.accept(["empty.py", "new.py"], confirmed=True)
    assert not (root / "empty.py").exists()
    assert (root / "new.py").is_file()
    work.undo(confirmed=True)
    assert (root / "empty.py").is_file()
    assert not (root / "new.py").exists()
    work.discard()


def test_durable_restore_after_copy_discard_and_simulated_restart(tmp_path: Path) -> None:
    from cortex_backend.agent.workcopy import restore

    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "new.py").write_text("new content", encoding="utf-8")
    (work.path / "delete.py").unlink()
    work.accept(["new.py", "delete.py"], confirmed=True)
    work.discard()
    with pytest.raises(WorkCopyError) as refused:
        restore(root, work.run_id, confirmed=False, data_dir=work.data_dir)
    assert refused.value.code == "consent_required"
    recovered = restore(root, work.run_id, confirmed=True, data_dir=work.data_dir)
    assert recovered == ("delete.py", "new.py")
    assert not (root / "new.py").exists()
    assert (root / "delete.py").read_bytes() == b"old file\n"
