"""Recipe API and client-boundary tests over an explicit execution lifecycle."""

from __future__ import annotations

from io import BytesIO
import base64
import hashlib
from threading import Event
import time
from typing import Any

from fastapi.testclient import TestClient
from PIL import Image

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from cortex_backend.execution.recipe_coordinator import (
    RecipeExecutionCoordinator,
    RecipeWorkerOutput,
)
from cortex_backend.execution.repository import ExecutionRepository
from cortex_backend.execution.lifecycle import ExecutionLifecycle, RuntimeHealth
from support import session_headers as _session


ALLOWED_HOSTS = ("testserver", "127.0.0.1", "localhost", "::1")


def _image_bytes() -> bytes:
    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()



class _Attempt:
    def __init__(self) -> None:
        content = _image_bytes()
        self.output = RecipeWorkerOutput(
            content=content,
            mime_type="image/png",
            format="PNG",
            width=4,
            height=3,
            sha256=hashlib.sha256(content).hexdigest(),
        )
        self.cancelled = Event()

    def transform(
        self,
        _request_id: str,
        _job_id: str,
        _plan: Any,
        _content: bytes,
        cancel_event: Event,
    ) -> RecipeWorkerOutput:
        if cancel_event.is_set() or self.cancelled.is_set():
            raise RuntimeError("cancelled")
        return self.output

    def cancel(self, _reason: str = "user") -> None:
        self.cancelled.set()

    def close(self) -> None:
        return None


def _app(tmp_path):
    repository = ExecutionRepository(
        tmp_path / "execution.sqlite",
        tmp_path / "artifacts",
        max_artifact_bytes=2 * 1024 * 1024,
    )
    owner = repository.installation_principal_id
    source_job, _ = repository.create_job(
        job_id="source-job",
        owner=owner,
        request_id="source-request",
        profile="artifact.transform.v1",
        payload={},
    )
    source = repository.publish_artifact(
        source_job.job_id,
        name="source.png",
        content=_image_bytes(),
        mime_type="image/png",
    )
    lifecycle = ExecutionLifecycle(
        repository,
        coordinator_factory=lambda repo: RecipeExecutionCoordinator(
            repo, lambda _job: _Attempt()
        ),
        health_check=RuntimeHealth.ready,
        enabled=True,
        profile="local",
    )
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=ALLOWED_HOSTS,
        execution_lifecycle=lifecycle,
        installation_principal_id=owner,
    )
    return app, source.artifact_id


def _payload(artifact_id: str, *, request_id: str = "api-recipe", steps=None) -> dict:
    return {
        "request_id": request_id,
        "source_artifact_id": artifact_id,
        "plan": {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": artifact_id,
            "steps": steps or [{"op": "grayscale"}],
            "output_format": "png",
        },
    }


def test_recipe_route_requires_a_ready_runtime_and_preserves_default_off(tmp_path):
    app = create_app(build_demo_dependencies(), allowed_hosts=ALLOWED_HOSTS)
    with TestClient(app) as client:
        headers = _session(client, app)
        response = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json=_payload("missing-artifact"),
        )
        assert response.status_code == 404
        assert "path" not in response.text.lower()
        attachment = client.post(
            "/api/v1/execution/attachments",
            headers=headers,
            json={
                "request_id": "api-attachment",
                "content_base64": base64.b64encode(_image_bytes()).decode("ascii"),
            },
        )
        assert attachment.status_code == 404


def test_recipe_route_is_owner_scoped_idempotent_and_reaches_existing_execution_surface(tmp_path):
    app, artifact_id = _app(tmp_path)
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json=_payload(artifact_id),
        )
        assert accepted.status_code == 202
        body = accepted.json()
        assert body["profile"] == "recipe.image.v1"

        duplicate = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json=_payload(artifact_id),
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == body["job_id"]

        for _ in range(200):
            current = client.get(
                f"/api/v1/execution/{body['job_id']}",
                headers=headers,
            )
            if current.json()["status"] == "succeeded":
                break
            time.sleep(0.005)
        assert current.status_code == 200
        assert current.json()["status"] == "succeeded"
        assert current.json()["result"]["mime_type"] == "image/png"
        assert current.json()["result"]["artifact_id"]
        assert "path" not in current.text.lower()

        conflicting = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json=_payload(
                artifact_id,
                steps=[{"op": "brightness", "factor": "1.1"}],
            ),
        )
        assert conflicting.status_code == 409
        assert conflicting.json()["detail"] == "Recipe request conflicts with an existing request."

        tasks = client.get(
            "/api/v1/execution/tasks?include_terminal=true",
            headers=headers,
        )
        assert tasks.status_code == 200
        assert tasks.json()["tasks"][0]["profile"] == "recipe.image.v1"


def test_attachment_stage_is_bounded_idempotent_and_returns_opaque_artifact(tmp_path):
    app, _artifact_id = _app(tmp_path)
    encoded = base64.b64encode(_image_bytes()).decode("ascii")
    with TestClient(app) as client:
        headers = _session(client, app)
        accepted = client.post(
            "/api/v1/execution/attachments",
            headers=headers,
            json={"request_id": "api-attachment", "content_base64": encoded},
        )
        assert accepted.status_code == 201
        body = accepted.json()
        assert body["profile"] == "attachment.stage.v1"
        assert body["status"] == "succeeded"
        assert body["mime_type"] == "image/png"
        assert body["artifact_id"]
        assert "path" not in accepted.text.lower()
        assert encoded not in str(app.state.execution_lifecycle.repository.get_job(body["job_id"]).payload)

        duplicate = client.post(
            "/api/v1/execution/attachments",
            headers=headers,
            json={"request_id": "api-attachment", "content_base64": encoded},
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["artifact_id"] == body["artifact_id"]

        conflict = client.post(
            "/api/v1/execution/attachments",
            headers=headers,
            json={"request_id": "api-attachment", "content_base64": base64.b64encode(b"other").decode("ascii")},
        )
        assert conflict.status_code == 409

        malformed = client.post(
            "/api/v1/execution/attachments",
            headers=headers,
            json={"request_id": "api-malformed", "content_base64": "not-base64!"},
        )
        assert malformed.status_code == 422


def test_recipe_route_rejects_mismatched_plan_and_foreign_artifact_without_leaks(tmp_path):
    app, artifact_id = _app(tmp_path)
    repository = app.state.execution_lifecycle.repository
    foreign_job, _ = repository.create_job(
        job_id="foreign-source",
        owner="f" * 64,
        request_id="foreign-source-request",
        profile="artifact.transform.v1",
        payload={},
    )
    foreign = repository.publish_artifact(
        foreign_job.job_id,
        name="foreign.png",
        content=_image_bytes(),
        mime_type="image/png",
    )

    with TestClient(app) as client:
        headers = _session(client, app)
        mismatch = _payload(artifact_id)
        mismatch["plan"]["input_artifact_id"] = "another-artifact"
        invalid = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json=mismatch,
        )
        assert invalid.status_code == 422

        foreign_response = client.post(
            "/api/v1/execution/recipe/image",
            headers=headers,
            json=_payload(foreign.artifact_id, request_id="foreign-request"),
        )
        assert foreign_response.status_code == 404
        assert "foreign-source" not in foreign_response.text
        assert "path" not in foreign_response.text.lower()
