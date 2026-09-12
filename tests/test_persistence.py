"""Persistence, migration, and recovery tests for local Cortex data."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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
                thread.join()

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
                thread.join()

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
