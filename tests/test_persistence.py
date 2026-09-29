"""Persistence, migration, and recovery tests for local Cortex data."""

import errno
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from sqlite_faults import disk_full_when
import cortex_backend.repositories.storage as storage
from cortex_backend.repositories.storage import (
    DatabaseManager,
    PermanentMemoryManager,
    PersistenceError,
)


class PersistenceTests(unittest.TestCase):
    def test_persistence_logs_omit_private_paths_and_chat_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            private_db_path = Path(directory) / "Alice private records.sqlite"
            private_title = "Alice's confidential launch plan"
            with self.assertLogs(level="INFO") as captured:
                manager = DatabaseManager(db_path=str(private_db_path))
                manager.create_chat("private-thread-id", "Untitled")
                manager.update_chat_title("private-thread-id", private_title)

            rendered_logs = "\n".join(captured.output)
            self.assertNotIn(str(private_db_path), rendered_logs)
            self.assertNotIn(private_title, rendered_logs)
            self.assertIn("private title omitted", rendered_logs)

    def test_chat_attachment_metadata_survives_sqlite_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))
            attachment = {
                "attachment_id": "doc-1",
                "filename": "notes.md",
                "mime_type": "text/markdown",
                "size": 12,
                "sha256": "a" * 64,
                "kind": "document",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
            manager.add_message(
                "thread-attachments",
                "user",
                "Please review the attached file(s).",
                attachments=[attachment],
                thread_title="Attachments",
            )

            loaded = manager.load_chat("thread-attachments")
            self.assertEqual(loaded["messages"][0]["attachments"], [attachment])

    def test_database_operations_are_safe_across_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DatabaseManager(
                db_path=str(Path(directory) / "chats.sqlite"),
                legacy_history_dir=str(Path(directory) / "legacy"),
            )
            errors = []

            def write_chat(index):
                try:
                    manager.add_message(
                        f"thread-{index}",
                        "user",
                        f"hello {index}",
                        thread_title=f"Chat {index}",
                    )
                except Exception as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            threads = [threading.Thread(target=write_chat, args=(index,)) for index in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "a writer thread did not finish")

            self.assertEqual(errors, [])
            self.assertEqual(len(manager.get_all_chats_summary()), 6)

    def test_fork_transaction_rolls_back_on_invalid_message(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DatabaseManager(db_path=str(Path(directory) / "chats.sqlite"))

            with self.assertRaises(PersistenceError):
                manager.create_chat_from_messages(
                    "broken",
                    "Broken",
                    [
                        {"role": "user", "content": "valid"},
                        {"role": "assistant", "content": None},
                    ],
                )

            self.assertIsNone(manager.load_chat("broken"))

    def test_migration_migrates_skips_and_quarantines_per_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "valid.json").write_text(
                json.dumps({
                    "id": "valid",
                    "title": "Valid",
                    "timestamp": "2026-01-01T00:00:00",
                    "messages": [{"role": "user", "content": "hello"}],
                }),
                encoding="utf-8",
            )
            (legacy / "duplicate.json").write_text(
                json.dumps({"id": "duplicate", "messages": []}), encoding="utf-8"
            )
            (legacy / "malformed.json").write_text("{not json", encoding="utf-8")

            manager = DatabaseManager(
                db_path=str(root / "chats.sqlite"),
                legacy_history_dir=str(legacy),
            )
            manager.create_chat("duplicate", "Already present")
            result = manager.migrate_from_json_if_needed()

            self.assertEqual((result.migrated, result.skipped, result.quarantined), (1, 1, 1))
            self.assertIsNotNone(manager.load_chat("valid"))
            self.assertTrue((legacy / "quarantine" / "malformed.json").exists())
            self.assertTrue(list(root.glob("legacy_migrated_*/*.json")))

    def test_migration_quarantines_out_of_contract_chat_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            records = {
                "invalid-role.json": {"id": "invalid-role", "messages": [{"role": "tool", "content": "x"}]},
                "oversized-content.json": {
                    "id": "oversized-content",
                    "messages": [{"role": "user", "content": "x" * (storage.MAX_LEGACY_MESSAGE_CONTENT_CHARS + 1)}],
                },
                "too-many-messages.json": {
                    "id": "too-many-messages",
                    "messages": [
                        {"role": "user", "content": "x"}
                    ] * (storage.MAX_LEGACY_CHAT_MESSAGES + 1),
                },
            }
            for filename, record in records.items():
                (legacy / filename).write_text(json.dumps(record), encoding="utf-8")

            manager = DatabaseManager(
                db_path=str(root / "chats.sqlite"),
                legacy_history_dir=str(legacy),
            )
            result = manager.migrate_from_json_if_needed()

            self.assertEqual(result, storage.MigrationResult(quarantined=3))
            self.assertEqual(manager.get_all_chats_summary(), [])
            self.assertEqual(
                {path.name for path in (legacy / "quarantine").iterdir()},
                set(records),
            )

    def test_migration_rejects_oversized_source_file_before_json_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "oversized.json").write_text("{}" + (" " * 64), encoding="utf-8")
            (legacy / "valid.json").write_text(json.dumps({"id": "valid"}), encoding="utf-8")

            manager = DatabaseManager(
                db_path=str(root / "chats.sqlite"),
                legacy_history_dir=str(legacy),
            )
            with patch.object(storage, "MAX_LEGACY_CHAT_FILE_BYTES", 32):
                result = manager.migrate_from_json_if_needed()

            self.assertEqual(result, storage.MigrationResult(migrated=1, quarantined=1))
            self.assertTrue((legacy / "quarantine" / "oversized.json").exists())
            self.assertIsNotNone(manager.load_chat("valid"))

    def test_a_corrupt_memory_file_with_no_backup_can_still_be_repaired(self):
        """A damaged file must not make the memory panel permanently dead.

        The save path refuses to rotate a corrupt primary over a good backup,
        which is right. When there was no valid backup either it refused the
        whole save -- so every write failed, including Clear, which would have
        replaced the damaged file outright. The only repair was deleting the
        file by hand from the data directory.
        """
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "permanent_memory.json"
            memory_path.write_text('{"memos": ["remember tea"]}', encoding="utf-8")
            PermanentMemoryManager(memory_file_path=str(memory_path))
            # A truncated write: unreadable, and no backup was ever rotated.
            memory_path.write_text('{"memos": ["remember te', encoding="utf-8")
            self.assertFalse(Path(f"{memory_path}.bak").exists())

            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("a fresh memory")

            self.assertEqual(manager.get_memos(), ["a fresh memory"])
            reopened = PermanentMemoryManager(memory_file_path=str(memory_path))
            self.assertEqual(reopened.get_memos(), ["a fresh memory"])
            # The damaged bytes are set aside, not destroyed.
            self.assertEqual(
                Path(f"{memory_path}.corrupt").read_text(encoding="utf-8"),
                '{"memos": ["remember te',
            )

    def test_a_corrupt_memory_file_still_recovers_from_a_good_backup(self):
        """The protection this guard exists for must survive the repair path."""
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "permanent_memory.json"
            backup_path = Path(f"{memory_path}.bak")
            good = '{"memos": ["remember tea"]}'
            memory_path.write_text(good, encoding="utf-8")
            backup_path.write_text(good, encoding="utf-8")
            memory_path.write_text('{"memos": ["remember te', encoding="utf-8")

            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            self.assertEqual(manager.get_memos(), ["remember tea"])

            manager.add_memo("second memory")

            self.assertEqual(manager.get_memos(), ["remember tea", "second memory"])
            # Recovered from the backup rather than set aside, so nothing was
            # quarantined and the backup was never overwritten by corruption.
            self.assertFalse(Path(f"{memory_path}.corrupt").exists())
            self.assertIn("remember tea", backup_path.read_text(encoding="utf-8"))

    def test_permanent_memory_add_memo_is_safe_across_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            errors = []

            def add_memo(index):
                try:
                    manager.add_memo(f"memo {index}")
                except Exception as exc:  # pragma: no cover - assertion below reports it
                    errors.append(exc)

            threads = [threading.Thread(target=add_memo, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), "a writer thread did not finish")

            self.assertEqual(errors, [])
            expected = {f"memo {index}" for index in range(20)}
            self.assertEqual(set(manager.get_memos()), expected)

            reloaded = PermanentMemoryManager(memory_file_path=str(memory_path))
            self.assertEqual(set(reloaded.get_memos()), expected)

    def test_permanent_memory_recovers_from_backup_after_interrupted_write(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("first")
            manager.add_memo("second")
            memory_path.write_text("{interrupted", encoding="utf-8")

            recovered = PermanentMemoryManager(memory_file_path=str(memory_path))

            self.assertEqual(recovered.get_memos(), ["first"])

    def test_permanent_memory_recovery_then_failed_save_preserves_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            backup_path = Path(f"{memory_path}.bak")
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("first")
            manager.add_memo("second")
            memory_path.write_text("{interrupted", encoding="utf-8")

            recovered = PermanentMemoryManager(memory_file_path=str(memory_path))
            self.assertEqual(recovered.get_memos(), ["first"])
            backup_before = backup_path.read_bytes()

            real_replace = os.replace

            def fail_primary_replace(source, destination):
                if Path(destination) == memory_path:
                    raise OSError("injected primary replace failure")
                return real_replace(source, destination)

            with patch(
                "cortex_backend.repositories.storage.os.replace",
                side_effect=fail_primary_replace,
            ):
                with self.assertRaises(PersistenceError):
                    recovered.update_memos(["first", "third"])

            self.assertEqual(backup_path.read_bytes(), backup_before)
            self.assertEqual(
                json.loads(memory_path.read_text(encoding="utf-8"))["memos"],
                ["first"],
            )
            self.assertEqual(
                PermanentMemoryManager(memory_file_path=str(memory_path)).get_memos(),
                ["first"],
            )

    def test_permanent_memory_recovery_then_save_updates_primary_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            backup_path = Path(f"{memory_path}.bak")
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("first")
            manager.add_memo("second")
            memory_path.write_text("{interrupted", encoding="utf-8")

            recovered = PermanentMemoryManager(memory_file_path=str(memory_path))
            recovered.update_memos(["first", "third"])

            self.assertEqual(
                json.loads(memory_path.read_text(encoding="utf-8"))["memos"],
                ["first", "third"],
            )
            self.assertEqual(
                json.loads(backup_path.read_text(encoding="utf-8"))["memos"],
                ["first"],
            )
            self.assertEqual(
                PermanentMemoryManager(memory_file_path=str(memory_path)).get_memos(),
                ["first", "third"],
            )

    def test_failed_primary_repair_keeps_valid_backup_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            backup_path = Path(f"{memory_path}.bak")
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("first")
            manager.add_memo("second")
            memory_path.write_text("{interrupted", encoding="utf-8")
            backup_before = backup_path.read_bytes()

            real_replace = os.replace

            def fail_primary_repair(source, destination):
                if Path(destination) == memory_path:
                    raise OSError("injected primary repair failure")
                return real_replace(source, destination)

            with patch(
                "cortex_backend.repositories.storage.os.replace",
                side_effect=fail_primary_repair,
            ):
                recovered = PermanentMemoryManager(memory_file_path=str(memory_path))
                self.assertEqual(recovered.get_memos(), ["first"])
                with self.assertRaises(PersistenceError):
                    recovered.add_memo("third")

            self.assertEqual(memory_path.read_text(encoding="utf-8"), "{interrupted")
            self.assertEqual(backup_path.read_bytes(), backup_before)
            self.assertEqual(
                PermanentMemoryManager(memory_file_path=str(memory_path)).get_memos(),
                ["first"],
            )

    def test_failed_staged_backup_copy_does_not_truncate_existing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            backup_path = Path(f"{memory_path}.bak")
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("first")
            manager.add_memo("second")
            primary_before = memory_path.read_bytes()
            backup_before = backup_path.read_bytes()
            real_copy = shutil.copy2

            def fail_primary_copy(source, destination):
                if Path(source) == memory_path:
                    Path(destination).write_text("{partial", encoding="utf-8")
                    raise OSError("injected backup copy failure")
                return real_copy(source, destination)

            with patch(
                "cortex_backend.repositories.storage.shutil.copy2",
                side_effect=fail_primary_copy,
            ):
                with self.assertRaises(PersistenceError):
                    manager.add_memo("third")

            self.assertEqual(manager.get_memos(), ["first", "second"])
            self.assertEqual(memory_path.read_bytes(), primary_before)
            self.assertEqual(backup_path.read_bytes(), backup_before)


if __name__ == "__main__":
    unittest.main()


class TimestampZoneTests(unittest.TestCase):
    """Stored timestamps must say what zone they are in.

    They were written naive (no offset). ECMAScript parses a zone-less
    date-time as local time, so the WebView rendered every message footer
    shifted by the viewer's UTC offset -- and because the optimistic message
    the UI creates carries a "Z", the displayed time visibly jumped once the
    chat reloaded.
    """

    def _manager(self, root: Path) -> DatabaseManager:
        return DatabaseManager(
            db_path=str(root / "chat.sqlite"),
            legacy_history_dir=str(root / "legacy"),
        )

    def test_message_and_thread_timestamps_carry_an_explicit_utc_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            manager.add_message("thread-1", "user", "hello", thread_title="Greeting")

            chat = manager.load_chat("thread-1")
            self.assertIsNotNone(chat)
            stamps = [chat["timestamp"], chat["messages"][0]["timestamp"]]
            summary = manager.get_all_chats_summary()[0]
            stamps.append(summary["timestamp"])

            for stamp in stamps:
                parsed = datetime.fromisoformat(stamp)
                self.assertIsNotNone(
                    parsed.tzinfo,
                    f"{stamp!r} has no offset, so a browser reads it as local time",
                )
                self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_naive_timestamps_from_older_builds_are_read_back_as_utc(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            manager.add_message("thread-1", "user", "hello", thread_title="Greeting")

            # Rewrite the rows the way an older build stored them.
            with manager.connect() as connection:
                connection.execute(
                    "UPDATE messages SET timestamp = ? WHERE thread_id = ?",
                    ("2026-01-01T12:00:00", "thread-1"),
                )
                connection.execute(
                    "UPDATE threads SET timestamp = ? WHERE id = ?",
                    ("2026-01-01T12:00:00", "thread-1"),
                )

            chat = manager.load_chat("thread-1")
            self.assertEqual(chat["messages"][0]["timestamp"], "2026-01-01T12:00:00+00:00")
            self.assertEqual(chat["timestamp"], "2026-01-01T12:00:00+00:00")

    def test_forking_keeps_each_message_timestamp(self):
        """A fork used to stamp every copied turn with the moment of forking."""
        with tempfile.TemporaryDirectory() as directory:
            manager = self._manager(Path(directory))
            manager.add_message("thread-1", "user", "first", thread_title="Source")
            manager.add_message("thread-1", "assistant", "second")
            source = manager.load_chat("thread-1")["messages"]

            manager.create_chat_from_messages("fork-1", "Fork", source)

            forked = manager.load_chat("fork-1")["messages"]
            self.assertEqual(
                [message["timestamp"] for message in forked],
                [message["timestamp"] for message in source],
            )
            self.assertEqual(
                [message["content"] for message in forked],
                ["first", "second"],
            )


# -- Windows sharing violations and the memory backup copy ---------------------------


def _memory_manager(tmp_path: Path) -> tuple[PermanentMemoryManager, Path]:
    memory_file = tmp_path / "memory.json"
    manager = PermanentMemoryManager(memory_file_path=str(memory_file))
    manager.add_memo("first")  # the next save has a primary to back up
    return manager, memory_file


def _on_disk_memos(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8"))["memos"]


def _leftover_temporaries(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".tmp"))


def _hold_the_memory_file(monkeypatch: pytest.MonkeyPatch, memory_file: Path, *, refusals: int):
    """Refuse the first ``refusals`` renames onto the memory file, as a scanner holding it would."""
    real_replace = os.replace
    target = os.path.normcase(os.path.abspath(memory_file))
    attempts: list[str] = []
    waits: list[float] = []

    def replace(source, destination, *args, **kwargs):
        if os.path.normcase(os.path.abspath(destination)) == target:
            attempts.append(str(source))
            if len(attempts) <= refusals:
                raise PermissionError(errno.EACCES, "The process cannot access the file")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr("time.sleep", waits.append)
    return attempts, waits


def test_a_transient_sharing_violation_does_not_fail_a_memory_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, memory_file = _memory_manager(tmp_path)
    attempts, waits = _hold_the_memory_file(monkeypatch, memory_file, refusals=2)

    manager.add_memo("second")
    monkeypatch.undo()

    assert len(attempts) == 3 and len(waits) == 2
    assert manager.get_memos() == ["first", "second"]
    assert _on_disk_memos(memory_file) == ["first", "second"]
    assert _on_disk_memos(Path(manager.backup_file_path)) == ["first"]
    assert _leftover_temporaries(tmp_path) == []


def test_a_sharing_violation_that_does_not_lift_fails_the_save_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, memory_file = _memory_manager(tmp_path)
    before = memory_file.read_bytes()
    attempts, _ = _hold_the_memory_file(monkeypatch, memory_file, refusals=10_000)

    with pytest.raises(PersistenceError):
        manager.add_memo("second")
    monkeypatch.undo()

    assert len(attempts) == 4  # bounded, not a loop
    assert manager.get_memos() == ["first"]  # the in-memory list rolled back with the file
    assert memory_file.read_bytes() == before
    assert _leftover_temporaries(tmp_path) == []


def test_the_memory_backup_copy_is_flushed_to_disk_before_it_replaces_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup is what the next launch restores from, so it gets the same
    fsync the primary gets: one for the new file and one for the backup copy."""
    manager, _ = _memory_manager(tmp_path)
    backup = os.path.normcase(os.path.abspath(manager.backup_file_path))
    real_replace, real_fsync = os.replace, os.fsync
    flushed_before_backup_replaced: list[int] = []
    flushes = 0

    def fsync(descriptor):
        nonlocal flushes
        flushes += 1
        return real_fsync(descriptor)

    def replace(source, destination, *args, **kwargs):
        if os.path.normcase(os.path.abspath(destination)) == backup:
            flushed_before_backup_replaced.append(flushes)
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    manager.add_memo("second")
    monkeypatch.undo()

    assert flushed_before_backup_replaced == [2]


# -- the legacy JSON directory is retired once it is empty ---------------------------


def _legacy_chat(directory: Path, name: str, *, valid: bool = True) -> Path:
    path = directory / f"{name}.json"
    if valid:
        path.write_text(
            json.dumps({"id": name, "title": name, "messages": [{"role": "user", "content": "hi"}]}),
            encoding="utf-8",
        )
    else:
        path.write_text("{not json", encoding="utf-8")
    return path


def _legacy_manager(tmp_path: Path) -> tuple[DatabaseManager, Path]:
    legacy = tmp_path / "chat_history"
    legacy.mkdir()
    return DatabaseManager(db_path=str(tmp_path / "chats.sqlite"), legacy_history_dir=str(legacy)), legacy


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]


def test_a_second_migration_pass_is_silent_and_retires_the_source_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, legacy = _legacy_manager(tmp_path)
    _legacy_chat(legacy, "one")
    _legacy_chat(legacy, "two")

    with caplog.at_level(logging.INFO):
        first = manager.migrate_from_json_if_needed()
    assert first.migrated == 2
    assert len(_warnings(caplog)) == 1  # the one-time "legacy history found"
    archives = list(tmp_path.glob("chat_history_migrated_*"))
    assert len(archives) == 1
    assert sorted(entry.name for entry in archives[0].iterdir()) == ["one.json", "two.json"]
    assert not legacy.exists()  # nothing left in it, so it is out of the way ...
    retired = tmp_path / "chat_history.retired"
    assert retired.is_dir() and list(retired.iterdir()) == []  # ... under a name that says so

    caplog.clear()
    with caplog.at_level(logging.INFO):
        second = manager.migrate_from_json_if_needed()

    assert second == storage.MigrationResult()
    assert caplog.records == []
    assert sorted(entry.name for entry in archives[0].iterdir()) == ["one.json", "two.json"]
    assert {chat["id"] for chat in manager.get_all_chats_summary()} == {"one", "two"}


def test_an_empty_legacy_directory_is_retired_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, legacy = _legacy_manager(tmp_path)
    (legacy / "quarantine").mkdir()  # left empty by an earlier pass

    with caplog.at_level(logging.INFO):
        result = manager.migrate_from_json_if_needed()

    assert result == storage.MigrationResult()
    assert _warnings(caplog) == []
    assert not legacy.exists()
    # Renamed as it was, so the folder that was empty is still there, still empty, and not deleted.
    retired = tmp_path / "chat_history.retired"
    assert [entry.name for entry in retired.iterdir()] == ["quarantine"]
    assert list((retired / "quarantine").iterdir()) == []


def test_retiring_the_legacy_directory_never_deletes_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, legacy = _legacy_manager(tmp_path)
    (legacy / "quarantine").mkdir()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a folder was deleted")

    monkeypatch.setattr(storage.os, "rmdir", forbidden)
    monkeypatch.setattr(storage.shutil, "rmtree", forbidden)
    manager.migrate_from_json_if_needed()
    monkeypatch.undo()

    assert (tmp_path / "chat_history.retired" / "quarantine").is_dir()


def test_a_retired_name_that_is_taken_is_never_reused(tmp_path: Path) -> None:
    """History dropped into the old folder again is imported again, and retired under a new name."""
    manager, legacy = _legacy_manager(tmp_path)
    manager.migrate_from_json_if_needed()
    first = tmp_path / "chat_history.retired"
    (first / "kept.txt").write_text("from the first retirement", encoding="utf-8")
    assert first.is_dir() and not legacy.exists()

    legacy.mkdir()
    _legacy_chat(legacy, "again")
    result = manager.migrate_from_json_if_needed()

    assert result.migrated == 1
    assert (first / "kept.txt").read_text(encoding="utf-8") == "from the first retirement"
    second = tmp_path / "chat_history.retired-2"
    assert second.is_dir() and list(second.iterdir()) == []
    assert not legacy.exists()


def test_a_folder_that_cannot_be_renamed_is_left_where_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manager, legacy = _legacy_manager(tmp_path)

    def held(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "held by another program")

    monkeypatch.setattr(storage.os, "rename", held)
    with caplog.at_level(logging.INFO):
        result = manager.migrate_from_json_if_needed()
    monkeypatch.undo()

    assert result == storage.MigrationResult()
    assert legacy.is_dir() and not (tmp_path / "chat_history.retired").exists()
    assert _warnings(caplog) == []
    # The next launch, with the folder free, retires it.
    manager.migrate_from_json_if_needed()
    assert not legacy.exists() and (tmp_path / "chat_history.retired").is_dir()


def test_quarantined_files_keep_the_legacy_directory_and_are_never_touched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager, legacy = _legacy_manager(tmp_path)
    _legacy_chat(legacy, "good")
    _legacy_chat(legacy, "broken", valid=False)
    manager.migrate_from_json_if_needed()
    quarantined = legacy / "quarantine" / "broken.json"
    assert quarantined.read_text(encoding="utf-8") == "{not json"

    caplog.clear()
    with caplog.at_level(logging.INFO):
        again = manager.migrate_from_json_if_needed()

    assert again == storage.MigrationResult()
    assert _warnings(caplog) == []
    assert any("quarantine" in record.getMessage() for record in caplog.records)
    assert quarantined.read_text(encoding="utf-8") == "{not json"


def test_a_stray_file_keeps_the_legacy_directory(tmp_path: Path) -> None:
    manager, legacy = _legacy_manager(tmp_path)
    _legacy_chat(legacy, "one")
    (legacy / "notes.txt").write_text("mine", encoding="utf-8")

    manager.migrate_from_json_if_needed()
    manager.migrate_from_json_if_needed()

    assert (legacy / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_a_chat_file_that_could_not_be_archived_stays_and_keeps_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, legacy = _legacy_manager(tmp_path)
    source = _legacy_chat(legacy, "one")

    def refuse(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "held by another program")

    monkeypatch.setattr(manager, "_archive_legacy_file", refuse)
    manager.migrate_from_json_if_needed()
    monkeypatch.undo()

    assert source.exists()  # the source is what the next launch imports again
    again = manager.migrate_from_json_if_needed()
    assert again.skipped == 1  # already imported, so it is archived rather than duplicated
    assert not source.exists()
    assert len(manager.get_all_chats_summary()) == 1


# -- a full disk ---------------------------------------------------------------------


def _writes_a_message(sql: str) -> bool:
    return sql.lstrip().upper().startswith(("INSERT INTO MESSAGES", "UPDATE MESSAGES"))


def _a_chat_with_an_exchange(tmp_path: Path) -> tuple[DatabaseManager, dict]:
    manager = DatabaseManager(db_path=str(tmp_path / "chats.sqlite"))
    manager.add_message("t", "user", "kept", thread_title="Topic", expected_revision=0)
    manager.add_message("t", "assistant", "answer", expected_revision=1)
    chat = manager.load_chat("t")
    assert chat is not None and chat["revision"] == 2
    return manager, chat


def test_a_full_disk_leaves_the_chat_consistent_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, before = _a_chat_with_an_exchange(tmp_path)
    answer_id = int(before["messages"][1]["id"])
    disk_full_when(monkeypatch, _writes_a_message)

    with pytest.raises(PersistenceError) as appended:
        manager.add_message("t", "user", "lost", expected_revision=2)
    with pytest.raises(PersistenceError) as forked:
        manager.create_chat_from_messages("fork", "Fork", before["messages"])
    with pytest.raises(PersistenceError) as replaced:
        manager.replace_message("t", answer_id, "rewritten", expected_revision=2)
    monkeypatch.undo()

    # The failure is reported as what it was, however many layers wrapped it.
    for failure in (appended.value, forked.value, replaced.value):
        assert isinstance(failure.cause, PersistenceError)
        assert isinstance(failure.cause.cause, sqlite3.OperationalError)
        assert "disk is full" in str(failure.cause.cause)
    # Nothing was half written: the chat, its revision and its timestamp are as they were,
    # and no empty fork was left behind.
    assert manager.load_chat("t") == before
    assert manager.load_chat("fork") is None
    assert [item["id"] for item in manager.get_all_chats_summary()] == ["t"]
    # The revision the failed write was guarded by was not spent: once there is room it goes through.
    manager.replace_message("t", answer_id, "saved", expected_revision=2)
    manager.add_message("t", "user", "next", expected_revision=3)
    assert [message["content"] for message in manager.load_chat("t")["messages"]] == ["kept", "saved", "next"]
    assert DatabaseManager._database_is_valid(manager.db_path)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(OSError(errno.ENOSPC, "No space left on device"), True, id="enospc"),
        pytest.param(sqlite3.OperationalError("database or disk is full"), True, id="sqlite-full"),
        pytest.param(sqlite3.OperationalError("database is locked"), False, id="locked"),
        pytest.param(sqlite3.IntegrityError("FOREIGN KEY constraint failed"), False, id="constraint"),
        pytest.param(PermissionError(errno.EACCES, "held by another program"), False, id="permission"),
        pytest.param(RuntimeError("something else"), False, id="unrelated"),
    ],
)
def test_only_a_full_disk_is_recognised_as_one(failure: BaseException, expected: bool) -> None:
    from cortex_backend.api.routes import _is_disk_full

    wrapped = PersistenceError("Failed to add message.", operation="add_message", cause=PersistenceError(
        "SQLite operation failed.", operation="sqlite", cause=failure
    ))
    assert _is_disk_full(failure) is expected
    assert _is_disk_full(wrapped) is expected  # found through the wrapping stores add
    chained = RuntimeError("Could not save.")
    chained.__cause__ = failure
    assert _is_disk_full(chained) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="winerror only exists on Windows")
def test_windows_disk_full_errors_are_recognised() -> None:
    from cortex_backend.api.routes import _is_disk_full

    for winerror in (39, 112):
        assert _is_disk_full(OSError(0, "There is not enough space on the disk", None, winerror))
    assert not _is_disk_full(OSError(0, "Access is denied", None, 5))


def test_a_full_disk_is_reported_as_insufficient_storage_and_the_chat_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cortex_backend.api import create_app
    from cortex_backend.repositories.chats import LegacyDatabaseChatRepository
    from cortex_backend.testing import build_demo_dependencies
    from support import session_headers

    manager, before = _a_chat_with_an_exchange(tmp_path)
    fork_url = "/api/v1/chats/t/forks"
    first_message = str(before["messages"][0]["id"])
    dependencies = build_demo_dependencies()
    dependencies.chats = LegacyDatabaseChatRepository(manager)
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session_headers(client, app)
        disk_full_when(monkeypatch, _writes_a_message)

        full = client.post(fork_url, json={"message_id": first_message}, headers=headers)
        monkeypatch.undo()

        assert full.status_code == 507
        detail = full.json()["detail"]
        assert detail == (
            "Could not fork chat because the disk is full. Free some disk space and try again."
        )
        assert str(tmp_path) not in detail and "kept" not in detail
        # Nothing was saved: the chat is as it was and no empty fork was left behind.
        assert manager.load_chat("t") == before
        assert [item["id"] for item in manager.get_all_chats_summary()] == ["t"]
        # The same request goes through once there is room.
        again = client.post(fork_url, json={"message_id": first_message}, headers=headers)
        assert again.status_code == 201
        assert [message["content"] for message in again.json()["messages"]] == ["kept"]
        assert again.json()["revision"] == 1


def test_a_failure_that_is_not_a_full_disk_is_still_an_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cortex_backend.api import create_app
    from cortex_backend.repositories.chats import LegacyDatabaseChatRepository
    from cortex_backend.testing import build_demo_dependencies
    from support import session_headers

    manager, before = _a_chat_with_an_exchange(tmp_path)
    fork_url = "/api/v1/chats/t/forks"
    first_message = str(before["messages"][0]["id"])
    dependencies = build_demo_dependencies()
    dependencies.chats = LegacyDatabaseChatRepository(manager)
    app = create_app(dependencies, allowed_hosts=("testserver",))
    real_fork = manager.create_chat_from_messages

    def broken(*_args, **_kwargs):
        raise PersistenceError(
            "Failed to create forked chat.", operation="create_chat_from_messages", cause=sqlite3.DatabaseError("x")
        )

    with TestClient(app) as client:
        headers = session_headers(client, app)
        monkeypatch.setattr(manager, "create_chat_from_messages", broken)
        response = client.post(fork_url, json={"message_id": first_message}, headers=headers)
        monkeypatch.setattr(manager, "create_chat_from_messages", real_fork)

    assert response.status_code == 500
    # The request id is the one thing added to the text, so a report can be matched to the log.
    assert response.json()["detail"] == f"Could not fork chat. (Request ID: {response.headers['X-Request-ID']})"
    assert manager.load_chat("t") == before
