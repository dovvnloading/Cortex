"""Headless path and package-boundary tests for the web runtime."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cortex_backend.core.paths import AppPathError, AppPaths
from cortex_backend.repositories.legacy_storage import (
    DatabaseManager,
    PermanentMemoryManager,
)


REPOSITORY_ROOT = Path(__file__).parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
BACKEND_PACKAGE = BACKEND_ROOT / "cortex_backend"


class AppPathsTests(unittest.TestCase):
    def test_windows_paths_preserve_the_existing_data_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.for_windows({"APPDATA": directory})

            expected = Path(directory) / "ChatLLM" / "ChatLLM-Assistant"
            self.assertEqual(paths.data_dir, expected)
            self.assertEqual(paths.database, expected / "cortex_db.sqlite")
            self.assertEqual(paths.legacy_chat_history, expected / "chat_history")
            self.assertEqual(paths.permanent_memory, expected / "memory_bank.json")
            self.assertEqual(paths.permanent_memory_backup, expected / "memory_bank.json.bak")
            self.assertEqual(paths.vector_database, expected / "cortex_vectors.sqlite")

    def test_path_construction_has_no_file_system_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "not-created"
            paths = AppPaths.from_data_dir(data_dir)

            self.assertFalse(data_dir.exists())
            self.assertEqual(paths.ensure_data_dir(), data_dir)
            self.assertTrue(data_dir.is_dir())

    def test_missing_windows_appdata_fails_with_a_safe_error(self):
        with self.assertRaisesRegex(AppPathError, "APPDATA"):
            AppPaths.for_windows({})

    def test_unsupported_platform_requires_injected_paths(self):
        with self.assertRaisesRegex(AppPathError, "Windows only"):
            AppPaths.for_current_user(platform="linux", environ={})

    def test_persistence_managers_use_an_injected_app_paths_root(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.from_data_dir(Path(directory) / "profile")
            database = DatabaseManager(app_paths=paths)
            memories = PermanentMemoryManager(app_paths=paths)

            database.add_message(
                "thread-1", "user", "fixture message", thread_title="Fixture"
            )
            memories.add_memo("fixture memory")

            self.assertEqual(Path(database.db_path), paths.database)
            self.assertEqual(Path(database.legacy_history_dir), paths.legacy_chat_history)
            self.assertEqual(Path(memories.memory_file_path), paths.permanent_memory)
            self.assertEqual(database.load_chat("thread-1")["messages"][0]["content"], "fixture message")
            self.assertEqual(
                PermanentMemoryManager(app_paths=paths).get_memos(), ["fixture memory"]
            )


class BackendBoundaryTests(unittest.TestCase):
    def test_backend_has_no_ui_or_transport_imports(self):
        forbidden_roots = {
            "PySide6",
            "PyQt5",
            "PyQt6",
            "fastapi",
            "main_window",
            "Chat_LLM",
        }

        for relative_directory in ("core", "repositories", "services"):
            for path in (BACKEND_PACKAGE / relative_directory).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    imported_roots: list[str] = []
                    if isinstance(node, ast.Import):
                        imported_roots = [alias.name.split(".", 1)[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_roots = [node.module.split(".", 1)[0]]
                    self.assertFalse(
                        forbidden_roots.intersection(imported_roots),
                        f"forbidden boundary import in {path}: {imported_roots}",
                    )

    def test_web_runtime_imports_without_loading_qt(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(BACKEND_ROOT)
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import cortex_backend.core, cortex_backend.repositories, cortex_backend.services; "
                    "assert 'PySide6' not in sys.modules; "
                    "assert 'PyQt5' not in sys.modules; "
                    "assert 'PyQt6' not in sys.modules"
                ),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_root_launcher_is_web_only(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        process = subprocess.run(
            [sys.executable, "-c", "import main; assert callable(main.main)"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def test_removed_desktop_tree_contains_no_runtime_modules(self):
        legacy_tree = REPOSITORY_ROOT / "Chat_LLM" / "Chat_LLM"
        self.assertFalse(list(legacy_tree.glob("*.py")))
        self.assertFalse((REPOSITORY_ROOT / "Cortex_Startup.py").exists())
        self.assertFalse((REPOSITORY_ROOT / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
