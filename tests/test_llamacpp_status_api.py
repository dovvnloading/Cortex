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


def test_status_route_reports_the_launch_failure_cause() -> None:
    live = LlamaCppRuntimeStatus(
        state="failed",
        binary_present=True,
        loaded_model=None,
        last_error="The model does not fit in available memory.",
        models_directory="C:/synthetic/models",
        last_failure_code="memory",
    )

    status = _llamacpp_status(_request_with_manager(SimpleNamespace(status=live)))

    assert status.last_failure_code == "memory"
    assert status.model_dump()["last_failure_code"] == "memory"


def test_status_route_reports_no_failure_cause_by_default() -> None:
    live = LlamaCppRuntimeStatus(
        state="idle",
        binary_present=False,
        loaded_model=None,
        last_error=None,
        models_directory="C:/synthetic/models",
    )

    assert _llamacpp_status(_request_with_manager(SimpleNamespace(status=live))).last_failure_code is None
    assert _llamacpp_status(_request_with_manager(None)).last_failure_code is None


def test_status_route_reports_why_the_gpu_build_was_not_used() -> None:
    note = "No Vulkan graphics loader was found on this computer, so the CPU build is used."
    live = LlamaCppRuntimeStatus(
        state="ready",
        binary_present=True,
        loaded_model="gguf:model.gguf",
        last_error=None,
        models_directory="C:/synthetic/models",
        active_backend="cpu",
        backend_note=note,
    )

    status = _llamacpp_status(_request_with_manager(SimpleNamespace(status=live)))

    assert status.backend_note == note
    assert status.model_dump()["backend_note"] == note
    assert _llamacpp_status(_request_with_manager(None)).backend_note is None


def test_status_route_reports_how_many_layers_are_on_the_gpu() -> None:
    live = LlamaCppRuntimeStatus(
        state="ready",
        binary_present=True,
        loaded_model="gguf:model.gguf",
        last_error=None,
        models_directory="C:/synthetic/models",
        active_backend="vulkan",
        gpu_layers_offloaded=24,
        gpu_layers_total=33,
    )

    status = _llamacpp_status(_request_with_manager(SimpleNamespace(status=live)))

    assert (status.gpu_layers_offloaded, status.gpu_layers_total) == (24, 33)


def test_status_route_reports_unknown_offload_as_null_not_zero() -> None:
    live = LlamaCppRuntimeStatus(
        state="ready",
        binary_present=True,
        loaded_model="gguf:model.gguf",
        last_error=None,
        models_directory="C:/synthetic/models",
        active_backend="vulkan",
    )

    for status in (
        _llamacpp_status(_request_with_manager(SimpleNamespace(status=live))),
        _llamacpp_status(_request_with_manager(None)),
    ):
        assert status.gpu_layers_offloaded is None
        assert status.gpu_layers_total is None


def test_status_route_reports_that_the_context_window_was_limited() -> None:
    note = "The context window was limited to 4096 tokens, the most this model was trained for (32768 were requested)."
    live = LlamaCppRuntimeStatus(
        state="ready",
        binary_present=True,
        loaded_model="gguf:model.gguf",
        last_error=None,
        models_directory="C:/synthetic/models",
        active_backend="cpu",
        loaded_context=4096,
        context_note=note,
    )

    status = _llamacpp_status(_request_with_manager(SimpleNamespace(status=live)))

    assert status.context_note == note
    assert _llamacpp_status(_request_with_manager(None)).context_note is None
