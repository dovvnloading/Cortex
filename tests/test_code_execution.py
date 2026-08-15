"""Approval and safety coverage for the local code harness."""

from __future__ import annotations

import time
from pathlib import Path

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
from cortex_backend.execution.repository import ExecutionRepository
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


def test_approved_process_capability_runs_through_the_worker(tmp_path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = LocalExecutionCoordinator(repository, code_timeout_seconds=3.0)
    owner = repository.installation_principal_id
    request = CodeExecutionRequest(
        owner=owner,
        request_id="code-process",
        source="_result = cortex.process.run(['cmd', '/c', 'echo', 'worker'])",
        intent_summary="Run one approved local process.",
        capabilities=CodeCapabilities(process=True),
    )
    job = coordinator.start_code(request)
    repository.decide_approval(job.job_id, owner=owner, decision="approved")
    completed = coordinator.wait(job.job_id, timeout=5.0)
    assert completed.status == "succeeded"
    assert completed.result is not None
    assert completed.result["value"]["stdout"].strip() == "worker"
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


def test_model_proposal_keeps_referenced_capability() -> None:
    agent = SynthesisAgent("model", "model", "model", object(), code_execution_eligible=True)
    agent._parse_and_clean_response(
        "<code_execution_request>{\"language\":\"python\",\"source\":\"_result = cortex.process.run(['cmd', '/c', 'echo', 'x'])\",\"intent_summary\":\"Run x\",\"capabilities\":{\"process\":true}}</code_execution_request>",
        None,
    )
    assert agent.last_code_proposal is not None
    assert agent.last_code_proposal.capabilities == {
        "filesystem": False,
        "process": True,
        "network": False,
    }

    malformed, _, _ = agent._parse_and_clean_response(
        "<code_execution_request>{not-json}</code_execution_request>", None,
    )
    assert "code_execution_request" in malformed
    assert agent.last_code_proposal is None
