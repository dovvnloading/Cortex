"""Headless path and package-boundary tests for the web runtime."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from cortex_backend.core.paths import AppPathError, AppPaths
from cortex_backend.core import paths as paths_module
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

            expected = (
                Path(directory) / "ChatLLM" / "ChatLLM-Assistant"
            ).resolve(strict=False)
            self.assertEqual(paths.data_dir, expected)
            self.assertEqual(paths.database, expected / "cortex_db.sqlite")
            self.assertEqual(paths.legacy_chat_history, expected / "chat_history")
            self.assertEqual(paths.permanent_memory, expected / "memory_bank.json")
            self.assertEqual(paths.permanent_memory_backup, expected / "memory_bank.json.bak")

    def test_path_construction_has_no_file_system_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "not-created"
            paths = AppPaths.from_data_dir(data_dir)
            expected = data_dir.resolve(strict=False)

            self.assertFalse(data_dir.exists())
            self.assertEqual(paths.ensure_data_dir(), expected)
            self.assertTrue(data_dir.is_dir())
            self.assertEqual(paths.webview_profile, expected / "webview")

    def test_missing_windows_appdata_fails_with_a_safe_error(self):
        with self.assertRaisesRegex(AppPathError, "APPDATA"):
            AppPaths.for_windows({})

    def test_unsupported_platform_requires_injected_paths(self):
        with self.assertRaisesRegex(AppPathError, "Windows only"):
            AppPaths.for_current_user(platform="linux", environ={})

    def test_windows_data_roots_reject_unc_paths(self):
        with mock.patch.object(paths_module.sys, "platform", "win32"):
            with self.assertRaisesRegex(AppPathError, "UNC"):
                AppPaths.from_data_dir(r"\\server\share\cortex")

    def test_windows_data_roots_reject_existing_symlink_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with mock.patch.object(paths_module.sys, "platform", "win32"):
                with self.assertRaisesRegex(AppPathError, "reparse"):
                    AppPaths.from_data_dir(link / "child")

    def test_windows_private_acl_failure_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(paths_module.sys, "platform", "win32"):
                with mock.patch.object(
                    paths_module.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1),
                ):
                    with self.assertRaisesRegex(AppPathError, "permissions"):
                        paths_module.secure_private_path(directory, directory=True)

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
        # The Qt entries stay: they are what stops a UI framework creeping
        # back into the headless core. fastapi stays for the same reason on
        # the transport side. main_window and Chat_LLM named modules from a
        # tree that no longer exists, so they guarded nothing.
        forbidden_roots = {
            "PySide6",
            "PyQt5",
            "PyQt6",
            "fastapi",
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

    def test_code_execution_worker_import_stays_lean(self):
        """A code-execution worker subprocess must not pay for its siblings.

        cortex_backend.execution's __init__ re-exports nothing, so importing
        one submodule loads that submodule and no other. Heavy dependencies
        that belong to unrelated siblings must stay out of a worker that only
        runs plain Python: cryptography (native_broker's identity handshake),
        pydantic (broker/worker_protocol's message schemas), and Pillow
        (recipe_provider's image transforms).

        The sibling-module assertion is what keeps the __init__ empty. A plain
        re-export added there loads its submodule as a side effect of importing
        the package, and this fails on that alone -- before the re-exported
        module happens to be one with a heavy dependency.
        """
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(BACKEND_ROOT)
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from cortex_backend.execution.code_execution import code_worker_main; "
                    "assert 'cryptography' not in sys.modules; "
                    "assert 'pydantic' not in sys.modules; "
                    "assert 'PIL' not in sys.modules; "
                    "loaded = sorted("
                    "  name for name in sys.modules"
                    "  if name.startswith('cortex_backend.execution.')"
                    "); "
                    "assert loaded == ['cortex_backend.execution.code_execution'], loaded"
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


if __name__ == "__main__":
    unittest.main()


class ProductionBoundaryTests(unittest.TestCase):
    def test_the_api_package_does_not_import_test_doubles(self):
        """A packaged build must not carry the fake model gateway.

        cortex_backend.api.app used to import the fakes at module scope for a
        demo-dependency helper, and create_app fell back to it whenever it was
        called without dependencies -- so a mis-wired composition root produced
        a working application backed by a fake instead of failing.
        """
        for path in (BACKEND_PACKAGE / "api").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    self.assertFalse(
                        module.startswith("cortex_backend.testing"),
                        f"{path} imports {module}",
                    )

    def test_create_app_requires_its_dependencies(self):
        from cortex_backend.api import create_app

        with self.assertRaises(TypeError):
            create_app()
