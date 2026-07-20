"""Headless path and package-boundary tests for Web Stage 1."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PySide6.QtCore import QCoreApplication, QStandardPaths

from cortex_backend.core.paths import AppPathError, AppPaths
from memory import DatabaseManager, PermanentMemoryManager


REPOSITORY_ROOT = Path(__file__).parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
BACKEND_PACKAGE = BACKEND_ROOT / "cortex_backend"
LEGACY_APP_ROOT = REPOSITORY_ROOT / "Chat_LLM" / "Chat_LLM"


class AppPathsTests(unittest.TestCase):
    def test_windows_paths_preserve_the_legacy_qt_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.for_windows({"APPDATA": directory})

            expected = Path(directory) / "ChatLLM" / "ChatLLM-Assistant"
            self.assertEqual(paths.data_dir, expected)
            self.assertEqual(paths.database, expected / "cortex_db.sqlite")
            self.assertEqual(paths.legacy_chat_history, expected / "chat_history")
            self.assertEqual(paths.permanent_memory, expected / "memory_bank.json")
            self.assertEqual(
                paths.permanent_memory_backup,
                expected / "memory_bank.json.bak",
            )
            self.assertEqual(paths.vector_database, expected / "cortex_vectors.sqlite")

    def test_path_construction_has_no_file_system_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "not-created"

            paths = AppPaths.from_data_dir(data_dir)

            self.assertEqual(paths.data_dir, data_dir)
            self.assertFalse(data_dir.exists())
            self.assertEqual(paths.ensure_data_dir(), data_dir)
            self.assertTrue(data_dir.is_dir())

    def test_missing_windows_appdata_fails_with_a_safe_error(self):
        with self.assertRaisesRegex(AppPathError, "APPDATA"):
            AppPaths.for_windows({})

    def test_unsupported_platform_requires_injected_paths(self):
        with self.assertRaisesRegex(AppPathError, "Windows only"):
            AppPaths.for_current_user(platform="linux", environ={})

    @unittest.skipUnless(sys.platform == "win32", "Windows compatibility check")
    def test_app_paths_match_qstandardpaths_for_the_legacy_identity(self):
        application = QCoreApplication.instance() or QCoreApplication([])
        previous_organization = application.organizationName()
        previous_application = application.applicationName()
        try:
            application.setOrganizationName("ChatLLM")
            application.setApplicationName("ChatLLM-Assistant")
            qt_data_dir = Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppDataLocation
                )
            )

            self.assertEqual(
                AppPaths.for_current_user().data_dir.resolve(strict=False),
                qt_data_dir.resolve(strict=False),
            )
        finally:
            application.setOrganizationName(previous_organization)
            application.setApplicationName(previous_application)

    def test_persistence_managers_use_an_injected_app_paths_root(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = AppPaths.from_data_dir(Path(directory) / "profile")
            database = DatabaseManager(app_paths=paths)
            memories = PermanentMemoryManager(app_paths=paths)

            database.add_message(
                "thread-1",
                "user",
                "fixture message",
                thread_title="Fixture",
            )
            memories.add_memo("fixture memory")

            self.assertEqual(Path(database.db_path), paths.database)
            self.assertEqual(
                Path(database.legacy_history_dir),
                paths.legacy_chat_history,
            )
            self.assertEqual(Path(memories.memory_file_path), paths.permanent_memory)
            self.assertEqual(
                database.load_chat("thread-1")["messages"][0]["content"],
                "fixture message",
            )
            self.assertEqual(
                PermanentMemoryManager(app_paths=paths).get_memos(),
                ["fixture memory"],
            )


class BackendBoundaryTests(unittest.TestCase):
    def test_backend_core_and_repositories_have_no_ui_or_transport_imports(self):
        forbidden_roots = {
            "PySide6",
            "PyQt5",
            "PyQt6",
            "fastapi",
            "main_window",
            "Chat_LLM",
        }
        checked = []

        for relative_directory in ("core", "repositories", "services"):
            for path in (BACKEND_PACKAGE / relative_directory).rglob("*.py"):
                checked.append(path)
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    imported_roots = []
                    if isinstance(node, ast.Import):
                        imported_roots = [alias.name.split(".", 1)[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported_roots = [node.module.split(".", 1)[0]]
                    self.assertFalse(
                        forbidden_roots.intersection(imported_roots),
                        f"forbidden boundary import in {path}: {imported_roots}",
                    )

        self.assertGreaterEqual(len(checked), 5)

    def test_backend_foundations_import_without_loading_qt(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(BACKEND_ROOT)
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import cortex_backend.core; "
                    "import cortex_backend.repositories; "
                    "import cortex_backend.services; "
                    "assert 'PySide6' not in sys.modules"
                ),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(process.returncode, 0, process.stderr)

    def test_documented_qt_entrypoint_can_resolve_backend_from_source_checkout(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["QT_QPA_PLATFORM"] = "offscreen"
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import Chat_LLM; "
                    "assert 'cortex_backend' in sys.modules; "
                    "assert callable(Chat_LLM.main)"
                ),
            ],
            cwd=LEGACY_APP_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(process.returncode, 0, process.stderr)

    def test_legacy_memory_module_no_longer_imports_pyside(self):
        source = (REPOSITORY_ROOT / "Chat_LLM" / "Chat_LLM" / "memory.py").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("PySide6", source)
        self.assertNotIn("QStandardPaths", source)


if __name__ == "__main__":
    unittest.main()
