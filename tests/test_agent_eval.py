"""End-to-end task benchmarks use only copied fixtures and the real file tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import agent_eval


def test_check_passes_all_tasks_without_a_model(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_eval.main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["suite"] == "cortex-agent-tasks-v1"
    assert report["mode"] == "check"
    assert report["task_count"] == 4
    assert report["passed"] == 4 and report["failed"] == 0
    assert {task["name"] for task in report["tasks"]} == set(agent_eval.TASK_NAMES)
    assert all(task["passed"] and all(task["checks"].values()) for task in report["tasks"])


def test_wrong_final_state_is_a_failed_task(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = dict(agent_eval.SCRIPTED_REPLIES)
    replies["single-file-edit"] = (agent_eval._final("Done, without any edits."),)
    monkeypatch.setattr(agent_eval, "SCRIPTED_REPLIES", replies)
    assert agent_eval.main(["--check", "--task", "single-file-edit", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1 and report["passed"] == 0
    assert report["tasks"][0]["status"] == "completed"
    assert report["tasks"][0]["checks"]["end_state"] is False
    assert report["tasks"][0]["passed"] is False


def test_outside_workcopy_write_is_denied_and_task_refused() -> None:
    report = agent_eval.run_suite(task="outside-workcopy-refusal")
    case = report["tasks"][0]
    assert case["passed"] is True
    assert case["checks"]["boundary_denied"] is True
    assert case["checks"]["refusal"] is True
    assert case["checks"]["outside_guard"] is True
    assert case["checks"]["end_state"] is True
    assert case["tool_calls"] == 1


def test_json_output_reports_steps_and_wall_time(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_eval.main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"suite", "mode", "task_count", "passed", "failed", "tasks"}
    for task in report["tasks"]:
        assert set(task) == {
            "name", "passed", "status", "stop_reason", "steps", "model_calls", "tool_calls",
            "wall_seconds", "checks", "error_kind",
        }
        assert task["status"] == "completed"
        assert task["stop_reason"] == "final_answer"
        assert type(task["steps"]) is int and task["steps"] >= 2
        assert type(task["model_calls"]) is int and task["model_calls"] >= 2
        assert type(task["tool_calls"]) is int and task["tool_calls"] >= 1
        assert type(task["wall_seconds"]) is float and task["wall_seconds"] >= 0
        assert task["error_kind"] is None


def test_check_never_instantiates_a_local_network_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no local or remote model requests are allowed in --check")

    monkeypatch.setattr(agent_eval.LoopbackOllamaClient, "__init__", forbidden)
    assert agent_eval.run_suite()["failed"] == 0


def test_fixture_sources_are_unchanged_after_evaluation() -> None:
    source = Path(agent_eval.FIXTURE_ROOT)
    baseline = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert agent_eval.run_suite()["failed"] == 0
    assert {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()} == baseline


def test_invalid_model_name_or_task_is_rejected_without_running(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as invalid:
        agent_eval.main(["--model", "https://example.com/model", "--json"])
    assert invalid.value.code == 2
    with pytest.raises(SystemExit) as unknown:
        agent_eval.main(["--check", "--task", "not-a-task"])
    assert unknown.value.code == 2
    assert not capsys.readouterr().out


def test_model_transport_is_loopback_only_and_never_pulls(monkeypatch: pytest.MonkeyPatch) -> None:
    destinations: list[tuple[str, int]] = []
    requests: list[tuple[str, str]] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def read(self, _limit: int) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    class FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: int) -> None:
            destinations.append((host, port))
            assert timeout > 0

        def request(self, method: str, path: str, **_kwargs: object) -> None:
            requests.append((method, path))

        def getresponse(self) -> FakeResponse:
            if requests[-1][1] == "/api/tags":
                return FakeResponse({"models": [{"name": "synthetic:latest"}]})
            return FakeResponse({"message": {"content": "{}"}})

        def close(self) -> None:
            pass

    monkeypatch.setattr(agent_eval, "HTTPConnection", FakeConnection)
    client = agent_eval.LoopbackOllamaClient("synthetic:latest")
    assert client.chat(messages=[], options={}) == {"message": {"content": "{}"}}
    assert destinations == [("127.0.0.1", 11434)] * 2
    assert requests == [("GET", "/api/tags"), ("POST", "/api/chat")]
