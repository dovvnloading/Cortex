"""The llama.cpp runtime status the API serves mirrors the manager's status."""

from __future__ import annotations

from types import SimpleNamespace

from cortex_backend.api.routes import _llamacpp_status
from cortex_backend.llamacpp.server_manager import LlamaCppRuntimeStatus


def _request_with_manager(manager: object | None) -> object:
    state = SimpleNamespace(llamacpp_manager=manager)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_status_route_reports_the_loaded_context() -> None:
    live = LlamaCppRuntimeStatus(
        state="ready",
        binary_present=True,
        loaded_model="gguf:model.gguf",
        last_error=None,
        models_directory="C:/synthetic/models",
        active_backend="cpu",
        loaded_context=4096,
    )

    status = _llamacpp_status(_request_with_manager(SimpleNamespace(status=live)))

    assert status.loaded_context == 4096
    assert status.model_dump()["loaded_context"] == 4096


def test_status_route_reports_an_unknown_context_as_null() -> None:
    live = LlamaCppRuntimeStatus(
        state="idle",
        binary_present=False,
        loaded_model=None,
        last_error=None,
        models_directory="C:/synthetic/models",
    )

    with_manager = _llamacpp_status(_request_with_manager(SimpleNamespace(status=live)))
    without_manager = _llamacpp_status(_request_with_manager(None))

    assert with_manager.loaded_context is None
    assert without_manager.loaded_context is None
