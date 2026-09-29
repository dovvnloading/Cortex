"""End-to-end coverage for the two normal-app execution capabilities."""

from __future__ import annotations

import base64
from io import BytesIO
import time
from types import SimpleNamespace

import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.execution import scratch_compute
from cortex_backend.execution.profiles import build_execution_lifecycle
from cortex_backend.execution.repository import ExecutionRepository
from cortex_backend.execution.scratch_compute import (
    ScratchComputeError,
    evaluate_scratch_expression,
    extract_automatic_expression,
    scratch_worker_main,
    validate_scratch_expression,
)
from cortex_backend.services.generation import GenerationService
from cortex_backend.testing.fake_ollama import FakeGenerationEngine, FakeOllamaState
from support import session_headers as _session


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


_GO_AHEAD = {"go": True}


class _ScratchConnection:
    """A worker pipe that records what the worker says and answers its checkpoint."""

    def __init__(self, go: object = _GO_AHEAD) -> None:
        self._go = go
        self.messages: list[dict[str, object]] = []
        self.closed = False

    def send(self, message: dict[str, object]) -> None:
        self.messages.append(message)

    def recv(self) -> object:
        return self._go

    def close(self) -> None:
        self.closed = True


def _run_scratch_worker_in_process(monkeypatch, connection, expression="12 * 7"):
    """Drive ``scratch_worker_main`` in this process without clearing its environment.

    The entry point scrubs ``os.environ`` and applies process-wide limits, which
    is right in a worker child and would wreck the test runner, so those two
    are recorded instead of run.
    """

    events: list[str] = []
    monkeypatch.setattr(scratch_compute, "scrub_worker_environment", lambda: events.append("scrub"))
    monkeypatch.setattr(
        scratch_compute, "apply_resource_limits", lambda **_kwargs: events.append("limits")
    )
    scratch_worker_main(connection, SimpleNamespace(is_set=lambda: False), expression)
    return events


def test_scratch_worker_announces_readiness_before_evaluating(monkeypatch):
    connection = _ScratchConnection()

    events = _run_scratch_worker_in_process(monkeypatch, connection)

    assert connection.messages == [
        {"ok": True, "event": "ready"},
        {"ok": True, "value": "84"},
    ]
    assert connection.closed is True
    # Its own environment and limits are dealt with before it says ready.
    assert events == ["scrub", "limits"]


def test_scratch_worker_does_not_evaluate_until_it_is_released(monkeypatch):
    evaluated: list[str] = []
    monkeypatch.setattr(
        scratch_compute,
        "evaluate_scratch_expression",
        lambda expression, **_kwargs: evaluated.append(expression),
    )

    for denied_go in ({"go": False}, {}, None, "go"):
        connection = _ScratchConnection(go=denied_go)
        _run_scratch_worker_in_process(monkeypatch, connection)
        assert connection.messages == [{"ok": True, "event": "ready"}]
        assert connection.closed is True

    assert evaluated == []


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


def test_automatic_compute_prefix_does_not_backtrack_on_a_second_line():
    """The auto-compute prefix runs on the event loop, against the raw message.

    Its tail used to be ``(.+?)\s*[?.!]*\s*$`` -- three quantifiers that
    could each claim the same run of trailing whitespace, plus a ``.`` that
    could claim it too. Any prompt whose match had to fail made the engine try
    every division of that run; because ``.`` cannot cross a newline, an
    ordinary two-line message was enough. Cost grew with roughly the cube of
    the whitespace length: 2,400 spaces took 32 seconds, during which no other
    request, SSE stream or status poll could be served.
    """
    prompt = "what is 2+2" + " " * 2400 + "\nthanks!"

    started = time.perf_counter()
    assert extract_automatic_expression(prompt) is None
    elapsed = time.perf_counter() - started

    # Now well under a millisecond. The bound is deliberately loose so a
    # loaded CI runner cannot make this flaky, and still fails by ~16x
    # against the unfixed expression.
    assert elapsed < 2.0


def test_automatic_compute_prefix_still_reads_the_requests_it_should():
    """Removing the ambiguous tail must not change which prompts are accepted."""
    assert extract_automatic_expression("what is 2+2") == "2+2"
    assert extract_automatic_expression("what is 2+2?") == "2+2"
    assert extract_automatic_expression("compute 2+2 ?  ") == "2+2"
    assert extract_automatic_expression("  solve   10 / 4  ") == "10 / 4"
    assert extract_automatic_expression("Compute 1+1.") == "1+1"
    assert extract_automatic_expression("how much is 2**8") == "2**8"
    # Prose, an incomplete request, and a second line stay ordinary chat.
    assert extract_automatic_expression("what is the weather") is None
    assert extract_automatic_expression("compute") is None
    assert extract_automatic_expression("compute 2+2\nand also 3+3") is None


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
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.content.startswith(b"\x89PNG\r\n\x1a\n")


# A 2x2 GIF and a 2x2 BMP, written out as literals so building them cannot
# itself initialise Pillow's plugin registry and mask what is being tested.
_GIF_BYTES = bytes.fromhex(
    "47494638396102000200800000000000ffffff21f9040100000000"
    "2c00000000020002000002028401003b"
)
_BMP_BYTES = bytes.fromhex(
    "424d3a0000000000000036000000280000000200000002000000010018000000"
    "000004000000130b0000130b00000000000000000000ffffffffffff0000ffff"
    "ffffffff0000"
)


def test_image_capability_probe_leaves_chat_attachment_codecs_alone():
    """Probing the image provider must not narrow Pillow for the whole process.

    The probe imports PNG/JPEG/WebP explicitly and used to also set
    ``Image._initialized = 2``, which makes ``Image.init()`` a no-op for the
    rest of the interpreter. In the recipe worker child that is deliberate.
    In the backend process it left GIF, BMP and TIFF permanently unregistered,
    so ``Image.open`` raised ``UnidentifiedImageError`` and a valid GIF
    attachment was refused as ``attachment_image_invalid``.

    Run in a fresh interpreter: the flag is process-global and one-way, so any
    earlier test that decoded an image would register everything and hide the
    defect.
    """
    repository_root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "backend")
    program = (
        # Exactly the real startup order: the local profile builds its
        # coordinator, which probes the image provider, before any upload.
        "from cortex_backend.execution.recipe_provider import _pillow_health;"
        "available, code, _ = _pillow_health();"
        "assert available, code;"
        "from cortex_backend.services.attachments import _validate_image;"
        f"assert _validate_image({_GIF_BYTES!r}) == ('image/gif', 'gif');"
        f"assert _validate_image({_BMP_BYTES!r}) == ('image/bmp', 'bmp')"
    )
    process = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 0, process.stderr


def test_the_recipe_worker_still_pins_its_plugin_registry():
    """The worker child keeps the protection the probe gave up.

    It handles only the three fixed formats and must not let Image.open fall
    back to Pillow's broad optional-codec scan, so the pin moved there rather
    than being dropped.
    """
    repository_root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root / "backend")
    program = (
        "from PIL import Image;"
        "from cortex_backend.execution.recipe_provider import pin_plugin_registry;"
        "assert Image._initialized != 2;"
        "pin_plugin_registry();"
        "assert Image._initialized == 2"
    )
    process = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert process.returncode == 0, process.stderr


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


# -- The validator and the evaluator agree about what a call may look like ----------------


@pytest.mark.parametrize(
    "expression",
    ["min(5)", "max(5)", "abs(1, 2)", "abs()", "sqrt(1, 2)", "sqrt()", "round(1, 2, 3)", "round()", "min()"],
)
def test_a_call_with_the_wrong_number_of_arguments_does_not_validate(expression: str):
    """The validator accepted one to sixteen arguments for any allowed name.

    ``min(5)`` and ``abs(1, 2)`` therefore validated and then failed inside the
    worker, so asking "what is min(5)?" created a durable failed job in the
    tray instead of falling back to ordinary chat.
    """

    with pytest.raises(ScratchComputeError) as validated:
        validate_scratch_expression(expression)
    assert validated.value.code == "expression_not_allowed"
    # The evaluator refuses the same shapes with the same code.
    with pytest.raises(ScratchComputeError) as evaluated:
        evaluate_scratch_expression(expression)
    assert evaluated.value.code == "expression_not_allowed"


@pytest.mark.parametrize(
    "expression",
    ["min(1, 2)", "max(1, 2, 3)", "abs(-3)", "sqrt(81)", "sqrt(0)", "sqrt(-0)", "sqrt(--4)", "round(2.5)", "round(2.567, 2)"],
)
def test_every_call_shape_the_evaluator_accepts_still_validates_and_evaluates(expression: str):
    assert validate_scratch_expression(expression) == expression
    assert evaluate_scratch_expression(expression).value


def test_the_square_root_of_a_negative_number_has_its_own_error_code():
    # A literal is refused before any job exists...
    with pytest.raises(ScratchComputeError) as validated:
        validate_scratch_expression("sqrt(-4)")
    assert validated.value.code == "domain_error"
    # ...and one only the evaluator can see is refused with the same code, not
    # reported as a disallowed expression.
    assert validate_scratch_expression("sqrt(2 - 6)") == "sqrt(2 - 6)"
    with pytest.raises(ScratchComputeError) as evaluated:
        evaluate_scratch_expression("sqrt(2 - 6)")
    assert evaluated.value.code == "domain_error"


def test_a_prompt_that_cannot_evaluate_falls_back_to_ordinary_chat():
    for prompt in ("what is min(5)?", "what is abs(1, 2)", "what is sqrt(-4)?", "compute round()"):
        assert extract_automatic_expression(prompt) is None
    assert extract_automatic_expression("what is min(3, 5)?") == "min(3, 5)"
    assert extract_automatic_expression("what is sqrt(16)") == "sqrt(16)"
