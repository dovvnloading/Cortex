"""Cancellation of stalled Ollama model-pull streams and registry admission.

The real Ollama client owns an httpx response inside its pull generator.
Closing a dedicated client from a watcher must interrupt an in-flight read
rather than blocking the models job slot until a 600-second HTTP timeout.
"""

from __future__ import annotations

from collections.abc import Iterator
from threading import Event, Thread

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.services.models import ModelService
from cortex_backend.testing import build_demo_dependencies
from support import session_headers, wait_until


class _SharedGateway:
    """Only used for safe shared inventory reads, never the streaming pull."""

    def list(self):
        return {"models": [{"name": "fast:latest"}]}

    def pull(self, *_args, **_kwargs):
        raise AssertionError("The shared inventory client must not pull a model.")

    def show(self, _model):
        return {"capabilities": []}


class _DedicatedGateway:
    def __init__(self, *, blocks: bool):
        self.blocks = blocks
        self.closed = Event()
        self.waiting_for_chunk = Event()
        self.close_calls = 0

    def pull(self, model: str, *, stream: bool = False) -> Iterator[dict]:
        assert stream and model
        yield {"status": "pulling manifest"}
        if not self.blocks:
            yield {"status": "success"}
            return
        self.waiting_for_chunk.set()
        if not self.closed.wait(timeout=8):
            raise AssertionError("The blocked HTTP read was never interrupted.")
        # Closing the underlying client intentionally aborts the response read.
        raise RuntimeError("The HTTP connection was closed by Stop.")

    def close(self):
        self.close_calls += 1
        self.closed.set()


def test_stopped_ollama_pull_interrupts_the_blocking_next_and_releases_its_client():
    shared = _SharedGateway()
    slow = _DedicatedGateway(blocks=True)
    fast = _DedicatedGateway(blocks=False)
    queued = iter((slow, fast))
    service = ModelService(shared, pull_client_factory=lambda: next(queued))
    cancel = Event()
    updates = []
    result = []

    worker = Thread(
        target=lambda: result.append(
            service.pull_model("slow:latest", cancellation_event=cancel, verify=False, progress_callback=updates.append)
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert slow.waiting_for_chunk.wait(timeout=5), "The pull never reached its blocked read."
        assert updates and updates[0].status == "pulling manifest"
        cancel.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive(), "Stop must interrupt the read well before the Ollama timeout."
        assert result == [False]
        assert slow.closed.is_set()
        # Verify that cancellation never closed the shared inventory client
        # and the model service can accept another operation straight away.
        assert service.pull_model("fast:latest", cancellation_event=Event(), verify=True) is True
        assert fast.close_calls >= 1
        assert shared.list()["models"][0]["name"] == "fast:latest"
    finally:
        cancel.set()
        slow.close()
        worker.join(timeout=2.0)


def test_cancel_before_pull_skips_creating_its_http_client():
    clients = []
    service = ModelService(_SharedGateway(), pull_client_factory=lambda: clients.append(_DedicatedGateway(blocks=True)))
    cancel = Event()
    cancel.set()
    assert service.pull_model("slow:latest", cancellation_event=cancel) is False
    assert clients == []


def test_cancelled_model_pull_job_frees_models_slot_for_the_next_pull():
    slow = _DedicatedGateway(blocks=True)
    fast = _DedicatedGateway(blocks=False)
    queued = iter((slow, fast))
    dependencies = build_demo_dependencies()
    dependencies.models = ModelService(_SharedGateway(), pull_client_factory=lambda: next(queued))
    app = create_app(dependencies, allowed_hosts=("testserver",))

    try:
        with TestClient(app) as client:
            headers = session_headers(client, app)
            accepted = client.post("/api/v1/models/pulls", json={"model": "slow:latest"}, headers=headers)
            assert accepted.status_code == 202
            job_id = accepted.json()["job_id"]
            assert slow.waiting_for_chunk.wait(timeout=5), "Stalled pull never started."

            cancel = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers)
            assert cancel.status_code == 200

            def cancelled() -> bool:
                return client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()["status"] == "cancelled"

            wait_until(cancelled, describe="the stalled model pull to cancel")
            assert slow.closed.is_set()
            accepted_again = client.post(
                "/api/v1/models/pulls", json={"model": "fast:latest"}, headers=headers
            )
            assert accepted_again.status_code == 202

            def succeeded() -> bool:
                status = client.get(
                    f"/api/v1/jobs/{accepted_again.json()['job_id']}", headers=headers
                )
                return status.json()["status"] == "succeeded"

            wait_until(succeeded, describe="a new model pull after cancellation")
            assert fast.closed.is_set()
    finally:
        slow.close()
        fast.close()


class _InventoryWithout:
    """A shared client whose inventory never lists the pulled tag."""

    def list(self):
        return {"models": [{"name": "other:latest"}]}

    def pull(self, *_args, **_kwargs):
        raise AssertionError("The shared inventory client must not pull a model.")

    def show(self, _model):
        return {"capabilities": []}


def test_a_pull_whose_tag_is_then_missing_fails_instead_of_succeeding():
    """Verification failure returned the same False as Stop, and was recorded as success (#628)."""

    dependencies = build_demo_dependencies()
    dependencies.models = ModelService(
        _InventoryWithout(), pull_client_factory=lambda: _DedicatedGateway(blocks=False)
    )
    app = create_app(dependencies, allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session_headers(client, app)
        accepted = client.post("/api/v1/models/pulls", json={"model": "example:latest"}, headers=headers)
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]

        def finished():
            body = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
            return body if body["status"] in {"succeeded", "failed", "cancelled"} else None

        wait_until(lambda: finished() is not None, describe="the pull job to finish")
        body = finished()

    assert body["status"] == "failed"
    assert "does not list the requested tag" in (body.get("error") or "")


class _StalledInventory:
    """A shared client whose inventory read hangs until released."""

    def __init__(self):
        self.reading = Event()
        self.release = Event()

    def list(self):
        self.reading.set()
        self.release.wait(timeout=10)
        return {"models": []}

    def pull(self, *_args, **_kwargs):
        raise AssertionError("The shared inventory client must not pull a model.")

    def show(self, _model):
        return {"capabilities": []}


def test_stop_ends_a_model_check_stalled_on_the_inventory_read():
    """The check's inventory read could not be interrupted by Stop (#629)."""

    stalled = _StalledInventory()
    service = ModelService(stalled)
    cancel = Event()
    result = []
    worker = Thread(
        target=lambda: result.append(
            service.check(required_models=["fast:latest"], cancellation_event=cancel)
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert stalled.reading.wait(timeout=5)
        cancel.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive(), "Stop must end the check without waiting for the read"
        assert result[0].details == "cancelled"
    finally:
        stalled.release.set()


def test_stop_ends_a_pull_stalled_on_its_final_verification():
    stalled = _StalledInventory()
    service = ModelService(stalled, pull_client_factory=lambda: _DedicatedGateway(blocks=False))
    cancel = Event()
    result = []
    worker = Thread(
        target=lambda: result.append(
            service.pull_model("fast:latest", cancellation_event=cancel, verify=True)
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert stalled.reading.wait(timeout=5)
        cancel.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()
        assert result == [False]
    finally:
        stalled.release.set()
