"""Read-only source snapshots are bounded, consented and owner-scoped."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex_backend.services import workspace_context as ws


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("Project header\nHello Cortex\n", encoding="utf-8")
    package = root / "src"
    package.mkdir()
    (package / "app.py").write_text(
        "def main():\n    return 'hello'\n\nprint(main())\n", encoding="utf-8"
    )
    (root / ".env").write_text("PRIVATE_TOKEN=hidden\n", encoding="utf-8")
    (root / "private.key").write_text("PRIVATE_KEY\n", encoding="utf-8")
    (root / "credentials.json").write_text('{"secret":true}', encoding="utf-8")
    (root / "data.bin").write_bytes(b"\x00" * 100)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.py").write_text("ignore me", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("do not index", encoding="utf-8")
    return root


def _capture(registry: ws.WorkspaceSnapshotRegistry, root: Path, owner="owner"):
    return registry.capture(owner=owner, root_path=str(root), confirmed=True)


def test_workspace_requires_explicit_consent_before_touching_source(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    registry = ws.WorkspaceSnapshotRegistry()

    def forbidden(*args):
        raise AssertionError("no source inspection without confirmation")

    monkeypatch.setattr(ws, "_safe_root", forbidden)
    with pytest.raises(ws.WorkspaceAccessError) as error:
        registry.capture(owner="owner", root_path=str(root), confirmed=False)
    assert error.value.code == "consent_required"


def test_snapshot_indexes_only_bounded_regular_text_sources(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / "large.py").write_bytes(b"x" * (ws.MAX_WORKSPACE_FILE_BYTES + 1))
    (root / "invalid.py").write_bytes(b"\xff\xfe\xfa")
    snapshot = _capture(ws.WorkspaceSnapshotRegistry(), root)

    assert list(snapshot.files) == ["README.md", "src/app.py"]
    assert snapshot.file_count == 2
    assert snapshot.total_bytes < 1024
    assert snapshot.skipped_entries > 0
    assert snapshot.truncated is False
    assert all(file.sha256 and len(file.sha256) == 64 for file in snapshot.files.values())


def test_snapshot_cannot_escape_via_file_or_directory_symlink(tmp_path) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET_OUTSIDE\n", encoding="utf-8")
    try:
        (root / "link.py").symlink_to(outside)
        (root / "external").symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this runner cannot create test symlinks: {type(exc).__name__}")
    snapshot = _capture(ws.WorkspaceSnapshotRegistry(), root)
    assert "link.py" not in snapshot.files
    assert not any(path.startswith("external/") for path in snapshot.files)
    assert not any("SECRET_OUTSIDE" in "\n".join(file.lines) for file in snapshot.files.values())


def test_linked_root_and_relative_or_network_root_are_refused(tmp_path) -> None:
    root = _repo(tmp_path)
    registry = ws.WorkspaceSnapshotRegistry()
    for bad in ("relative/project", "\\\\server\\share", ""):
        with pytest.raises(ws.WorkspaceAccessError):
            registry.capture(owner="owner", root_path=bad, confirmed=True)
    linked = tmp_path / "alias"
    try:
        linked.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    with pytest.raises(ws.WorkspaceAccessError) as error:
        registry.capture(owner="owner", root_path=str(linked), confirmed=True)
    assert error.value.code == "linked_root"


def test_user_cannot_read_outside_snapshot_or_use_absolute_paths(tmp_path) -> None:
    registry = ws.WorkspaceSnapshotRegistry()
    snapshot = _capture(registry, _repo(tmp_path))
    malicious = ("../README.md", "/etc/passwd", "src/../README.md", "src\\app.py", "C:/Windows/win.ini", "src//app.py")
    for path in malicious:
        with pytest.raises(ws.WorkspaceAccessError) as error:
            registry.read(owner="owner", snapshot_id=snapshot.snapshot_id, path=path)
        assert error.value.code == "invalid_file"
    with pytest.raises(ws.WorkspaceAccessError) as error:
        registry.read(owner="owner", snapshot_id=snapshot.snapshot_id, path=".env")
    assert error.value.code == "file_unavailable"


def test_search_and_bounded_line_read_are_grounded_to_snapshot(tmp_path) -> None:
    registry = ws.WorkspaceSnapshotRegistry()
    snapshot = _capture(registry, _repo(tmp_path))
    hits = registry.search(owner="owner", snapshot_id=snapshot.snapshot_id, query="HELLO")
    assert {(hit.path, hit.line) for hit in hits} == {("README.md", 2), ("src/app.py", 2)}
    excerpt = registry.read(
        owner="owner", snapshot_id=snapshot.snapshot_id,
        path="src/app.py", start_line=2, max_lines=2,
    )
    assert excerpt.text == "2:     return 'hello'\n3: "
    assert excerpt.total_lines == 4
    assert excerpt.end_line == 3
    assert excerpt.truncated
    assert excerpt.sha256 == snapshot.files["src/app.py"].sha256
    assert registry.list_paths(owner="owner", snapshot_id=snapshot.snapshot_id) == (
        "README.md", "src/app.py",
    )


def test_snapshot_is_immutable_after_original_file_is_changed_or_deleted(tmp_path) -> None:
    root = _repo(tmp_path)
    registry = ws.WorkspaceSnapshotRegistry()
    snapshot = _capture(registry, root)
    (root / "src/app.py").write_text("PRIVATE_NEW_DATA\n", encoding="utf-8")
    (root / "README.md").unlink()
    old = registry.read(owner="owner", snapshot_id=snapshot.snapshot_id, path="src/app.py")
    assert "return 'hello'" in old.text
    assert "PRIVATE_NEW_DATA" not in old.text
    assert registry.read(
        owner="owner", snapshot_id=snapshot.snapshot_id, path="README.md"
    ).text.startswith("1: Project header")


def test_snapshot_is_owner_scoped_revocable_and_expires(tmp_path) -> None:
    ticks = [100.0]
    registry = ws.WorkspaceSnapshotRegistry(clock=lambda: ticks[0])
    snapshot = _capture(registry, _repo(tmp_path), owner="owner-one")
    with pytest.raises(ws.WorkspaceAccessError) as error:
        registry.read(owner="owner-two", snapshot_id=snapshot.snapshot_id, path="README.md")
    assert error.value.code == "unavailable"
    ticks[0] += ws.WORKSPACE_TTL_SECONDS
    with pytest.raises(ws.WorkspaceAccessError) as error:
        registry.get(owner="owner-one", snapshot_id=snapshot.snapshot_id)
    assert error.value.code == "unavailable"

    snapshot = _capture(registry, _repo(tmp_path), owner="owner-one")
    registry.revoke(owner="owner-one", snapshot_id=snapshot.snapshot_id)
    with pytest.raises(ws.WorkspaceAccessError):
        registry.get(owner="owner-one", snapshot_id=snapshot.snapshot_id)


def test_owner_lease_limit_and_close_are_fail_closed(tmp_path) -> None:
    registry = ws.WorkspaceSnapshotRegistry()
    root = _repo(tmp_path)
    first = _capture(registry, root)
    second = _capture(registry, root)
    with pytest.raises(ws.WorkspaceAccessError) as error:
        _capture(registry, root)
    assert error.value.code == "owner_limit"
    registry.revoke(owner="owner", snapshot_id=first.snapshot_id)
    third = _capture(registry, root)
    assert third.snapshot_id != second.snapshot_id
    registry.close()
    with pytest.raises(ws.WorkspaceAccessError):
        registry.get(owner="owner", snapshot_id=second.snapshot_id)


def test_indexing_is_bounded_and_reports_truncation(tmp_path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(ws, "MAX_SCANNED_ENTRIES", 4)
    snapshot = _capture(ws.WorkspaceSnapshotRegistry(), root)
    assert snapshot.truncated
    assert snapshot.scanned_entries <= 4


def test_read_and_search_validate_limits_without_access(tmp_path) -> None:
    registry = ws.WorkspaceSnapshotRegistry()
    snapshot = _capture(registry, _repo(tmp_path))
    for limit in (0, -1, True, 1000):
        with pytest.raises(ws.WorkspaceAccessError):
            registry.list_paths(owner="owner", snapshot_id=snapshot.snapshot_id, limit=limit)
        with pytest.raises(ws.WorkspaceAccessError):
            registry.search(owner="owner", snapshot_id=snapshot.snapshot_id, query="main", limit=limit)
    for start, count in ((0, 2), (1, 0), (True, 3), (1, 121), (10**6, 1)):
        with pytest.raises(ws.WorkspaceAccessError):
            registry.read(
                owner="owner", snapshot_id=snapshot.snapshot_id,
                path="src/app.py", start_line=start, max_lines=count,
            )
    for text in ("", "x", "main\nDROP", "x" * 121):
        with pytest.raises(ws.WorkspaceAccessError):
            registry.search(owner="owner", snapshot_id=snapshot.snapshot_id, query=text)


def test_permission_errors_never_echo_a_private_host_path(tmp_path) -> None:
    private = str(tmp_path / "sensitive-repository-name")
    with pytest.raises(ws.WorkspaceAccessError) as error:
        ws.WorkspaceSnapshotRegistry().capture(
            owner="owner", root_path=private, confirmed=True
        )
    assert private not in str(error.value)
    assert error.value.code == "invalid_root"


def test_control_char_or_bidi_filename_cannot_forge_a_diff_path(tmp_path) -> None:
    root = _repo(tmp_path)
    weird = ("fake\n+++ b/forged.py", "other\u202ename.py", "bad:name.py", "slash\\evil.py")
    for index, name in enumerate(weird):
        try:
            (root / name).write_text(f"secret marker {index}\n", encoding="utf-8")
        except (OSError, ValueError):
            continue  # Some names are not expressible on Windows.
    snapshot = _capture(ws.WorkspaceSnapshotRegistry(), root)
    assert not any("fake" in path or "other" in path or "bad:name" in path or "slash" in path for path in snapshot.files)
    assert all("secret marker" not in "\n".join(file.lines) for file in snapshot.files.values())
