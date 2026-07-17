"""Persistence, migration, and recovery tests for local Cortex data."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).parents[1] / "Chat_LLM" / "Chat_LLM"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from memory import DatabaseManager, PermanentMemoryManager, PersistenceError  # noqa: E402


class PersistenceTests(unittest.TestCase):
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

    def test_permanent_memory_recovers_from_backup_after_interrupted_write(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_path = Path(directory) / "memory_bank.json"
            manager = PermanentMemoryManager(memory_file_path=str(memory_path))
            manager.add_memo("first")
            manager.add_memo("second")
            memory_path.write_text("{interrupted", encoding="utf-8")

            recovered = PermanentMemoryManager(memory_file_path=str(memory_path))

            self.assertEqual(recovered.get_memos(), ["first"])


if __name__ == "__main__":
    unittest.main()
