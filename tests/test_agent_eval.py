"""End-to-end task benchmarks use only copied fixtures and the real file tools."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from cortex_backend.agent.loop import AgentLoop
from tools import agent_eval
from tools import agent_eval_replay


def test_check_passes_all_tasks_without_a_model(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_eval.main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["suite"] == "cortex-agent-tasks-v1"
    assert report["mode"] == "check"
    assert report["task_count"] == 6
    assert report["passed"] == 6 and report["failed"] == 0
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


@pytest.mark.parametrize("name", ["fix-failing-test", "commit-the-change"])
def test_new_tasks_pass_in_check_mode(name: str) -> None:
    report = agent_eval.run_suite(task=name)
    case = report["tasks"][0]
    assert report["passed"] == 1 and report["failed"] == 0
    assert case["passed"] is True
    assert case["checks"]["end_state"] is True
    assert case["checks"]["fixture_untouched"] is True
    assert case["tool_calls"] >= 2


def test_fix_requires_successful_post_edit_verify_loop() -> None:
    case = agent_eval.run_suite(task="fix-failing-test")["tasks"][0]
    assert case["checks"]["pytest_after_edit"] is True
    assert case["checks"]["pytest_command"] is True
    assert case["checks"]["verify_completed"] is True


def test_fix_fails_if_pre_final_verification_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # The resulting edited work copy is correct, but no pytest tool was run.
    def skip_checks(self: AgentLoop, run_id: str, state: object, answer: str):
        return self._complete_final(run_id, state, answer)  # type: ignore[arg-type]

    monkeypatch.setattr(AgentLoop, "_before_final_checks", skip_checks)
    case = agent_eval.run_suite(task="fix-failing-test")["tasks"][0]
    assert case["status"] == "completed"
    assert case["checks"]["end_state"] is True
    assert case["checks"]["pytest_after_edit"] is False
    assert case["checks"]["pytest_command"] is False
    assert case["passed"] is False


def test_commit_fails_if_workcopy_contains_two_new_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    original = agent_eval_replay.ReplayGitLauncher.commit

    def commit_twice(self: agent_eval_replay.ReplayGitLauncher, parent: str, message: str) -> None:
        original(self, parent, message)
        head = (self.root / ".git" / "refs" / "heads" / "main").read_text(encoding="ascii").strip()
        original(self, head, "Unexpected extra commit")

    monkeypatch.setattr(agent_eval_replay.ReplayGitLauncher, "commit", commit_twice)
    case = agent_eval.run_suite(task="commit-the-change")["tasks"][0]
    assert case["status"] == "completed"
    assert case["checks"]["end_state"] is True
    assert case["checks"]["committed_contents"] is True
    assert case["checks"]["git_commit_observed"] is True
    assert case["checks"]["one_new_commit"] is False
    assert case["passed"] is False


def test_new_tasks_have_check_results_in_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_eval.main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    tasks = {case["name"]: case for case in report["tasks"]}
    assert set(tasks) == set(agent_eval.TASK_NAMES)
    assert all(tasks[name]["passed"] for name in ("fix-failing-test", "commit-the-change"))
    assert tasks["fix-failing-test"]["checks"]["pytest_after_edit"] is True
    assert tasks["commit-the-change"]["checks"]["one_new_commit"] is True
    assert tasks["commit-the-change"]["checks"]["committed_contents"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="Windows supports real sandbox processes")
@pytest.mark.parametrize("name", ["fix-failing-test", "commit-the-change"])
def test_real_process_tasks_skip_non_windows_with_reason(name: str) -> None:
    case = agent_eval.run_suite(task=name, mode="model", model="not-installed")["tasks"][0]
    assert case["passed"] is False
    assert case["status"] == "skipped"
    assert case["stop_reason"] == "real_processes_require_windows"
    assert case["model_calls"] == 0 and case["tool_calls"] == 0
