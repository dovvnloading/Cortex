"""Persistence, migration, and recovery tests for local Cortex data."""

import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cortex_backend.repositories.legacy_storage as legacy_storage
from cortex_backend.repositories.legacy_storage import (
    DatabaseManager,
    PermanentMemoryManager,
    PersistenceError,
)


class PersistenceTests(unittest.TestCase):
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
                    "messages": [{"role": "user", "content": "x" * (legacy_storage.MAX_LEGACY_MESSAGE_CONTENT_CHARS + 1)}],
                },
                "too-many-messages.json": {
                    "id": "too-many-messages",
                    "messages": [
                        {"role": "user", "content": "x"}
                    ] * (legacy_storage.MAX_LEGACY_CHAT_MESSAGES + 1),
                },
            }
            for filename, record in records.items():
                (legacy / filename).write_text(json.dumps(record), encoding="utf-8")

            manager = DatabaseManager(
                db_path=str(root / "chats.sqlite"),
                legacy_history_dir=str(legacy),
            )
            result = manager.migrate_from_json_if_needed()

            self.assertEqual(result, legacy_storage.MigrationResult(quarantined=3))
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
            with patch.object(legacy_storage, "MAX_LEGACY_CHAT_FILE_BYTES", 32):
                result = manager.migrate_from_json_if_needed()

            self.assertEqual(result, legacy_storage.MigrationResult(migrated=1, quarantined=1))
            self.assertTrue((legacy / "quarantine" / "oversized.json").exists())
            self.assertIsNotNone(manager.load_chat("valid"))

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
                "cortex_backend.repositories.legacy_storage.os.replace",
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
                "cortex_backend.repositories.legacy_storage.os.replace",
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
                "cortex_backend.repositories.legacy_storage.shutil.copy2",
                side_effect=fail_primary_copy,
            ):
                with self.assertRaises(PersistenceError):
                    manager.add_memo("third")

            self.assertEqual(manager.get_memos(), ["first", "second"])
            self.assertEqual(memory_path.read_bytes(), primary_before)
            self.assertEqual(backup_path.read_bytes(), backup_before)


if __name__ == "__main__":
    unittest.main()
