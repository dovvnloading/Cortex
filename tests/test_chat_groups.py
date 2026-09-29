"""Chat groups (folders/projects): persistence, migration, and the HTTP surface.

The load-bearing guarantee throughout is that a group is only ever *filing*:
deleting one, or losing one to a corrupted link, must never take conversations
with it.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from cortex_backend.repositories.chats import (
    ChatGroupNotFound,
    InMemoryChatRepository,
    LegacyDatabaseChatRepository,
)
from cortex_backend.repositories import storage
from cortex_backend.repositories.storage import DatabaseManager, PersistenceError


def _repositories(tmp_path: Path):
    """Both implementations, so the in-memory double used by API tests cannot
    silently drift from the SQLite one users actually run."""
    database = DatabaseManager(db_path=str(tmp_path / "chats.sqlite"))
    return [InMemoryChatRepository(), LegacyDatabaseChatRepository(database)]


# -- repository parity -----------------------------------------------------


def test_group_lifecycle_matches_across_repositories(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_chat("t1", "Alpha")
        repository.create_chat("t2", "Beta")
        repository.create_group("g1", "Research")
        repository.create_group("g2", "Work")

        groups = repository.list_groups()
        assert [group["name"] for group in groups] == ["Research", "Work"]
        assert [group["collapsed"] for group in groups] == [False, False]

        assert repository.set_chat_group("t1", "g1") is True
        summaries = {item["id"]: item["group_id"] for item in repository.list_summaries()}
        assert summaries == {"t1": "g1", "t2": None}

        # Ungrouping is an explicit null, not a missing field.
        assert repository.set_chat_group("t1", None) is True
        assert all(item["group_id"] is None for item in repository.list_summaries())


def test_rename_and_collapse_share_one_update_path(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_group("g1", "Research")

        assert repository.update_group("g1", name="Deep Research") is True
        assert repository.list_groups()[0]["name"] == "Deep Research"
        assert repository.list_groups()[0]["collapsed"] is False

        assert repository.update_group("g1", collapsed=True) is True
        group = repository.list_groups()[0]
        assert group["collapsed"] is True
        assert group["name"] == "Deep Research"  # untouched by a collapse

        assert repository.update_group("missing", name="x") is False


def test_deleting_a_group_keeps_its_chats(tmp_path: Path) -> None:
    """The whole point: a group is filing, not a container that owns chats."""
    for repository in _repositories(tmp_path):
        repository.create_chat("t1", "Alpha")
        repository.create_group("g1", "Research")
        repository.set_chat_group("t1", "g1")

        repository.delete_group("g1")

        summaries = repository.list_summaries()
        assert [item["id"] for item in summaries] == ["t1"]
        assert summaries[0]["group_id"] is None
        assert repository.list_groups() == []
        assert repository.get_chat("t1") is not None


def test_moving_a_chat_into_an_unknown_group_is_rejected(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_chat("t1", "Alpha")
        with pytest.raises(ChatGroupNotFound):
            repository.set_chat_group("t1", "ghost")
        assert repository.list_summaries()[0]["group_id"] is None


def test_moving_an_unknown_chat_reports_miss_without_raising(tmp_path: Path) -> None:
    for repository in _repositories(tmp_path):
        repository.create_group("g1", "Research")
        assert repository.set_chat_group("missing-thread", "g1") is False


# -- schema migration ------------------------------------------------------


def _write_old_schema_database(path: Path, *, user_version: int = 3) -> None:
    """A database as an earlier build left it: one chat, no groups column.

    Shaped the way that version had it, because the upgrade trusts the stored
    version: generation_stats_json arrived in version 3, so a file stamped
    older -- including 0, a file that predates versioning -- does not have it.
    """
    stats = ", generation_stats_json TEXT" if user_version >= 3 else ""
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL, timestamp TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, "
        "role TEXT NOT NULL, content TEXT NOT NULL, sources TEXT, thoughts TEXT, attachments TEXT, "
        f"timestamp TEXT NOT NULL{stats})"
    )
    connection.execute(
        "INSERT INTO threads VALUES ('old-1', 'Existing chat', '2026-01-01T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO messages (thread_id, role, content, timestamp) "
        "VALUES ('old-1', 'user', 'hello', '2026-01-01T00:00:00Z')"
    )
    connection.execute(f"PRAGMA user_version = {user_version}")
    connection.commit()
    connection.close()


def _user_version(path: str | Path) -> int:
    probe = sqlite3.connect(path)
    try:
        return int(probe.execute("PRAGMA user_version").fetchone()[0])
    finally:
        probe.close()


def _column_names(path: str | Path, table: str) -> set[str]:
    probe = sqlite3.connect(path)
    try:
        return {row[1] for row in probe.execute(f"PRAGMA table_info({table})")}
    finally:
        probe.close()


def test_a_v3_database_upgrades_in_place_without_losing_chats(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)

    database = DatabaseManager(db_path=str(path))

    summaries = database.get_all_chats_summary()
    assert [item["id"] for item in summaries] == ["old-1"]
    assert summaries[0]["group_id"] is None
    assert len(database.load_chat("old-1")["messages"]) == 1
    assert database.list_groups() == []

    probe = sqlite3.connect(path)
    try:
        assert probe.execute("PRAGMA user_version").fetchone()[0] == DatabaseManager.SCHEMA_VERSION
    finally:
        probe.close()


def test_upgrading_a_v3_database_keeps_a_pre_upgrade_snapshot(tmp_path: Path) -> None:
    """The ordinary backup is refreshed after the schema upgrade, so an older
    release would refuse it. The snapshot taken first is the rollback path."""
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)

    database = DatabaseManager(db_path=str(path))

    snapshot = Path(f"{path}.pre-v3.bak")
    assert database.pre_upgrade_snapshot_path == str(snapshot)
    assert database.backup_status == ("ok", None)
    assert snapshot.exists()
    # The snapshot is the database as it was: old version, old shape, same chat.
    assert _user_version(snapshot) == 3
    assert "group_id" not in _column_names(snapshot, "threads")
    probe = sqlite3.connect(snapshot)
    try:
        assert probe.execute("SELECT content FROM messages").fetchall() == [("hello",)]
    finally:
        probe.close()
    # ... while the live database and its ordinary backup are on the new version.
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert _user_version(f"{path}.bak") == DatabaseManager.SCHEMA_VERSION

    # A second open sees a current database and leaves the snapshot alone.
    snapshot_before = snapshot.read_bytes()
    modified_before = snapshot.stat().st_mtime_ns
    reopened = DatabaseManager(db_path=str(path))
    assert reopened.pre_upgrade_snapshot_path is None
    assert snapshot.read_bytes() == snapshot_before
    assert snapshot.stat().st_mtime_ns == modified_before


def _chat_ids(path: str | Path) -> set[str]:
    probe = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {row[0] for row in probe.execute("SELECT id FROM threads")}
    finally:
        probe.close()


def test_an_upgrade_that_fails_part_way_keeps_its_snapshot_and_the_retry_reuses_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed step rolls back, so the database is unchanged and the snapshot
    from the first attempt still describes it. The retry must keep that one and
    not pile a second full copy beside it."""
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    real_step = storage._MIGRATIONS[4]

    def failing_step(connection: sqlite3.Connection) -> None:
        real_step(connection)
        raise sqlite3.OperationalError("simulated failure after the step's own changes")

    monkeypatch.setitem(storage._MIGRATIONS, 4, failing_step)
    with pytest.raises(PersistenceError):
        DatabaseManager(db_path=str(path))
    monkeypatch.undo()

    snapshot = Path(f"{path}.pre-v3.bak")
    first_attempt = snapshot.read_bytes()
    modified_before = snapshot.stat().st_mtime_ns
    assert _user_version(path) == 3

    database = DatabaseManager(db_path=str(path))

    assert database.pre_upgrade_snapshot_path == str(snapshot)
    assert snapshot.read_bytes() == first_attempt
    assert snapshot.stat().st_mtime_ns == modified_before
    assert not list(tmp_path.glob("*.superseded-*"))
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION


def _in_write_ahead_mode(path: Path) -> None:
    """Every install that ran a release from before the ladder is already in WAL
    mode, and so is a snapshot taken of it: the mode is part of the file header."""
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    finally:
        connection.close()


def _sidecars_beside(path: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in path.parent.iterdir()
        if entry.name.startswith(f"{path.name}-") and entry.name.endswith(("-wal", "-shm"))
    )


def _roll_back_and_chat_on_the_older_release(tmp_path: Path, path: Path, snapshot: Path) -> None:
    """The documented rollback: move the database aside, copy the snapshot over it,
    and let the older release write one more chat."""
    aside = tmp_path / "moved-aside"
    aside.mkdir()
    for suffix in ("", "-wal", "-shm"):
        leftover = Path(f"{path}{suffix}")
        if leftover.exists():
            shutil.move(str(leftover), str(aside / leftover.name))
    shutil.copy2(snapshot, path)
    older_release = sqlite3.connect(path)
    try:
        older_release.execute(
            "INSERT INTO threads (id, title, timestamp) "
            "VALUES ('after-rollback', 'Written on the older release', '2026-02-01T00:00:00Z')"
        )
        older_release.commit()
    finally:
        older_release.close()
    assert _user_version(path) == 3


def test_a_second_upgrade_after_a_rollback_takes_a_fresh_snapshot_and_keeps_the_first(
    tmp_path: Path,
) -> None:
    """The documented rollback copies the snapshot over the database and runs
    the older release. Whatever that release then writes is in no snapshot, so
    the next upgrade must not leave the old one standing as the file the README
    tells people to restore: that would drop those chats on the next rollback."""
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    DatabaseManager(db_path=str(path))  # first upgrade; the snapshot holds only 'old-1'
    snapshot = Path(f"{path}.pre-v3.bak")
    first_snapshot = snapshot.read_bytes()

    _roll_back_and_chat_on_the_older_release(tmp_path, path, snapshot)

    database = DatabaseManager(db_path=str(path))  # the second upgrade

    assert database.pre_upgrade_snapshot_path == str(snapshot)
    assert _user_version(snapshot) == 3
    assert _chat_ids(snapshot) == {"old-1", "after-rollback"}
    superseded = Path(f"{snapshot}.superseded-1")
    assert superseded.read_bytes() == first_snapshot
    assert _chat_ids(superseded) == {"old-1"}
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert _chat_ids(path) == {"old-1", "after-rollback"}


def test_reusing_a_pre_upgrade_snapshot_leaves_no_sidecars_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deciding whether the snapshot still matches means reading it. Reading a
    file whose header says write-ahead logging made SQLite create a -wal and a
    -shm beside it, and nothing ever removed them."""
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    _in_write_ahead_mode(path)
    real_step = storage._MIGRATIONS[4]

    def failing_step(connection: sqlite3.Connection) -> None:
        real_step(connection)
        raise sqlite3.OperationalError("simulated failure after the step's own changes")

    monkeypatch.setitem(storage._MIGRATIONS, 4, failing_step)
    with pytest.raises(PersistenceError):
        DatabaseManager(db_path=str(path))
    monkeypatch.undo()
    snapshot = Path(f"{path}.pre-v3.bak")
    first_attempt = snapshot.read_bytes()
    assert first_attempt[18:20] == b"", "the snapshot is flagged as write-ahead-log mode"

    database = DatabaseManager(db_path=str(path))  # the retry reads the snapshot

    assert database.pre_upgrade_snapshot_path == str(snapshot)
    assert snapshot.read_bytes() == first_attempt
    assert _sidecars_beside(snapshot) == []


def test_a_superseded_snapshot_leaves_no_stale_sidecars_beside_the_new_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    _in_write_ahead_mode(path)
    DatabaseManager(db_path=str(path))  # first upgrade
    snapshot = Path(f"{path}.pre-v3.bak")
    _roll_back_and_chat_on_the_older_release(tmp_path, path, snapshot)

    DatabaseManager(db_path=str(path))  # the second upgrade supersedes the snapshot

    superseded = Path(f"{snapshot}.superseded-1")
    assert superseded.exists()
    assert _sidecars_beside(snapshot) == []
    assert _sidecars_beside(superseded) == []
    assert _chat_ids(snapshot) == {"old-1", "after-rollback"}


def test_a_snapshot_that_is_not_a_database_is_kept_aside_never_trusted_or_overwritten(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    snapshot = Path(f"{path}.pre-v3.bak")
    snapshot.write_bytes(b"not a database")
    Path(f"{snapshot}.superseded-1").write_bytes(b"kept from an earlier time")

    database = DatabaseManager(db_path=str(path))

    assert database.pre_upgrade_snapshot_path == str(snapshot)
    assert _user_version(snapshot) == 3
    assert Path(f"{snapshot}.superseded-1").read_bytes() == b"kept from an earlier time"
    assert Path(f"{snapshot}.superseded-2").read_bytes() == b"not a database"


def test_a_pre_versioning_database_with_history_is_snapshotted(tmp_path: Path) -> None:
    """user_version 0 with tables is real history from a build that predates
    versioning, and it is about to be altered like any other."""
    path = tmp_path / "unversioned.sqlite"
    _write_old_schema_database(path, user_version=0)

    database = DatabaseManager(db_path=str(path))

    snapshot = Path(f"{path}.pre-v0.bak")
    assert database.pre_upgrade_snapshot_path == str(snapshot)
    assert _user_version(snapshot) == 0
    assert "group_id" not in _column_names(snapshot, "threads")
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION


def test_a_new_or_current_database_gets_no_pre_upgrade_snapshot(tmp_path: Path) -> None:
    fresh = DatabaseManager(db_path=str(tmp_path / "fresh.sqlite"))
    assert fresh.pre_upgrade_snapshot_path is None
    DatabaseManager(db_path=str(tmp_path / "fresh.sqlite"))

    assert not list(tmp_path.glob("*.pre-v*.bak"))


def _snapshot_hits_a_full_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    def full(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(storage, "snapshot_database", full)


def _snapshot_source_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "snapshot_database", locked)


def _snapshot_comes_out_torn(monkeypatch: pytest.MonkeyPatch) -> None:
    def torn(_source, destination, **_kwargs):
        Path(destination).write_bytes(b"torn snapshot")

    monkeypatch.setattr(storage, "snapshot_database", torn)


def _snapshot_name_is_held_open(monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace

    def replace(source, destination, *args, **kwargs):
        if str(destination).endswith(".pre-v3.bak"):
            raise PermissionError(13, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


@pytest.mark.parametrize(
    "inject",
    [
        _snapshot_hits_a_full_disk,
        _snapshot_source_is_locked,
        _snapshot_comes_out_torn,
        _snapshot_name_is_held_open,
    ],
    ids=["disk-full", "source-locked", "snapshot-corrupt", "name-held-open"],
)
def test_a_pre_upgrade_snapshot_that_cannot_be_kept_refuses_the_upgrade_and_the_next_launch_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inject
) -> None:
    """Carrying on would finish the upgrade in this same launch, and nothing
    would ever retry the snapshot: the rollback point would simply not exist.
    The database is left exactly as it was, and the next launch tries again."""
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    inject(monkeypatch)

    with pytest.raises(PersistenceError, match="not upgraded") as refused:
        DatabaseManager(db_path=str(path))
    monkeypatch.undo()

    assert str(tmp_path) not in str(refused.value)
    assert _user_version(path) == 3
    assert "group_id" not in _column_names(path, "threads")
    assert not Path(f"{path}.pre-v3.bak").exists()
    assert not [entry.name for entry in tmp_path.iterdir() if ".tmp" in entry.name]
    assert _chat_ids(path) == {"old-1"}

    database = DatabaseManager(db_path=str(path))

    assert database.pre_upgrade_snapshot_path == f"{path}.pre-v3.bak"
    assert _user_version(f"{path}.pre-v3.bak") == 3
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert _chat_ids(path) == {"old-1"}


def test_a_newer_database_is_refused_untouched_and_names_the_snapshot_to_restore(
    tmp_path: Path,
) -> None:
    """An older release used to create indexes in a newer file and only then
    refuse it. It now refuses first, and says which file goes back."""
    path = tmp_path / "newer.sqlite"
    _write_old_schema_database(path, user_version=99)
    # What the newer release kept when it upgraded from versions 3 and 4, plus
    # one from the future that this release could not read anyway.
    for version in (3, 4, 7):
        Path(f"{path}.pre-v{version}.bak").write_bytes(b"snapshot")
    before = path.read_bytes()

    with pytest.raises(PersistenceError, match=r"schema version 99") as refused:
        DatabaseManager(db_path=str(path))

    assert "newer.sqlite.pre-v4.bak" in str(refused.value)
    # Restoring replaces the database, so the message says where to put it first.
    assert "move the current database" in str(refused.value)
    assert "written since the upgrade" in str(refused.value)
    assert "pre-v7" not in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    assert path.read_bytes() == before


def test_a_newer_database_with_no_snapshot_is_still_refused_with_guidance(tmp_path: Path) -> None:
    path = tmp_path / "newer.sqlite"
    _write_old_schema_database(path, user_version=99)

    with pytest.raises(PersistenceError, match=r"schema version 99.*release that wrote it"):
        DatabaseManager(db_path=str(path))


def _schema_names(path: str | Path) -> set[tuple[str, str]]:
    probe = sqlite3.connect(path)
    try:
        return {
            (row[0], row[1])
            for row in probe.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        probe.close()


def test_the_ladder_has_a_step_for_every_version_and_none_beyond(tmp_path: Path) -> None:
    assert sorted(storage._MIGRATIONS) == list(range(1, DatabaseManager.SCHEMA_VERSION + 1))


def test_a_failing_migration_step_leaves_the_version_and_tables_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.sqlite"
    _write_old_schema_database(path)
    real_step = storage._MIGRATIONS[4]

    def failing_step(connection: sqlite3.Connection) -> None:
        real_step(connection)  # creates chat_groups and adds threads.group_id ...
        connection.execute("PRAGMA user_version = 4")  # ... even stamps the version ...
        raise sqlite3.OperationalError("simulated failure")  # ... and then fails

    monkeypatch.setitem(storage._MIGRATIONS, 4, failing_step)
    with pytest.raises(PersistenceError, match="previous version") as failed:
        DatabaseManager(db_path=str(path))
    monkeypatch.undo()

    assert isinstance(failed.value.cause, sqlite3.OperationalError)
    assert _user_version(path) == 3
    assert "group_id" not in _column_names(path, "threads")
    assert ("table", "chat_groups") not in _schema_names(path)
    assert ("index", "idx_threads_group") not in _schema_names(path)
    assert _chat_ids(path) == {"old-1"}

    # Nothing was half-applied, so the next launch upgrades from where it was.
    database = DatabaseManager(db_path=str(path))
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert database.list_groups() == []
    assert _chat_ids(path) == {"old-1"}


def test_every_step_commits_with_its_own_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash part-way through a long upgrade used to leave a file altered by
    some steps and still stamped with the old version. Each step now carries its
    own stamp, so the file always says exactly how far it got."""
    path = tmp_path / "unversioned.sqlite"
    _write_old_schema_database(path, user_version=0)

    def failing_step(connection: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("simulated failure")

    monkeypatch.setitem(storage._MIGRATIONS, 3, failing_step)
    with pytest.raises(PersistenceError):
        DatabaseManager(db_path=str(path))
    monkeypatch.undo()

    assert _user_version(path) == 2
    assert "generation_stats_json" not in _column_names(path, "messages")

    DatabaseManager(db_path=str(path))
    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert "generation_stats_json" in _column_names(path, "messages")


def test_a_pre_versioning_database_with_no_user_version_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.sqlite"
    _write_old_schema_database(path, user_version=0)
    assert _user_version(path) == 0

    database = DatabaseManager(db_path=str(path))

    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert {"attachments", "generation_stats_json"} <= _column_names(path, "messages")
    assert "group_id" in _column_names(path, "threads")
    assert [item["id"] for item in database.get_all_chats_summary()] == ["old-1"]
    assert len(database.load_chat("old-1")["messages"]) == 1


def test_a_database_that_already_has_part_of_a_step_still_upgrades(tmp_path: Path) -> None:
    """An older build applied each ALTER on its own, so a crash could leave the
    column without the stamp. Steps are idempotent for exactly this file."""
    path = tmp_path / "half-upgraded.sqlite"
    _write_old_schema_database(path, user_version=2)
    probe = sqlite3.connect(path)
    try:
        probe.execute("ALTER TABLE messages ADD COLUMN generation_stats_json TEXT")
        probe.execute("ALTER TABLE threads ADD COLUMN group_id TEXT")
        probe.commit()
    finally:
        probe.close()

    database = DatabaseManager(db_path=str(path))

    assert _user_version(path) == DatabaseManager.SCHEMA_VERSION
    assert database.list_groups() == []
    assert _chat_ids(path) == {"old-1"}


def test_a_new_database_ends_up_with_the_same_shape_as_an_upgraded_one(tmp_path: Path) -> None:
    """A fresh install climbs the same ladder, so the two cannot drift apart."""
    DatabaseManager(db_path=str(tmp_path / "fresh.sqlite"))
    old = tmp_path / "old.sqlite"
    _write_old_schema_database(old, user_version=0)
    DatabaseManager(db_path=str(old))

    assert _schema_names(tmp_path / "fresh.sqlite") == _schema_names(old)
    for table in ("threads", "messages", "chat_groups"):
        assert _column_names(tmp_path / "fresh.sqlite", table) == _column_names(old, table)


def test_a_chat_pointing_at_a_vanished_group_is_returned_to_ungrouped(tmp_path: Path) -> None:
    """There is no FOREIGN KEY on threads.group_id (SQLite cannot add one via
    ALTER TABLE), so a chat could outlive its group after an interrupted
    delete. Such a chat would be filed under a group the sidebar never renders
    and would look deleted -- startup must repair it."""
    path = tmp_path / "chats.sqlite"
    database = DatabaseManager(db_path=str(path))
    database.create_chat("t1", "Alpha")
    database.create_group("g1", "Research")
    database.set_chat_group("t1", "g1")

    corrupt = sqlite3.connect(path)
    try:
        corrupt.execute("DELETE FROM chat_groups WHERE id = 'g1'")
        corrupt.commit()
    finally:
        corrupt.close()

    repaired = DatabaseManager(db_path=str(path))

    assert repaired.get_all_chats_summary()[0]["group_id"] is None
    assert len(repaired.load_chat("t1")["messages"]) == 0
    assert repaired.get_all_chats_summary()[0]["id"] == "t1"


# -- HTTP surface ----------------------------------------------------------


def test_group_routes_round_trip_over_http(client, headers) -> None:
    assert client.get("/api/v1/chat-groups", headers=headers).json() == []

    created = client.post(
        "/api/v1/chat-groups", json={"name": "Research"}, headers=headers
    )
    assert created.status_code == 201
    group = created.json()
    assert group["name"] == "Research"
    assert group["collapsed"] is False
    assert group["position"] == 0

    chat = client.post(
        "/api/v1/chats", json={"title": "Alpha"}, headers=headers
    ).json()

    moved = client.patch(
        f"/api/v1/chats/{chat['id']}/group",
        json={"group_id": group["id"]},
        headers=headers,
    )
    assert moved.status_code == 200
    assert moved.json()["group_id"] == group["id"]

    collapsed = client.patch(
        f"/api/v1/chat-groups/{group['id']}",
        json={"collapsed": True},
        headers=headers,
    )
    assert collapsed.status_code == 200
    assert collapsed.json()["collapsed"] is True
    assert collapsed.json()["name"] == "Research"

    # Deleting the group must leave the chat, now ungrouped.
    assert client.delete(
        f"/api/v1/chat-groups/{group['id']}", headers=headers
    ).status_code == 204
    assert client.get("/api/v1/chat-groups", headers=headers).json() == []
    summaries = client.get("/api/v1/chats", headers=headers).json()
    assert [item["id"] for item in summaries] == [chat["id"]]
    assert summaries[0]["group_id"] is None


def test_group_routes_report_missing_targets_as_404(client, headers) -> None:
    chat = client.post(
        "/api/v1/chats", json={"title": "Alpha"}, headers=headers
    ).json()

    assert client.patch(
        "/api/v1/chat-groups/ghost", json={"name": "x"}, headers=headers
    ).status_code == 404
    assert client.patch(
        f"/api/v1/chats/{chat['id']}/group",
        json={"group_id": "ghost"},
        headers=headers,
    ).status_code == 404

    group = client.post(
        "/api/v1/chat-groups", json={"name": "Research"}, headers=headers
    ).json()
    assert client.patch(
        "/api/v1/chats/ghost-thread/group",
        json={"group_id": group["id"]},
        headers=headers,
    ).status_code == 404


def test_group_routes_require_a_session(client) -> None:
    assert client.get("/api/v1/chat-groups").status_code == 401
    assert client.post("/api/v1/chat-groups", json={"name": "x"}).status_code == 401


def test_group_names_are_bounded_and_non_empty(client, headers) -> None:
    assert client.post(
        "/api/v1/chat-groups", json={"name": "   "}, headers=headers
    ).status_code == 422
    assert client.post(
        "/api/v1/chat-groups", json={"name": ""}, headers=headers
    ).status_code == 422
    assert client.post(
        "/api/v1/chat-groups", json={"name": "x" * 121}, headers=headers
    ).status_code == 422


def test_chat_and_message_text_is_trimmed_and_rejects_invisible_input(client, headers) -> None:
    chat = client.post(
        "/api/v1/chats", json={"title": "  Project  "}, headers=headers
    )
    assert chat.status_code == 201
    chat_payload = chat.json()
    assert chat_payload["title"] == "Project"

    thread_id = chat_payload["id"]
    message = client.post(
        f"/api/v1/chats/{thread_id}/messages",
        json={"role": "user", "content": " hello "},
        headers=headers,
    )
    assert message.status_code == 200
    assert message.json()["messages"][-1]["content"] == "hello"
    assert client.patch(
        f"/api/v1/chats/{thread_id}",
        json={"title": "\t\n"},
        headers=headers,
    ).status_code == 422
    assert client.post(
        "/api/v1/chat-groups", json={"name": "\u200b"}, headers=headers
    ).status_code == 422
    assert client.post(
        f"/api/v1/chats/{thread_id}/messages",
        json={"role": "user", "content": " \n\t "},
        headers=headers,
    ).status_code == 422
