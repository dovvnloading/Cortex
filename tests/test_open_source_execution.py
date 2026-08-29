"""End-to-end coverage for the two normal-app execution capabilities."""

from __future__ import annotations

import base64
from io import BytesIO
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from cortex_backend.api import build_demo_dependencies, create_app
from cortex_backend.execution import ExecutionRepository, build_execution_lifecycle
from cortex_backend.execution.scratch_compute import (
    ScratchComputeError,
    evaluate_scratch_expression,
)
from cortex_backend.services.generation import GenerationService
from cortex_backend.testing.fake_ollama import FakeGenerationEngine, FakeOllamaState


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")
INSTALLATION_PRINCIPAL_ID = "a" * 64


class _ImmediateScratchCoordinator:
    """Deterministic coordinator seam for generation-observation coverage."""

    scratch_available = True

    def __init__(self) -> None:
        self.repository = SimpleNamespace(
            installation_principal_id=INSTALLATION_PRINCIPAL_ID
        )
        self.request = None
        self.wait_timeout: float | None = None
        self.closed = False

    def start_scratch(self, request):
        self.request = request
        return SimpleNamespace(job_id="automatic-scratch")

    def wait(self, job_id: str, *, timeout: float):
        assert job_id == "automatic-scratch"
        self.wait_timeout = timeout
        return SimpleNamespace(status="succeeded", result={"value": "81"})

    def shutdown(self) -> None:
        self.closed = True


def _session(client: TestClient, app) -> dict[str, str]:
    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}


def _image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()


def _wait_for_terminal(client: TestClient, headers: dict[str, str], job_id: str) -> dict:
    for _ in range(500):
        response = client.get(f"/api/v1/execution/{job_id}", headers=headers)
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.01)
    raise AssertionError("execution job did not finish")


def _app(tmp_path):
    repository = ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
    )
    lifecycle = build_execution_lifecycle(repository, profile="local")
    return create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        execution_lifecycle=lifecycle,
        installation_principal_id=repository.installation_principal_id,
    )


def test_safe_expression_language_rejects_python_and_host_capabilities():
    assert evaluate_scratch_expression("round(sqrt(81) / 2, 2)").value == "4.5"
    for expression in (
        "__import__('os').system('whoami')",
        "open('secret.txt').read()",
        "[value for value in range(10)]",
        "(lambda: 1)()",
    ):
        try:
            evaluate_scratch_expression(expression)
        except ScratchComputeError:
            continue
        raise AssertionError(f"unsafe expression was accepted: {expression}")


def test_local_profile_runs_scratch_and_fixed_image_recipe_end_to_end(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        system = client.get("/api/v1/system", headers=headers)
        assert system.status_code == 200
        assert system.json()["execution_preview_available"] is True
        assert system.json()["scratch_compute_available"] is True
        assert system.json()["image_transform_available"] is True

        scratch = client.post(
            "/api/v1/execution/scratch",
            headers=headers,
            json={"request_id": "scratch-one", "expression": "12 * (3 + 4)"},
        )
        assert scratch.status_code == 202
        completed = _wait_for_terminal(client, headers, scratch.json()["job_id"])
        assert completed["status"] == "succeeded"
        assert completed["result"] == {
            "schema_version": "scratch.result.v1",
            "value": "84",
        }

        unsafe = client.post(
            "/api/v1/execution/scratch",
            headers=headers,
            json={"request_id": "scratch-unsafe", "expression": "open('file')"},
        )
        assert unsafe.status_code == 422

        staged = client.post(
            "/api/v1/execution/attachments",
            headers=headers,
            json={
                "request_id": "image-stage",
                "content_base64": base64.b64encode(_image_bytes()).decode("ascii"),
            },
        )
        assert staged.status_code == 201
        artifact_id = staged.json()["artifact_id"]
        transformed = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json={
                "request_id": "image-transform",
                "source_artifact_id": artifact_id,
                "plan": {
                    "schema_version": "artifact.transform.v1",
                    "input_artifact_id": artifact_id,
                    "steps": [{"op": "grayscale"}],
                    "output_format": "png",
                },
            },
        )
        assert transformed.status_code == 202
        image_completed = _wait_for_terminal(client, headers, transformed.json()["job_id"])
        assert image_completed["status"] == "succeeded"
        result_artifact = image_completed["result"]["artifact_id"]
        download = client.get(
            f"/api/v1/execution/artifacts/{result_artifact}", headers=headers
        )
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("image/png")
        assert download.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_explicit_math_request_adds_a_verified_local_observation_to_generation():
    state = FakeOllamaState()
    dependencies = build_demo_dependencies(ollama_state=state)
    captured = []
    coordinator = _ImmediateScratchCoordinator()
    dependencies.generation = GenerationService(
        history_loader=lambda thread_id: (dependencies.chats.get_chat(thread_id) or {}).get(
            "messages", []
        ),
        memory_loader=dependencies.memories.get_memos,
        engine_factory=lambda snapshot: captured.append(snapshot)
        or FakeGenerationEngine(state),
    )
    app = create_app(
        dependencies,
        allowed_hosts=ALLOWED_HOSTS,
        execution_coordinator=coordinator,
        installation_principal_id=INSTALLATION_PRINCIPAL_ID,
    )
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/generations",
            headers=headers,
            json={"request_id": "generation-math", "user_input": "calculate 9 * 9"},
        )
        assert accepted.status_code == 202
        for _ in range(500):
            generation = client.get(
                f"/api/v1/generations/{accepted.json()['job_id']}", headers=headers
            )
            assert generation.status_code == 200
            if generation.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("generation did not finish")
    assert generation.json()["status"] == "succeeded"
    assert captured
    assert coordinator.request is not None
    assert coordinator.request.expression == "9 * 9"
    assert coordinator.wait_timeout is not None
    assert coordinator.closed is True
    # The verified result reaches the model as a host observation, not as a
    # user instruction. Worker output is data: the prompt renders it in the
    # user turn inside untrusted-reference delimiters, while the system role
    # stays reserved for the user's own standing policy.
    assert "9 * 9 = 81" in (captured[0].host_observations or "")
    assert "9 * 9 = 81" not in (captured[0].user_system_instructions or "")
