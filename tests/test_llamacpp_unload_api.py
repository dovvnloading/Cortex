"""Unloading the local model on request: the route, its refusals, and the settings behind idle unload."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cortex_backend.core.settings import CortexSettings, LlamaCppSettings
from cortex_backend.llamacpp.errors import LlamaCppError, RuntimeBusyError
from cortex_backend.llamacpp.server_manager import LlamaCppRuntimeStatus, LlamaServerManager


class _FakeManager:
    """Stands in for the manager: records unloads, reports a status, can be told to refuse."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.unload_calls = 0
        self.closed = False
        self._loaded = True

    @property
    def status(self) -> LlamaCppRuntimeStatus:
        return LlamaCppRuntimeStatus(
            state="ready" if self._loaded else "idle",
            binary_present=True,
            loaded_model="gguf:model.gguf" if self._loaded else None,
            last_error=None,
            models_directory="C:/synthetic/models",
            active_backend="vulkan",
            last_restart_reason=None if self._loaded else "the model was unloaded at your request",
        )

    def unload(self) -> bool:
        self.unload_calls += 1
        if self.raises is not None:
            raise self.raises
        was_loaded, self._loaded = self._loaded, False
        return was_loaded

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_manager(app) -> _FakeManager:
    manager = _FakeManager()
    app.state.llamacpp_manager = manager
    return manager


class _BusyJobs:
    """The app's job registry, except that a response is being generated."""

    def __init__(self, real) -> None:
        self._real = real

    def active_snapshot(self, *, kind: str):
        return object() if kind == "generation" else None

    def __getattr__(self, name: str):
        return getattr(self._real, name)


def test_unload_stops_the_model_and_returns_the_new_status(client, headers, fake_manager) -> None:
    response = client.post("/api/v1/llamacpp/unload", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "idle"
    assert body["loaded_model"] is None
    assert body["last_restart_reason"] == "the model was unloaded at your request"
    assert fake_manager.unload_calls == 1


def test_unload_is_safe_to_repeat(client, headers, fake_manager) -> None:
    assert client.post("/api/v1/llamacpp/unload", headers=headers).status_code == 200
    again = client.post("/api/v1/llamacpp/unload", headers=headers)

    assert again.status_code == 200
    assert again.json()["state"] == "idle"


def test_unload_needs_a_session(client, fake_manager) -> None:
    response = client.post("/api/v1/llamacpp/unload")

    assert response.status_code == 401
    assert fake_manager.unload_calls == 0


def test_unload_is_refused_while_a_response_is_being_generated(app, client, headers, fake_manager) -> None:
    app.state.jobs = _BusyJobs(app.state.jobs)

    response = client.post("/api/v1/llamacpp/unload", headers=headers)

    assert response.status_code == 409
    assert "being generated" in response.json()["detail"]
    assert fake_manager.unload_calls == 0
    assert fake_manager.status.state == "ready"


def test_unload_reports_a_manager_that_is_busy_as_a_conflict(client, headers, app) -> None:
    app.state.llamacpp_manager = _FakeManager(
        raises=RuntimeBusyError("The model is answering a request. Stop it or wait for it to finish, then unload.")
    )

    response = client.post("/api/v1/llamacpp/unload", headers=headers)

    assert response.status_code == 409
    assert "answering a request" in response.json()["detail"]


def test_unload_reports_a_process_that_would_not_exit_without_claiming_success(client, headers, app) -> None:
    app.state.llamacpp_manager = _FakeManager(
        raises=LlamaCppError("The local model runtime did not exit cleanly; restart Cortex before trying again.")
    )

    response = client.post("/api/v1/llamacpp/unload", headers=headers)

    assert response.status_code == 500
    assert "restart Cortex" in response.json()["detail"]


def test_unload_without_a_runtime_says_so(client, headers, app) -> None:
    assert app.state.llamacpp_manager is None

    response = client.post("/api/v1/llamacpp/unload", headers=headers)

    assert response.status_code == 409
    assert "unavailable" in response.json()["detail"]


def test_unload_through_the_real_manager_is_refused_while_a_request_holds_the_model(
    client, headers, app, tmp_path: Path
) -> None:
    manager = LlamaServerManager(
        runtime_dir=tmp_path,
        fetcher=SimpleNamespace(),  # type: ignore[arg-type]
        release=None,
        gpu_backend_setting=lambda: "cpu",
        models_directory=lambda: tmp_path,
    )
    app.state.llamacpp_manager = manager
    try:
        with manager.request_scope():
            busy = client.post("/api/v1/llamacpp/unload", headers=headers)
        idle = client.post("/api/v1/llamacpp/unload", headers=headers)
    finally:
        manager.close()

    assert busy.status_code == 409
    assert "answering a request" in busy.json()["detail"]
    assert idle.status_code == 200
    assert idle.json()["state"] == "idle"


def test_the_unload_route_is_part_of_the_generated_contract() -> None:
    from cortex_backend.api.routers import build_router

    paths = {route.path: route.methods for route in build_router().routes if getattr(route, "methods", None)}

    assert paths["/llamacpp/unload"] == {"POST"}


# -- the idle period that shares the runtime settings -----------------------------------------


def test_the_idle_period_defaults_to_half_an_hour() -> None:
    assert CortexSettings().llamacpp.idle_unload_minutes == 30
    assert CortexSettings.model_validate({"llamacpp": {"gpu_backend": "cpu"}}).llamacpp.idle_unload_minutes == 30


@pytest.mark.parametrize("minutes", [-1, 1441, 10**6])
def test_the_idle_period_is_bounded(minutes: int) -> None:
    with pytest.raises(ValidationError):
        LlamaCppSettings(idle_unload_minutes=minutes)


@pytest.mark.parametrize("minutes", [0, 1, 30, 1440])
def test_zero_and_every_period_up_to_a_day_are_valid(minutes: int) -> None:
    assert LlamaCppSettings(idle_unload_minutes=minutes).idle_unload_minutes == minutes


def test_the_settings_api_round_trips_the_idle_period(client, headers) -> None:
    current = client.get("/api/v1/settings", headers=headers).json()["settings"]
    current["llamacpp"] = {**current.get("llamacpp", {}), "idle_unload_minutes": 12}

    saved = client.put("/api/v1/settings", headers=headers, json={"settings": current})

    assert saved.status_code == 200
    assert client.get("/api/v1/settings", headers=headers).json()["settings"]["llamacpp"]["idle_unload_minutes"] == 12


def test_the_settings_api_refuses_an_idle_period_out_of_range(client, headers) -> None:
    current = client.get("/api/v1/settings", headers=headers).json()["settings"]
    current["llamacpp"] = {**current.get("llamacpp", {}), "idle_unload_minutes": 5000}

    assert client.put("/api/v1/settings", headers=headers, json={"settings": current}).status_code == 422
