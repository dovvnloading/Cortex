"""Synthetic, consent-gated agent work copies and durable recovery."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from cortex_backend.agent.workcopy import WorkCopyError, create
from cortex_backend.services.workspace_checkpoint import WorkspaceCheckpointStore


_DELETED_FILE_BYTES = b"old file\n"
_NEW_FILE_BYTES = b"new = 1\n"
_EMPTY_FILE_BYTES = b""

def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_bytes(b"print('original')\r\n")
    (root / "delete.py").write_bytes(_DELETED_FILE_BYTES)
    (root / "empty.py").write_bytes(_EMPTY_FILE_BYTES)
    (root / ".gitignore").write_bytes(b"ignored.py\nlogs/\n*.tmp\n")
    (root / "ignored.py").write_bytes(b"ignore me")
    (root / "logs").mkdir()
    (root / "logs" / "private.py").write_bytes(b"ignore me")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dependency.py").write_bytes(b"ignored")
    (root / ".env").write_bytes(b"TOKEN=secret")
    (root / "credentials.json").write_bytes(b"secret")
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


def test_ignored_generated_files_do_not_count_against_the_copy_limits(tmp_path: Path) -> None:
    """#608: the whole project was read before the ignore rules applied, so
    2,050 tiny ignored files hit the 2,048-file limit and no run could start."""
    root = tmp_path / "project"
    (root / "generated").mkdir(parents=True)
    (root / "app.py").write_bytes(b"print('tracked')\n")
    (root / ".gitignore").write_bytes(b"generated/\n")
    for index in range(2050):
        (root / "generated" / f"item{index}.json").write_bytes(b"{}")

    work = _copy(tmp_path, root)
    try:
        assert (work.path / "app.py").read_bytes() == b"print('tracked')\n"
        assert not (work.path / "generated").exists()
        assert work.changeset() == {}
    finally:
        work.discard()


def test_nested_ignore_rules_apply_below_their_folder_and_ignored_folders_stay_ignored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    (root / "pkg" / "build").mkdir(parents=True)
    (root / "pkg" / ".gitignore").write_bytes(b"*.gen.py\n")
    (root / "pkg" / "keep.py").write_bytes(b"keep = 1\n")
    (root / "pkg" / "out.gen.py").write_bytes(b"generated = 1\n")
    (root / "top.gen.py").write_bytes(b"not under pkg = 1\n")
    (root / ".gitignore").write_bytes(b"pkg/build/\n!pkg/build/wanted.py\n")
    (root / "pkg" / "build" / "wanted.py").write_bytes(b"inside an ignored folder\n")

    work = _copy(tmp_path, root)
    try:
        assert (work.path / "pkg" / "keep.py").is_file()
        assert not (work.path / "pkg" / "out.gen.py").exists()
        assert (work.path / "top.gen.py").is_file(), "a nested rule reached outside its folder"
        # As in Git, a file in an ignored folder cannot be re-included.
        assert not (work.path / "pkg" / "build").exists()
    finally:
        work.discard()


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_git_worktree_includes_dirty_and_untracked_files_without_hooks_or_filters(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitattributes").write_bytes(b"*.py filter=sentinel\n")
    _git(root, "add", "src/app.py", "delete.py", ".gitignore", ".gitattributes")
    _git(root, "commit", "-qm", "baseline")
    head = _git(root, "rev-parse", "HEAD")
    marker = tmp_path / "executed-hook.txt"
    hook = root / ".git" / "hooks" / "post-checkout"
    hook.write_bytes(b"#!/bin/sh\nprintf triggered > '" + str(marker).encode("utf-8") + b"'\n")
    hook.chmod(0o755)
    _git(root, "config", "filter.sentinel.smudge", f"sh -c 'echo filter > {marker}; cat'")
    (root / "src" / "app.py").write_bytes(b"print('dirty')\r\n")
    (root / "delete.py").unlink()
    (root / "new.py").write_bytes(b"untracked = True\n")
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
    (work.path / "new.py").write_bytes(_NEW_FILE_BYTES)
    (work.path / "empty.py").write_bytes(b"not empty")
    changes = work.changeset()
    assert {path: change.kind for path, change in changes.items()} == {
        "delete.py": "delete", "empty.py": "modify",
        "new.py": "add", "src/app.py": "modify",
    }
    assert changes["new.py"].baseline_sha256 is None
    assert changes["new.py"].new_sha256 == sha256(_NEW_FILE_BYTES).hexdigest()
    assert changes["delete.py"].new_sha256 is None
    assert changes["delete.py"].baseline_sha256 == sha256(_DELETED_FILE_BYTES).hexdigest()
    assert "-print('original')" in changes["src/app.py"].diff
    assert "+print('changed')" in changes["src/app.py"].diff
    assert not (root / "new.py").exists()
    with pytest.raises(WorkCopyError, match="work copy") as denied:
        work.accept(changes.keys(), confirmed=False)
    assert denied.value.code == "consent_required"
    assert work.accept(changes.keys(), confirmed=True) == tuple(sorted(changes))
    assert not (root / "delete.py").exists()
    assert (root / "new.py").read_bytes() == _NEW_FILE_BYTES
    assert (root / "src" / "app.py").read_bytes() == b"print('changed')\r\n"
    assert work.changeset() == {}
    journal = WorkspaceCheckpointStore(work.data_dir / "workspace_checkpoints.sqlite")
    saved = journal.get(owner="agent-workcopy", checkpoint_id=work.checkpoint_id)
    assert {entry.path for entry in saved.files} == set(changes)
    assert next(entry for entry in saved.files if entry.path == "new.py").original == _EMPTY_FILE_BYTES
    with pytest.raises(WorkCopyError) as denied_undo:
        work.undo(confirmed=False)
    assert denied_undo.value.code == "consent_required"
    assert work.undo(confirmed=True) == tuple(sorted(changes))
    assert not (root / "new.py").exists()
    assert (root / "delete.py").read_bytes() == _DELETED_FILE_BYTES
    assert (root / "empty.py").read_bytes() == _EMPTY_FILE_BYTES
    assert (root / "src" / "app.py").read_bytes() == b"print('original')\r\n"
    work.discard()


def test_a_second_accepted_run_keeps_the_first_runs_changes(tmp_path: Path) -> None:
    """#580: the first accept's undo point blocked every later accept on the
    project with recovery_required, and nothing let the user keep its edits."""
    root = _project(tmp_path)
    first = _copy(tmp_path, root, "run-a")
    (first.path / "src" / "app.py").write_bytes(b"first accepted change\n")
    first.accept(["src/app.py"], confirmed=True)
    first.discard()

    second = _copy(tmp_path, root, "run-b")
    try:
        (second.path / "delete.py").write_bytes(b"second accepted change\n")
        assert second.accept(["delete.py"], confirmed=True) == ("delete.py",)
        assert (root / "src" / "app.py").read_bytes() == b"first accepted change\n"
        assert (root / "delete.py").read_bytes() == b"second accepted change\n"

        # Each undo point restores only its own run's files.
        assert second.undo(confirmed=True) == ("delete.py",)
        assert (root / "delete.py").read_bytes() == _DELETED_FILE_BYTES
        assert (root / "src" / "app.py").read_bytes() == b"first accepted change\n"
    finally:
        second.discard()


def test_undoing_an_earlier_accept_after_a_later_one_changed_the_file_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path)
    first = _copy(tmp_path, root, "run-a")
    (first.path / "src" / "app.py").write_bytes(b"first\n")
    first.accept(["src/app.py"], confirmed=True)
    second = _copy(tmp_path, root, "run-b")
    (second.path / "src" / "app.py").write_bytes(b"second\n")
    second.accept(["src/app.py"], confirmed=True)

    with pytest.raises(WorkCopyError) as refused:
        first.undo(confirmed=True)

    assert refused.value.code == "stale_source"
    assert (root / "src" / "app.py").read_bytes() == b"second\n"
    first.discard()
    second.discard()


def test_an_unfinished_write_still_blocks_a_new_accept_on_the_project(tmp_path: Path) -> None:
    root = _project(tmp_path)
    first = _copy(tmp_path, root, "run-a")
    store = WorkspaceCheckpointStore(first.data_dir / "workspace_checkpoints.sqlite")
    info = root.lstat()
    store.prepare(
        owner="agent-workcopy", checkpoint_id="interrupted-batch", root=root,
        root_identity=(info.st_dev, info.st_ino),
        candidates={"delete.py": (_DELETED_FILE_BYTES, b"half written\n", 0o100644)},
    )
    (first.path / "src" / "app.py").write_bytes(b"blocked change\n")

    with pytest.raises(WorkCopyError) as blocked:
        first.accept(["src/app.py"], confirmed=True)

    assert blocked.value.code == "recovery_required"
    assert (root / "src" / "app.py").read_bytes() == b"print('original')\r\n"
    first.discard()


def test_undo_never_rebases_unrelated_external_files_into_staged_deletions(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "src" / "app.py").write_bytes(b"agent change\\n")
    (work.path / "delete.py").write_bytes(b"unaccepted staged change\\n")
    work.accept(["src/app.py"], confirmed=True)

    # These edits happened in the real project, outside the agent work copy.
    external = root / "external.py"
    external.write_bytes(b"keep this independent file\\n")
    (root / "empty.py").write_bytes(b"independent source edit\\n")

    assert work.undo(confirmed=True) == ("src/app.py",)
    changes = work.changeset()
    assert {path: change.kind for path, change in changes.items()} == {
        "delete.py": "modify", "src/app.py": "modify",
    }
    assert "external.py" not in changes and "empty.py" not in changes
    with pytest.raises(WorkCopyError) as invalid:
        work.accept(["external.py"], confirmed=True)
    assert invalid.value.code == "invalid_selection"
    assert external.read_bytes() == b"keep this independent file\\n"
    assert (root / "empty.py").read_bytes() == b"independent source edit\\n"
    work.discard()


def test_external_edit_refuses_whole_accept_before_any_write(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "new.py").write_bytes(b"addition")
    (work.path / "src" / "app.py").write_bytes(b"agent edit")
    (root / "src" / "app.py").write_bytes(b"external edit")
    with pytest.raises(WorkCopyError) as failure:
        work.accept(["new.py", "src/app.py"], confirmed=True)
    assert failure.value.code == "stale_source"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "external edit"
    assert not (root / "new.py").exists()
    work.discard()


def test_external_edit_after_accept_refuses_undo(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "src" / "app.py").write_bytes(b"agent edit")
    work.accept(["src/app.py"], confirmed=True)
    (root / "src" / "app.py").write_bytes(b"external edit")
    with pytest.raises(WorkCopyError) as failure:
        work.undo(confirmed=True)
    assert failure.value.code == "stale_source"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "external edit"
    work.discard()


def test_sequential_edit_then_delete_can_undo_original(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    path = work.path / "src" / "app.py"
    path.write_bytes(b"first\n")
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
    (external / "secret.py").write_bytes(b"private")
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
    (work.path / "new.py").write_bytes(b"ok")
    with pytest.raises(WorkCopyError):
        work.accept(["linked/secret.py", "new.py"], confirmed=True)
    assert not (root / "new.py").exists()
    assert (external / "secret.py").read_text(encoding="utf-8") == "private"
    work.discard()


def test_discard_retries_read_only_shadow_git_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = _copy(tmp_path, _project(tmp_path))
    git_object = work.path / ".cortex-agent-git" / "objects" / "1f" / ("b1" * 19)
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"object")
    git_object.chmod(stat.S_IREAD)

    original_unlink = Path.unlink

    def reject_read_only(path: Path, missing_ok: bool = False) -> None:
        # Linux can unlink read-only files; emulate the Windows WinError 5.
        if path == git_object and not (path.stat().st_mode & stat.S_IWRITE):
            raise PermissionError("read-only Git object")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", reject_read_only)
    work.discard()
    assert not work.path.exists()


def test_discard_unlinks_links_in_copy_without_touching_destination(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    outside = tmp_path / "valuable.py"
    outside.write_bytes(b"leave me")
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
    (work.path / "new.py").write_bytes(b"content")
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
    added.write_bytes(b"first")
    work.accept(["new.py"], confirmed=True)
    added.write_bytes(b"second")
    work.accept(["new.py"], confirmed=True)
    assert (root / "new.py").read_text(encoding="utf-8") == "second"
    work.undo(confirmed=True)
    assert not (root / "new.py").exists()
    work.discard()


def test_external_creation_refuses_added_file(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "new.py").write_bytes(b"agent edit")
    (root / "new.py").write_bytes(b"external edit")
    with pytest.raises(WorkCopyError) as failure:
        work.accept(["new.py"], confirmed=True)
    assert failure.value.code == "stale_source"
    assert (root / "new.py").read_text(encoding="utf-8") == "external edit"
    work.discard()


def test_accept_rejects_replaced_directory_link(tmp_path: Path) -> None:
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    (work.path / "src" / "app.py").write_bytes(b"agent edit")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "app.py").write_bytes(b"private")
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
    (root / ".gitignore").write_bytes(b"/top.py\nnested/*.py\n!nested/keep.py\n")
    (root / "top.py").write_bytes(b"root ignored")
    (root / "sub").mkdir()
    (root / "sub" / "top.py").write_bytes(b"still included")
    (root / "nested").mkdir()
    (root / "nested" / "skip.py").write_bytes(b"ignored")
    (root / "nested" / "keep.py").write_bytes(b"included")
    (root / "nested" / "deep").mkdir()
    (root / "nested" / "deep" / "not_ignored.py").write_bytes(b"included")
    (root / "nested" / ".gitignore").write_bytes(b"nested_only.txt\n")
    (root / "nested" / "nested_only.txt").write_bytes(b"ignore")
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
    (work.path / "new.py").write_bytes(_EMPTY_FILE_BYTES)
    changes = work.changeset()
    assert changes["empty.py"].kind == "delete"
    assert changes["new.py"].kind == "add"
    assert changes["empty.py"].baseline_sha256 == sha256(_EMPTY_FILE_BYTES).hexdigest()
    assert changes["new.py"].new_sha256 == sha256(_EMPTY_FILE_BYTES).hexdigest()
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
    (work.path / "new.py").write_bytes(b"new content")
    (work.path / "delete.py").unlink()
    work.accept(["new.py", "delete.py"], confirmed=True)
    work.discard()
    with pytest.raises(WorkCopyError) as refused:
        restore(root, work.run_id, confirmed=False, data_dir=work.data_dir)
    assert refused.value.code == "consent_required"
    recovered = restore(root, work.run_id, confirmed=True, data_dir=work.data_dir)
    assert recovered == ("delete.py", "new.py")
    assert not (root / "new.py").exists()
    assert (root / "delete.py").read_bytes() == _DELETED_FILE_BYTES


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_a_restart_removes_work_copies_the_earlier_process_left(tmp_path: Path) -> None:
    """#607: shutdown kept the copies and a new process had no run objects to
    discard them with, so copies and linked worktrees stayed for good."""
    from cortex_backend.agent.run_store import AgentRunStore
    from cortex_backend.services.agent_runs import AgentRuns

    data = tmp_path / "cortex-data"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "app.py").write_bytes(b"print('tracked')\n")
    _git(repo, "add", "app.py")
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid", "commit", "-qm", "init")
    plain = _project(tmp_path)

    store = AgentRunStore(data / "agent" / "runs.sqlite")
    linked = create(repo, "left-linked", data_dir=data, confirmed=True)
    loose = create(plain, "left-plain", data_dir=data, confirmed=True)
    for copy, root in ((linked, repo), (loose, plain)):
        (copy.path / "unaccepted.py").write_bytes(b"never accepted = 1\n")
        store.create_run(owner="earlier-session", project_root=root, workcopy=copy.path,
                         mode="ask", model="m", run_id=copy.run_id)
    assert linked.path.name in _git(repo, "worktree", "list", "--porcelain")

    # A new process: no run objects, the same data directory.
    AgentRuns(data_dir=data).list_runs(owner="new-session")

    assert not linked.path.exists()
    assert not loose.path.exists()
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert linked.path.name not in listed, "the linked worktree's metadata was left in the project"
    assert (repo / "app.py").read_bytes() == b"print('tracked')\n"
    assert (plain / "src" / "app.py").is_file()


def test_one_change_too_large_to_review_leaves_the_others_reviewable(tmp_path: Path) -> None:
    """#583: a 61,000-character diff failed the whole changeset, so no change
    in the run could be reviewed or accepted."""
    root = _project(tmp_path)
    work = _copy(tmp_path, root)
    try:
        lines = "".join(f"value_{index} = {index}\n" for index in range(4000))
        (work.path / "generated.py").write_bytes(lines.encode("utf-8"))
        (work.path / "src" / "app.py").write_bytes(b"print('small change')\r\n")

        changes = work.changeset()

        assert set(changes) == {"generated.py", "src/app.py"}
        assert not changes["generated.py"].reviewable
        assert "review limit" in changes["generated.py"].diff
        assert len(changes["generated.py"].diff) < 1000
        assert changes["src/app.py"].reviewable
        with pytest.raises(WorkCopyError) as refused:
            work.accept(["generated.py", "src/app.py"], confirmed=True)
        assert refused.value.code == "diff_too_large"
        assert not (root / "generated.py").exists(), "nothing of a refused batch is written"
        assert work.accept(["src/app.py"], confirmed=True) == ("src/app.py",)
        assert (root / "src" / "app.py").read_bytes() == b"print('small change')\r\n"
    finally:
        work.discard()
