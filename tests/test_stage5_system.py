"""Stage 5 settings migration, backup, model, and diagnostics tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient

import Cortex_Preview
from Cortex_Preview import build_preview_app
from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.repositories.legacy_settings import LegacySettingsReader
from cortex_backend.repositories.legacy_storage import (
    DatabaseManager,
    PermanentMemoryManager,
)
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository
from cortex_backend.services.models import ModelService
from cortex_backend.testing.fake_ollama import FakeOllamaState


ROOT = Path(__file__).resolve().parents[1]
QSETTINGS_FIXTURE = ROOT / "tests" / "fixtures" / "qsettings" / "legacy.ini"
CHAT_FIXTURE = ROOT / "tests" / "fixtures" / "legacy_chat" / "fixture-chat.json"
MEMORY_FIXTURE = ROOT / "tests" / "fixtures" / "memory" / "memory_bank.json"


def _copy_qsettings_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.ini"
    shutil.copy2(QSETTINGS_FIXTURE, path)
    return path


def test_qsettings_fixture_maps_every_legacy_key_without_mutating_source(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    before = legacy.read_bytes()

    result = LegacySettingsReader(legacy).load()

    assert result.invalid_keys == ()
    assert set(result.present_keys) == {
        "agreement_accepted",
        "chat_model",
        "memories_enabled",
        "num_ctx",
        "seed",
        "suggestions_enabled",
        "suggestions_model",
        "temperature",
        "target_language",
        "theme",
        "translation_enabled",
        "user_system_instructions",
    }
    assert result.settings.models.chat == "gemma3:4b"
    assert result.settings.generation.num_ctx == 8192
    assert result.settings.translation.target_language == "French"
    assert legacy.read_bytes() == before


def test_qsettings_migration_handles_malformed_partial_and_repeated_reads(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    legacy.write_text(
        legacy.read_text(encoding="utf-8")
        .replace("temperature=1.25", "temperature=not-a-number")
        .replace("num_ctx=8192", "num_ctx=1024")
        .replace("suggestions_model=qwen3:4b\n", ""),
        encoding="utf-8",
    )
    before = legacy.read_bytes()

    repository = SQLiteSettingsRepository(
        tmp_path / "cortex.sqlite",
        legacy=LegacySettingsReader(legacy),
    )
    migrated = repository.load()
    repeated = repository.load()

    assert set(migrated.invalid_keys) == {"temperature", "num_ctx"}
    assert migrated.settings.suggestions.model is None
    assert migrated.migration is not None
    assert migrated.migration.status == "migrated"
    assert repeated.migration is not None
    assert repeated.migration.status == "already_migrated"
    assert legacy.read_bytes() == before
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM settings_migration_ledger"
        ).fetchone()[0] == 1


def test_fresh_settings_migration_is_safe_for_parallel_workspace_reads(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    repository = SQLiteSettingsRepository(
        tmp_path / "cortex.sqlite",
        legacy=LegacySettingsReader(legacy),
    )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: repository.load(), range(5)))

    assert {result.settings.models.chat for result in results} == {"gemma3:4b"}
    assert sum(result.migration is not None for result in results) == 5
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM settings_migration_ledger"
        ).fetchone()[0] == 1


def test_fresh_settings_migration_is_safe_across_repository_instances(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    migration_barrier = Barrier(2)

    class BarrieredLegacyReader:
        def __init__(self, path: Path) -> None:
            self.reader = LegacySettingsReader(path)

        def load(self, *, defaults=None):
            result = self.reader.load(defaults=defaults)
            migration_barrier.wait()
            return result

    db_path = tmp_path / "cortex.sqlite"
    repositories = (
        SQLiteSettingsRepository(db_path, legacy=BarrieredLegacyReader(legacy)),
        SQLiteSettingsRepository(db_path, legacy=BarrieredLegacyReader(legacy)),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda repository: repository.load(), repositories))

    assert {result.settings.models.chat for result in results} == {"gemma3:4b"}
    assert {result.migration.status for result in results if result.migration} == {"migrated", "already_migrated"}
    with repositories[0].connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM settings_migration_ledger"
        ).fetchone()[0] == 1


def test_settings_database_backup_restores_previous_valid_snapshot(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    repository = SQLiteSettingsRepository(
        tmp_path / "cortex.sqlite",
        legacy=LegacySettingsReader(legacy),
    )
    original = repository.load().settings
    updated = original.model_copy(
        update={"appearance": original.appearance.model_copy(update={"theme": "light"})}
    )

    repository.save(updated)
    assert repository.backup_path.exists()
    repository.restore_backup()

    restored = repository.load().settings
    assert restored == original
    assert restored.appearance.theme == "dark"


def test_existing_chat_and_memory_fixtures_remain_unchanged(tmp_path: Path):
    legacy_dir = tmp_path / "chat_history"
    legacy_dir.mkdir()
    shutil.copy2(CHAT_FIXTURE, legacy_dir / CHAT_FIXTURE.name)
    memory_path = tmp_path / "memory_bank.json"
    shutil.copy2(MEMORY_FIXTURE, memory_path)

    database = DatabaseManager(
        db_path=str(tmp_path / "cortex.sqlite"),
        legacy_history_dir=str(legacy_dir),
    )
    result = database.migrate_from_json_if_needed()
    memories = PermanentMemoryManager(memory_file_path=str(memory_path))

    assert result.migrated == 1
    assert database.load_chat("fixture-chat")["messages"][1]["content"] == "Fixture answer"
    assert memories.get_memos() == json.loads(memory_path.read_text(encoding="utf-8"))["memos"]
    assert (legacy_dir / CHAT_FIXTURE.name).exists() is False


def test_packaged_runtime_builder_opens_existing_chat_fixture_without_qt(tmp_path: Path):
    legacy_dir = tmp_path / "chat_history"
    legacy_dir.mkdir()
    shutil.copy2(CHAT_FIXTURE, legacy_dir / CHAT_FIXTURE.name)

    app = build_preview_app(
        data_dir=tmp_path,
        serve_frontend=False,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.post(
            "/api/v1/session/exchange",
            json={"bootstrap_token": app.state.session_manager.bootstrap_token},
        ).json()["session_token"]
        response = client.get(
            "/api/v1/chats",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()[0]["id"] == "fixture-chat"
    assert not (legacy_dir / CHAT_FIXTURE.name).exists()
    assert list(tmp_path.glob("chat_history_migrated_*/*.json"))


def test_preview_app_builds_ollama_client_with_a_bounded_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured_kwargs: dict[str, object] = {}
    real_client_cls = Cortex_Preview.ollama.Client

    class RecordingClient(real_client_cls):
        def __init__(self, *, host=None, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__(host=host, **kwargs)

    monkeypatch.setattr(Cortex_Preview.ollama, "Client", RecordingClient)

    build_preview_app(data_dir=tmp_path, serve_frontend=False)

    assert captured_kwargs.get("timeout") is not None
    assert isinstance(captured_kwargs["timeout"], httpx.Timeout)


def test_model_inventory_pull_progress_and_failure_are_safe():
    state = FakeOllamaState(installed_models={"qwen3:8b"})
    app = create_app(
        build_demo_dependencies(ollama_state=state),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
    )
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/session/exchange",
            json={"bootstrap_token": app.state.session_manager.bootstrap_token},
        ).json()["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        inventory = client.get("/api/v1/models", headers=headers)
        assert inventory.status_code == 200
        assert inventory.json()["connection"]["status"] == "connected"

        accepted = client.post(
            "/api/v1/models/pulls",
            json={"model": "nemotron-3-nano:4b"},
            headers=headers,
        )
        assert accepted.status_code == 202
        events = client.get(
            f"/api/v1/jobs/{accepted.json()['job_id']}/events",
            headers=headers,
        )
        assert events.status_code == 200
        assert '"phase":"model_pull"' in events.text
        assert '"percent":100' in events.text
        assert "nemotron-3-nano:4b" in client.get(
            "/api/v1/models", headers=headers
        ).json()["installed_models"]

        state.fail_pull_stream = True
        failed = client.post(
            "/api/v1/models/pulls",
            json={"model": "failed-model:1b"},
            headers=headers,
        )
        failed_events = client.get(
            f"/api/v1/jobs/{failed.json()['job_id']}/events",
            headers=headers,
        )
        assert failed_events.status_code == 200
        assert '"status":"failed"' in failed_events.text
        assert "failed-model:1b" not in client.get(
            "/api/v1/models", headers=headers
        ).json()["installed_models"]


def test_duplicate_model_tags_are_normalized_to_one_installed_entry():
    details = ModelService.extract_model_details(
        {"models": [{"name": "qwen3:8b", "size": 1}, {"name": "qwen3:8b", "size": 2}]}
    )

    assert [item.name for item in details] == ["qwen3:8b"]


def test_model_inventory_stays_available_when_ollama_is_unavailable():
    state = FakeOllamaState(fail_list=True)
    app = create_app(
        build_demo_dependencies(ollama_state=state),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
    )
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/session/exchange",
            json={"bootstrap_token": app.state.session_manager.bootstrap_token},
        ).json()["session_token"]
        response = client.get(
            "/api/v1/models",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert response.json()["connection"]["status"] == "error"


def test_diagnostics_exposes_migration_and_setup_capabilities():
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
    )
    with TestClient(app) as client:
        token = client.post(
            "/api/v1/session/exchange",
            json={"bootstrap_token": app.state.session_manager.bootstrap_token},
        ).json()["session_token"]
        diagnostics = client.get(
            "/api/v1/diagnostics",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert diagnostics.status_code == 200
        payload = diagnostics.json()
        assert payload["settings_source"] == "memory"
        assert payload["ollama_setup_url"] == "https://ollama.com/download"


def test_settings_live_in_their_own_database_not_the_chat_database(tmp_path: Path):
    """Settings writes take a full-file backup copy first, so colocating them
    with chat history meant every settings save byte-copied the whole
    transcript store. The settings database must be a separate file, and its
    backup must never touch the chat database."""
    chat_db = tmp_path / "cortex_db.sqlite"
    settings_db = tmp_path / "cortex_settings.sqlite"
    chat_db.write_bytes(b"pretend this is a large chat history" * 1000)
    chat_before = chat_db.read_bytes()

    repository = SQLiteSettingsRepository(settings_db, adopt_from=chat_db)
    saved = repository.load().settings
    repository.save(saved)

    assert settings_db.exists()
    # The backup that save() takes is of the settings file, not the chat one.
    assert repository.backup_path == Path(f"{settings_db}.bak")
    assert not Path(f"{chat_db}.bak").exists()
    assert chat_db.read_bytes() == chat_before


def test_settings_colocated_in_the_chat_database_are_adopted_once(tmp_path: Path):
    """Regression guard for the upgrade path: without adoption, every existing
    install would silently revert to default settings the first time it ran a
    build that moved settings into their own file."""
    chat_db = tmp_path / "cortex_db.sqlite"
    settings_db = tmp_path / "cortex_settings.sqlite"

    # An install from before the split: settings living inside the chat database.
    old = SQLiteSettingsRepository(chat_db)
    configured = old.load().settings
    configured = configured.model_copy(
        update={"models": configured.models.model_copy(update={"chat": "gguf:kept.gguf"})}
    )
    old.save(configured)

    adopted = SQLiteSettingsRepository(settings_db, adopt_from=chat_db)

    assert adopted.load().settings.models.chat == "gguf:kept.gguf"
    with adopted.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM settings_migration_ledger WHERE migration_key = ?",
            ("chatdb-colocated-settings-to-own-file-v1",),
        ).fetchone()[0] == 1

    # Adoption is one-time: a later edit is not clobbered by re-adopting the
    # stale row on the next startup.
    current = adopted.load().settings
    adopted.save(
        current.model_copy(
            update={"models": current.models.model_copy(update={"chat": "gguf:newer.gguf"})}
        )
    )
    reopened = SQLiteSettingsRepository(settings_db, adopt_from=chat_db)
    assert reopened.load().settings.models.chat == "gguf:newer.gguf"


def test_adoption_is_skipped_cleanly_when_there_is_nothing_to_adopt(tmp_path: Path):
    """A fresh install, and an unreadable or settings-free old database, must
    both start normally rather than failing closed."""
    settings_db = tmp_path / "cortex_settings.sqlite"

    # Nothing at the old path at all.
    fresh = SQLiteSettingsRepository(settings_db, adopt_from=tmp_path / "absent.sqlite")
    assert fresh.load().settings is not None

    # An old database that exists but holds no settings table.
    other_db = tmp_path / "unrelated.sqlite"
    other_db.write_bytes(b"not a sqlite file at all")
    second = tmp_path / "second_settings.sqlite"
    repository = SQLiteSettingsRepository(second, adopt_from=other_db)
    assert repository.load().settings is not None
