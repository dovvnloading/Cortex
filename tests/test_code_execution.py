"""Approval and safety coverage for the local code harness."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Event

import pytest

from cortex_backend.execution.code_execution import (
    CodeCapabilities,
    CodeExecutionError,
    CodeExecutionRequest,
    CodeExecutionResult,
    code_worker_main,
    run_code_in_worker,
)
from cortex_backend.execution import code_execution
from cortex_backend.execution.local_runtime import LocalExecutionCoordinator
from cortex_backend.execution.repository import ExecutionRepository, LeaseConflict
from cortex_backend.services.llm import SynthesisAgent


def test_code_source_requires_bounded_constructs_and_explicit_capabilities() -> None:
    assert run_code_in_worker("total = 0\nfor i in range(4):\n total += i\n_result = total").value == 6
    assert run_code_in_worker("_result = float('nan')").value == "nan"
    with pytest.raises(CodeExecutionError, match="imports_not_allowed"):
        run_code_in_worker("import os")
    with pytest.raises(CodeExecutionError, match="PermissionError"):
        run_code_in_worker("_result = cortex.fs.listdir('.')")
    with pytest.raises(CodeExecutionError, match="attribute_not_allowed"):
        run_code_in_worker("_result = cortex.fs.__class__")
    with pytest.raises(CodeExecutionError, match="call_not_allowed"):
        run_code_in_worker("_result = cortex.fs.__class__.__subclasses__()")
    with pytest.raises(CodeExecutionError, match="attribute_not_allowed"):
        run_code_in_worker("cortex.fs.enabled = True\n_result = cortex.fs.listdir('.')")
    with pytest.raises(CodeExecutionError, match="capabilities_invalid"):
        run_code_in_worker("print('not run')", {"filesystem": "false"})
    with pytest.raises(CodeExecutionError, match="loop_work_too_large"):
        run_code_in_worker("_result = [(i, j) for i in range(10000) for j in range(10000)]")
    with pytest.raises(CodeExecutionError, match="sequence_too_large"):
        run_code_in_worker("_result = 'x' * 100001")
    with pytest.raises(CodeExecutionError, match="name_not_allowed"):
        run_code_in_worker("_result = __builtins__")
    with pytest.raises(CodeExecutionError, match="exponent_too_large"):
        run_code_in_worker("exponent = 1001\n_result = 2 ** exponent")
    allowed = run_code_in_worker(
        "_result = cortex.fs.listdir('.')",
        CodeCapabilities(filesystem=True).as_dict(),
    )
    assert isinstance(allowed.value, list)


def test_code_worker_announces_readiness_before_running_source(monkeypatch, tmp_path: Path) -> None:
    class _Connection:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []
            self.closed = False

        def send(self, message: dict[str, object]) -> None:
            self.messages.append(message)

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(code_execution, "_scrub_worker_environment", lambda: None)
    monkeypatch.setattr(
        code_execution,
        "run_code_in_worker",
        lambda *_args: CodeExecutionResult(stdout="ok\n", stderr=""),
    )

    code_worker_main(connection, "print('ok')", {}, str(tmp_path))

    assert connection.messages[0] == {"ok": True, "event": "ready"}
    assert connection.messages[1]["ok"] is True
    assert connection.closed is True


def test_brokered_filesystem_is_scoped_and_budgeted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("safe", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")

    with pytest.raises(CodeExecutionError, match="PermissionError"):
        run_code_in_worker(
            "_result = cortex.fs.read_text('../outside.txt')",
            {"filesystem": True},
            str(workspace),
        )
    with pytest.raises(CodeExecutionError, match="filesystem_limit"):
        run_code_in_worker(
            "for i in range(33):\n cortex.fs.listdir('.')",
            {"filesystem": True},
            str(workspace),
        )
    result = run_code_in_worker(
        "_result = cortex.fs.read_text('inside.txt')",
        {"filesystem": True},
        str(workspace),
    )
    assert result.value == "safe"


def test_network_broker_rejects_private_targets() -> None:
    with pytest.raises(CodeExecutionError, match="PermissionError"):
        run_code_in_worker(
            "_result = cortex.net.get('http://127.0.0.1:80')",
            {"network": True},
        )


def test_network_validation_returns_the_address_it_vetted(monkeypatch) -> None:
    """The vetted address must come back so the caller can dial it directly.

    Discarding it and handing the hostname to the HTTP stack is what allowed
    DNS rebinding: the stack resolved a second time, and a nameserver
    answering differently on that second lookup reached targets the check
    had just rejected.
    """
    def fake_getaddrinfo(host, port, *args, **kwargs):
        del host, port, args, kwargs
        return [(0, 0, 0, "", ("93.184.216.34", 80))]

    monkeypatch.setattr(code_execution.socket, "getaddrinfo", fake_getaddrinfo)

    url, pinned_ip = code_execution._validate_network_url("http://example.test/status")

    assert url == "http://example.test/status"
    assert pinned_ip == "93.184.216.34"


def test_network_broker_pins_the_vetted_address_against_dns_rebinding(monkeypatch) -> None:
    """A nameserver that answers public-then-private must not win.

    The first resolution passes validation; a second resolution (the one the
    HTTP stack would otherwise perform when it opens the socket) returns
    loopback. The connection must still be made to the first, vetted address
    -- never to the rebound one.
    """
    answers = [
        [(0, 0, 0, "", ("93.184.216.34", 80))],  # vetted: public
        [(0, 0, 0, "", ("127.0.0.1", 80))],      # rebound: loopback
    ]

    def rebinding_getaddrinfo(host, port, *args, **kwargs):
        del host, port, args, kwargs
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(code_execution.socket, "getaddrinfo", rebinding_getaddrinfo)

    _, pinned_ip = code_execution._validate_network_url("http://rebind.test/status")
    assert pinned_ip == "93.184.216.34"

    dialed: list[tuple[str, int]] = []

    def fake_create_connection(address, timeout=None, source_address=None):
        del timeout, source_address
        dialed.append(address)
        raise OSError("connection not actually made in this test")

    plain, _tls = code_execution._pinned_connection_classes(pinned_ip)
    connection = plain("rebind.test", 80)
    monkeypatch.setattr(connection, "_create_connection", fake_create_connection)
    with pytest.raises(OSError):
        connection.connect()

    assert dialed == [("93.184.216.34", 80)], (
        "the connection re-resolved instead of using the vetted address"
    )
    # The hostname is still what travels in Host / SNI, so servers and
    # certificate validation are unaffected by the pinning.
    assert connection.host == "rebind.test"


def test_code_execution_waits_for_one_time_approval_and_returns_structured_output(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner,
        request_id="code-approval",
        source="print('ok')\n_result = 2 + 2",
        intent_summary="Verify a small local calculation.",
    )
    job = coordinator.start_code(request)
    assert job.approval_state == "pending"
    time.sleep(0.1)
    assert repository.get_job(job.job_id).status == "queued"

    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    completed = coordinator.wait(job.job_id, timeout=5.0)
    assert completed.status == "succeeded"
    assert completed.result is not None
    assert completed.result["stdout"] == "ok\n"
    assert completed.result["value"] == 4
    assert completed.result["schema_version"] == "code.result.v1"
    event_count = len(repository.events(job.job_id))
    duplicate = coordinator.start_code(request)
    assert duplicate.job_id == job.job_id
    time.sleep(0.15)
    assert len(repository.events(job.job_id)) == event_count
    coordinator.shutdown()
    assert not (repository.artifact_root / ".code_workspaces" / job.job_id).exists()


def test_approval_is_bound_to_the_exact_source_digest(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-scope-mismatch",
            source="print('must fail closed')",
            intent_summary="Verify approval binding.",
        )
    )
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_approvals SET scope_digest = ? WHERE job_id = ?",
            ("0" * 64, job.job_id),
        )
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    completed = coordinator.wait(job.job_id, timeout=5.0)
    assert completed.status == "failed"
    assert completed.error == "approval_scope_mismatch"
    coordinator.shutdown()


def test_denied_code_never_starts(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository)
    owner = repository.installation_principal_id
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-denied",
            source="print('must not run')",
            intent_summary="Test denial.",
        )
    )
    repository.decide_approval(job.job_id, owner=owner, decision="denied")
    time.sleep(0.1)
    current = repository.get_job(job.job_id)
    assert current is not None
    assert current.status == "cancelled"
    assert current.result is None
    coordinator.shutdown()


def test_cancelling_pending_code_revokes_approval_and_finishes(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository)
    owner = repository.installation_principal_id
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-cancel-pending",
            source="print('must not run')",
            intent_summary="Test pending cancellation.",
        )
    )
    cancelled = coordinator.cancel(job.job_id, owner=owner)
    assert cancelled.status == "cancelled"
    assert cancelled.approval_state == "denied"
    coordinator.shutdown()


def test_pending_code_approval_expires_while_coordinator_is_live(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository)
    owner = repository.installation_principal_id
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-expire-live",
            source="print('must not run')",
            intent_summary="Test live approval expiry.",
        )
    )
    with repository.connect() as connection:
        connection.execute(
            "UPDATE execution_approvals SET expires_at = ? WHERE job_id = ?",
            ("2000-01-01T00:00:00+00:00", job.job_id),
        )
    for _ in range(100):
        current = repository.get_job(job.job_id)
        if current is not None and current.status == "cancelled":
            break
        time.sleep(0.01)
    assert current is not None
    assert current.status == "cancelled"
    assert current.approval_state == "expired"
    coordinator.shutdown()


def test_process_capability_fails_closed_before_a_job_is_created(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    with pytest.raises(CodeExecutionError, match="process_capability_unavailable"):
        CodeExecutionRequest(
            owner=owner,
            request_id="code-process",
            source="_result = cortex.process.run(['cmd', '/c', 'type', '../outside.txt'])",
            intent_summary="Read outside the brokered workspace.",
            capabilities=CodeCapabilities(process=True),
        )
    with pytest.raises(CodeExecutionError, match="process_capability_unavailable"):
        CodeExecutionRequest(
            owner=owner,
            request_id="code-process-without-grant",
            source="_result = cortex.process.run(['cmd', '/c', 'echo', 'worker'])",
            intent_summary="Try to reference the disabled process broker.",
        )
    with pytest.raises(CodeExecutionError, match="process_capability_unavailable"):
        run_code_in_worker(
            "_result = cortex.process.run(['cmd', '/c', 'echo', 'worker'])",
            {"process": True},
        )
    assert repository.list_jobs(owner=owner, include_terminal=True) == []
    coordinator.shutdown()


def test_cancellation_wins_if_requested_before_code_completion_commits(
    tmp_path, monkeypatch
) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    completion_started = Event()
    release_completion = Event()
    original_transition = repository.transition

    def delayed_transition(job_id, **kwargs):
        if kwargs.get("event") == "code.completed":
            completion_started.set()
            assert release_completion.wait(timeout=3.0)
        return original_transition(job_id, **kwargs)

    monkeypatch.setattr(repository, "transition", delayed_transition)
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-cancel-completion-race",
            source="_result = 42",
            intent_summary="Exercise the cancellation commit boundary.",
        )
    )
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    assert completion_started.wait(timeout=5.0)

    cancelling = coordinator.cancel(job.job_id, owner=owner)
    assert cancelling.status == "cancelling"
    release_completion.set()
    completed = coordinator.wait(job.job_id, timeout=5.0)

    assert completed.status == "cancelled"
    assert completed.result is None
    events = repository.events(job.job_id)
    assert events[-1].event == "code.cancelled"
    assert not any(event.event == "code.completed" for event in events)
    coordinator.shutdown()


def test_cancellation_wins_if_requested_before_code_failure_commits(
    tmp_path, monkeypatch
) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    failure_started = Event()
    release_failure = Event()
    original_transition = repository.transition

    def delayed_transition(job_id, **kwargs):
        if kwargs.get("event") == "code.failed":
            failure_started.set()
            assert release_failure.wait(timeout=3.0)
        return original_transition(job_id, **kwargs)

    monkeypatch.setattr(repository, "transition", delayed_transition)
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-cancel-failure-race",
            source="_result = 1 / 0",
            intent_summary="Exercise the failure commit boundary.",
        )
    )
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    assert failure_started.wait(timeout=5.0)

    cancelling = coordinator.cancel(job.job_id, owner=owner)
    assert cancelling.status == "cancelling"
    release_failure.set()
    completed = coordinator.wait(job.job_id, timeout=5.0)

    assert completed.status == "cancelled"
    assert completed.result is None
    events = repository.events(job.job_id)
    assert events[-1].event == "code.cancelled"
    assert not any(event.event == "code.failed" for event in events)
    coordinator.shutdown()


def test_live_supervisor_lease_is_renewed_until_shutdown(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    first = LocalExecutionCoordinator(repository, supervisor_lease_seconds=0.06)
    second = LocalExecutionCoordinator(repository, supervisor_lease_seconds=0.06)
    first.startup_recover()
    time.sleep(0.2)

    with pytest.raises(LeaseConflict, match="supervisor is already running"):
        second.startup_recover()

    first.shutdown()
    second.startup_recover()
    second.shutdown()


def test_supervisor_restart_waits_for_timed_out_heartbeat(
    tmp_path, monkeypatch
) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, supervisor_lease_seconds=0.06)
    original_claim = repository.claim_supervisor_lease
    renewal_entered = Event()
    release_renewal = Event()
    claim_count = 0

    def blocked_claim(*, lease_owner: str, ttl_seconds: float = 30.0) -> str:
        nonlocal claim_count
        claim_count += 1
        if claim_count == 2:
            renewal_entered.set()
            assert release_renewal.wait(timeout=3.0)
        return original_claim(lease_owner=lease_owner, ttl_seconds=ttl_seconds)

    monkeypatch.setattr(repository, "claim_supervisor_lease", blocked_claim)
    coordinator.startup_recover()
    assert renewal_entered.wait(timeout=3.0)
    coordinator.shutdown(timeout=0)

    with pytest.raises(RuntimeError, match="still stopping"):
        coordinator.startup_recover()
    assert claim_count == 2

    release_renewal.set()
    old_thread = coordinator._supervisor_thread
    assert old_thread is not None
    old_thread.join(timeout=3.0)
    assert not old_thread.is_alive()

    coordinator.startup_recover()
    assert coordinator._supervisor_thread is not old_thread
    coordinator.shutdown()


def test_foreign_live_job_lease_does_not_mark_code_as_failed(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-foreign-live-lease",
            source="_result = 42",
            intent_summary="Do not overwrite another coordinator.",
        )
    )
    repository.claim_lease(
        job.job_id,
        lease_owner="foreign-live-coordinator",
        ttl_seconds=3.0,
    )
    foreign_workspace = repository.artifact_root / ".code_workspaces" / job.job_id
    foreign_workspace.mkdir(parents=True)
    sentinel = foreign_workspace / "foreign-owner.txt"
    sentinel.write_text("in use", encoding="utf-8")
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    for _ in range(200):
        with coordinator._code_lock:
            active = job.job_id in coordinator._code_threads
        if not active:
            break
        time.sleep(0.005)

    untouched = repository.get_job(job.job_id)
    assert untouched is not None
    assert untouched.status == "queued"
    assert untouched.error is None
    assert not any(event.event == "code.failed" for event in repository.events(job.job_id))
    assert sentinel.read_text(encoding="utf-8") == "in use"

    repository.release_lease(
        job.job_id,
        lease_owner="foreign-live-coordinator",
    )
    coordinator._launch_code(job.job_id)
    completed = coordinator.wait(job.job_id, timeout=5.0)
    assert completed.status == "succeeded"
    coordinator.shutdown()


def test_code_workspace_cleanup_happens_before_lease_release(
    tmp_path, monkeypatch
) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    cleanup_observed = Event()
    cleanup_had_lease: list[bool] = []
    original_cleanup = coordinator._cleanup_code_workspace

    def tracked_cleanup(job_id: str) -> None:
        with repository.connect() as connection:
            lease = connection.execute(
                "SELECT 1 FROM execution_leases WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        cleanup_had_lease.append(lease is not None)
        original_cleanup(job_id)
        cleanup_observed.set()

    monkeypatch.setattr(coordinator, "_cleanup_code_workspace", tracked_cleanup)
    job = coordinator.start_code(
        CodeExecutionRequest(
            owner=owner,
            request_id="code-cleanup-lease-order",
            source="_result = 42",
            intent_summary="Keep cleanup fenced by the job lease.",
        )
    )
    repository.decide_approval(job.job_id, owner=owner, decision="approved")

    completed = coordinator.wait(job.job_id, timeout=5.0)
    assert completed.status == "succeeded"
    assert cleanup_observed.wait(timeout=3.0)
    assert cleanup_had_lease == [True]
    lease_released = False
    for _ in range(300):
        with repository.connect() as connection:
            lease_released = connection.execute(
                "SELECT 1 FROM execution_leases WHERE job_id = ?",
                (job.job_id,),
            ).fetchone() is None
        if lease_released:
            break
        time.sleep(0.01)
    assert lease_released
    coordinator.shutdown()


def test_model_only_proposes_code_from_the_structured_envelope() -> None:
    agent = SynthesisAgent("model", "model", "model", object(), code_execution_eligible=True)
    visible, _, _ = agent._parse_and_clean_response(
        "Here is the plan.\n<code_execution_request>{\"language\":\"python\",\"source\":\"print('x')\",\"intent_summary\":\"Print x\",\"capabilities\":{}}</code_execution_request>",
        None,
    )
    assert visible == "Here is the plan."
    assert agent.last_code_proposal is not None
    assert agent.last_code_proposal.source == "print('x')"

    agent._parse_and_clean_response("```python\nprint('not a request')\n```", None)
    assert agent.last_code_proposal is None


def test_model_proposal_drops_unused_capabilities() -> None:
    agent = SynthesisAgent("model", "model", "model", object(), code_execution_eligible=True)
    visible, _, _ = agent._parse_and_clean_response(
        "<code_execution_request>{\"language\":\"python\",\"source\":\"print('x')\",\"intent_summary\":\"Print x\",\"capabilities\":{\"filesystem\":true,\"process\":true,\"network\":true}}</code_execution_request>",
        None,
    )
    assert visible == ""
    assert agent.last_code_proposal is not None
    assert agent.last_code_proposal.capabilities == {
        "filesystem": False,
        "process": False,
        "network": False,
    }


def test_model_proposal_rejects_the_unavailable_process_capability() -> None:
    agent = SynthesisAgent("model", "model", "model", object(), code_execution_eligible=True)
    agent._parse_and_clean_response(
        "<code_execution_request>{\"language\":\"python\",\"source\":\"_result = cortex.process.run(['cmd', '/c', 'echo', 'x'])\",\"intent_summary\":\"Run x\",\"capabilities\":{\"process\":true}}</code_execution_request>",
        None,
    )
    assert agent.last_code_proposal is None

    malformed, _, _ = agent._parse_and_clean_response(
        "<code_execution_request>{not-json}</code_execution_request>", None,
    )
    assert "code_execution_request" in malformed
    assert agent.last_code_proposal is None
