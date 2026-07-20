"""Stage 5 settings migration, backup, model, and diagnostics tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
from PySide6.QtCore import QSettings

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.repositories.sqlite_settings import SQLiteSettingsRepository
from cortex_backend.services.models import ModelService
from cortex_backend.testing.fake_ollama import FakeOllamaState
from qt_settings_adapter import QSettingsAdapter


ROOT = Path(__file__).resolve().parents[1]
QSETTINGS_FIXTURE = ROOT / "tests" / "fixtures" / "qsettings" / "legacy.ini"
CHAT_FIXTURE = ROOT / "tests" / "fixtures" / "legacy_chat" / "fixture-chat.json"
MEMORY_FIXTURE = ROOT / "tests" / "fixtures" / "memory" / "memory_bank.json"


def _copy_qsettings_fixture(tmp_path: Path) -> QSettings:
    path = tmp_path / "legacy.ini"
    shutil.copy2(QSETTINGS_FIXTURE, path)
    return QSettings(str(path), QSettings.Format.IniFormat)


def test_qsettings_fixture_maps_every_legacy_key_without_mutating_source(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    before = (tmp_path / "legacy.ini").read_bytes()

    result = QSettingsAdapter(legacy).load()

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
    assert (tmp_path / "legacy.ini").read_bytes() == before


def test_qsettings_migration_handles_malformed_partial_and_repeated_reads(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    legacy.setValue("temperature", "not-a-number")
    legacy.setValue("num_ctx", "1024")
    legacy.remove("suggestions_model")
    legacy.sync()
    before = {key: legacy.value(key) for key in legacy.allKeys()}

    repository = SQLiteSettingsRepository(
        tmp_path / "cortex.sqlite",
        legacy=QSettingsAdapter(legacy),
    )
    migrated = repository.load()
    repeated = repository.load()

    assert set(migrated.invalid_keys) == {"temperature", "num_ctx"}
    assert migrated.settings.suggestions.model == migrated.settings.models.chat
    assert migrated.migration is not None
    assert migrated.migration.status == "migrated"
    assert repeated.migration is not None
    assert repeated.migration.status == "already_migrated"
    assert {key: legacy.value(key) for key in legacy.allKeys()} == before
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM settings_migration_ledger"
        ).fetchone()[0] == 1


def test_settings_database_backup_restores_previous_valid_snapshot(tmp_path: Path):
    legacy = _copy_qsettings_fixture(tmp_path)
    repository = SQLiteSettingsRepository(
        tmp_path / "cortex.sqlite",
        legacy=QSettingsAdapter(legacy),
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

    from memory import DatabaseManager, PermanentMemoryManager

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
