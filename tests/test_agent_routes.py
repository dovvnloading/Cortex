"""Authenticated, opt-in agent run routes with synthetic work copies and loops."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from cortex_backend.agent.loop import LoopResult
from cortex_backend.agent.run_store import AgentRunStore
from cortex_backend.api import create_app
from cortex_backend.core.settings import AgentSettings
from cortex_backend.testing import build_demo_dependencies
from support import parse_sse_events, session_headers, wait_until


ROOT = "/api/v1/agent/runs"
SOURCE = b"value = 42\n"
STAGED = b"value = 43\n"


class FakeAgentLoop:
    """Complete or park a run without model I/O or checking out real files."""

    calls: list[str] = []
    started = Event()
    release = Event()

    def __init__(
        self, store: AgentRunStore, _client: object, model: str,
        registry: object, budgets: object, *, owner: str, goal: str,
        after_tool: object = None, **_kwargs: object,
    ) -> None:
        del registry, budgets, after_tool
        self.store = store
        self.owner = owner
        self.model = model
        self.goal = goal
        self.run_id = ""
        self.cancellation = Event()

    def run(self, run_id: str, *, goal: str | None = None) -> LoopResult:
        assert goal is None or goal == self.goal
        self.run_id = run_id
        self.calls.append("run")
        self.store.transition(run_id, owner=self.owner, status="running")
        record = self.store.get_run(run_id, owner=self.owner)
        assert record is not None
        # The only edit this fake performs is inside the disposable work copy.
        target = Path(record.workcopy) / "app.py"
        target.write_bytes(STAGED)
        self.started.set()
        if self.model == "blocking":
            self.release.wait(timeout=5)
        if self.cancellation.is_set():
            self.store.transition(run_id, owner=self.owner, status="cancelled", stop_reason="cancelled")
            return LoopResult(run_id, "cancelled", "cancelled", None, 0, 0)
        if self.model == "approval":
            step = self.store.append_step(
                run_id, owner=self.owner, kind="tool", summary="Tool needs approval",
                tool="replace_file", ended=False,
            )
            self.store.request_approval(
                run_id, owner=self.owner, step_seq=step.seq,
                request={"tool": "replace_file", "reason": "approval_required"},
            )
            return LoopResult(
                run_id, "waiting_approval", None, None, 1, 1,
                approval_request={"tool": "replace_file", "path": "app.py"},
            )
        self.store.append_step(run_id, owner=self.owner, kind="model", summary="Validated model turn")
        self.store.transition(run_id, owner=self.owner, status="completed", stop_reason="final_answer")
        return LoopResult(run_id, "completed", "final_answer", "Synthetic answer", 1, 0)

    def resume(self, run_id: str) -> LoopResult:
        self.calls.append("resume")
        approved = self.store.get_approval(run_id, owner=self.owner, step_seq=1)
        assert approved is not None and approved.decision in {"approved", "rejected"}
        self.store.finish_step(run_id, owner=self.owner, step_seq=1, result={"outcome": approved.decision})
        self.store.transition(run_id, owner=self.owner, status="completed", stop_reason="final_answer")
        return LoopResult(run_id, "completed", "final_answer", "Resumed", 1, 1)

    def cancel(self, run_id: str) -> None:
        self.calls.append("cancel")
        self.cancellation.set()
        current = self.store.get_run(run_id, owner=self.owner)
        assert current is not None
        if current.status == "waiting_approval":
            self.store.transition(run_id, owner=self.owner, status="cancelled", stop_reason="cancelled")
        self.release.set()


@pytest.fixture(autouse=True)
def clear_fake_loop() -> None:
    FakeAgentLoop.calls = []
    FakeAgentLoop.started = Event()
    FakeAgentLoop.release = Event()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "source-project"
    root.mkdir()
    (root / "app.py").write_bytes(SOURCE)
    return root


def _app(tmp_path: Path, *, enabled: bool = True, run_store: AgentRunStore | None = None):
    dependencies = build_demo_dependencies()
    if enabled:
        settings = dependencies.settings.load().settings
        dependencies.settings.save(settings.model_copy(update={"agent": AgentSettings(enabled=True)}))
    app = create_app(
        dependencies,
        allowed_hosts=("testserver",),
        agent_data_dir=tmp_path / "private-data",
        agent_run_store=run_store,
        agent_chat_client=object(),
        agent_loop_factory=FakeAgentLoop,
    )
    return app


def _start(project: Path, *, model: str = "local-model", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_root": str(project),
        "goal": "Change only the disposable copy.",
        "mode": "ask",
        "model": model,
        "budgets": {"max_model_calls": 2},
        "confirmed_project": True,
    }
    payload.update(overrides)
    return payload


def _run_done(client: TestClient, headers: dict[str, str], run_id: str, status: str = "completed") -> None:
    wait_until(
        lambda: client.get(f"{ROOT}/{run_id}", headers=headers).json()["status"] == status,
        describe=f"agent run {status}",
    )


def _second_session(client: TestClient, app) -> dict[str, str]:
    bootstrap, _expiry = app.state.session_manager.issue_bootstrap_token()
    result = client.post("/api/v1/session/exchange", json={"bootstrap_token": bootstrap})
    assert result.status_code == 200
    return {"Authorization": f"Bearer {result.json()['session_token']}"}


def _assert_code(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
    assert "/" not in response.json()["detail"]["message"]


def test_default_disabled_all_endpoints_are_forbidden_and_bearer_required(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path, enabled=False)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        assert app.state.dependencies.settings.load().settings.agent.enabled is False
        requests = (
            ("post", ROOT, _start(project)),
            ("get", ROOT, None),
            ("get", ROOT + "/missing", None),
            ("get", ROOT + "/missing/events", None),
            ("post", ROOT + "/missing/approvals/1", {"decision": "deny"}),
            ("post", ROOT + "/missing/cancel", None),
            ("get", ROOT + "/missing/changeset", None),
            ("post", ROOT + "/missing/accept", {"paths": ["app.py"], "confirmed": True}),
            ("post", ROOT + "/missing/undo", {"confirmed": True}),
            ("post", ROOT + "/missing/discard", None),
            ("post", ROOT + "/missing/resume", None),
        )
        for method, target, payload in requests:
            no_auth = client.request(method, target, json=payload)
            assert no_auth.status_code == 401, target
            response = client.request(method, target, headers=headers, json=payload)
            _assert_code(response, 403, "agent_disabled")
        assert not (tmp_path / "private-data").exists()


def test_settings_persisted_flag_controls_access(tmp_path: Path) -> None:
    app = _app(tmp_path, enabled=False)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        before = client.get("/api/v1/settings", headers=headers).json()["settings"]
        assert before["agent"] == {"enabled": False}
        before["agent"]["enabled"] = True
        saved = client.put("/api/v1/settings", headers=headers, json={
            "settings": before, "expected_revision": before["revision"],
        })
        assert saved.status_code == 200
        assert client.get("/api/v1/settings", headers=headers).json()["settings"]["agent"]["enabled"] is True
        assert client.get(ROOT, headers=headers).json() == {"runs": []}
        current = saved.json()["settings"]
        current["agent"]["enabled"] = False
        assert client.put("/api/v1/settings", headers=headers, json={
            "settings": current, "expected_revision": current["revision"],
        }).status_code == 200
        _assert_code(client.get(ROOT, headers=headers), 403, "agent_disabled")


def test_start_consent_cloud_budget_and_path_errors_are_path_free(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        _assert_code(client.post(ROOT, headers=headers, json=_start(
            project, confirmed_project=False,
        )), 403, "consent_required")
        _assert_code(client.post(ROOT, headers=headers, json=_start(
            project, model="cloud:openai:gpt", confirmed_cloud=False,
        )), 403, "cloud_consent_required")
        _assert_code(client.post(ROOT, headers=headers, json=_start(
            project, budgets={"unbounded": 1},
        )), 422, "invalid_budgets")
        invalid = tmp_path / "a-private-absolute-project"
        _assert_code(client.post(ROOT, headers=headers, json=_start(
            invalid,
        )), 422, "invalid_root")
        assert str(invalid) not in client.get(ROOT, headers=headers).text
        assert FakeAgentLoop.calls == []
        response = client.post(ROOT, headers=headers, json=_start(
            project, model="cloud:openai:gpt", confirmed_cloud=True,
        ))
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        _run_done(client, headers, run_id)
        assert (project / "app.py").read_bytes() == SOURCE
        assert app.state.agent_workflow._ledger().get_run(run_id, owner="other-owner") is None


def test_create_list_get_events_replay_and_changeset(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        created = client.post(ROOT, headers=headers, json=_start(project, mode="plan_only"))
        assert created.status_code == 202
        run = created.json()
        assert run["mode"] == "plan_only" and run["status"] in {"pending", "running", "completed"}
        assert run["budgets"]["max_model_calls"] == 2
        run_id = run["id"]
        _run_done(client, headers, run_id)
        listed = client.get(ROOT, headers=headers).json()["runs"]
        assert [item["id"] for item in listed] == [run_id]
        assert client.get(f"{ROOT}/{run_id}", headers=headers).json()["status"] == "completed"
        stream = client.get(f"{ROOT}/{run_id}/events", headers=headers)
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = parse_sse_events(stream.text)
        assert {item["event"] for item in events} >= {"step", "status", "output"}
        assert [item["event_id"] for item in events] == list(range(1, len(events) + 1))
        replay = client.get(
            f"{ROOT}/{run_id}/events", headers={**headers, "Last-Event-ID": "1"},
        )
        assert [item["event_id"] for item in parse_sse_events(replay.text)] == [
            item["event_id"] for item in events[1:]
        ]
        _assert_code(client.get(
            f"{ROOT}/{run_id}/events", headers={**headers, "Last-Event-ID": "../bad"},
        ), 400, "invalid_cursor")
        _assert_code(client.get(f"{ROOT}/missing", headers=headers), 404, "not_found")
        _assert_code(client.get(f"{ROOT}/missing/events", headers=headers), 404, "not_found")
        changes = client.get(f"{ROOT}/{run_id}/changeset", headers=headers).json()["changes"]
        assert len(changes) == 1
        assert changes[0]["path"] == "app.py"
        assert changes[0]["kind"] == "modify"
        assert "-value = 42" in changes[0]["diff"]
        assert "+value = 43" in changes[0]["diff"]
        assert (project / "app.py").read_bytes() == SOURCE


def test_command_output_and_agent_answer_have_distinct_provenance(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        created = client.post(ROOT, headers=headers, json=_start(project, model="blocking"))
        assert created.status_code == 202
        run_id = created.json()["id"]
        assert FakeAgentLoop.started.wait(5)
        hostile = '<a href="https://hostile.test">Claimed agent answer</a>'
        workflow = app.state.agent_workflow
        workflow._shell_output(run_id, hostile, "call-7")
        workflow._shell_output(run_id, "second chunk", "call-7")
        FakeAgentLoop.release.set()
        _run_done(client, headers, run_id)

        events = parse_sse_events(client.get(f"{ROOT}/{run_id}/events", headers=headers).text)
        outputs = [event for event in events if event["event"] == "output"]
        assert [event["data"] for event in outputs] == [
            {"text": hostile, "source": "command", "tool_call_id": "call-7"},
            {"text": "second chunk", "source": "command", "tool_call_id": "call-7"},
            {"text": "Synthetic answer", "source": "agent"},
        ]
        assert [event["event_id"] for event in events] == list(range(1, len(events) + 1))
        cursor = outputs[0]["event_id"]
        replay = parse_sse_events(client.get(
            f"{ROOT}/{run_id}/events", headers={**headers, "Last-Event-ID": str(cursor)},
        ).text)
        assert [event["event_id"] for event in replay] == [
            event["event_id"] for event in events if event["event_id"] > cursor
        ]


def test_second_run_conflicts_and_cancel_releases_one_worker(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        first = client.post(ROOT, headers=headers, json=_start(project, model="blocking"))
        assert first.status_code == 202
        assert FakeAgentLoop.started.wait(5)
        _assert_code(client.post(ROOT, headers=headers, json=_start(project)), 409, "busy")
        blocked_run = first.json()["id"]
        assert client.get(f"{ROOT}/{blocked_run}", headers=headers).status_code == 200
        cancellation = client.post(f"{ROOT}/{blocked_run}/cancel", headers=headers)
        assert cancellation.status_code == 200
        _run_done(client, headers, blocked_run, "cancelled")
        wait_until(lambda: app.state.agent_workflow._active_id is None)
        next_run = client.post(ROOT, headers=headers, json=_start(project))
        assert next_run.status_code == 202
        _run_done(client, headers, next_run.json()["id"])
        assert (project / "app.py").read_bytes() == SOURCE


def test_approval_replay_decision_and_resume(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        created = client.post(ROOT, headers=headers, json=_start(project, model="approval"))
        assert created.status_code == 202
        run_id = created.json()["id"]
        _run_done(client, headers, run_id, "waiting_approval")
        wait_until(lambda: not app.state.agent_workflow._live[run_id].driving)
        events = parse_sse_events(client.get(f"{ROOT}/{run_id}/events", headers=headers).text)
        approvals = [event for event in events if event["event"] == "approval"]
        assert len(approvals) == 1 and approvals[0]["data"]["seq"] == 1
        _assert_code(client.post(ROOT, headers=headers, json=_start(project)), 409, "busy")
        _assert_code(client.post(f"{ROOT}/{run_id}/resume", headers=headers), 409, "approval_required")
        _assert_code(client.post(f"{ROOT}/{run_id}/approvals/9", headers=headers, json={
            "decision": "allow_once",
        }), 409, "approval_unavailable")
        decision = client.post(f"{ROOT}/{run_id}/approvals/1", headers=headers, json={"decision": "deny"})
        assert decision.status_code == 200 and decision.json()["status"] == "running"
        _assert_code(client.post(f"{ROOT}/{run_id}/approvals/1", headers=headers, json={
            "decision": "allow_once",
        }), 409, "approval_unavailable")
        resumed = client.post(f"{ROOT}/{run_id}/resume", headers=headers)
        assert resumed.status_code == 200
        _run_done(client, headers, run_id)
        assert "resume" in FakeAgentLoop.calls
        _assert_code(client.post(f"{ROOT}/{run_id}/resume", headers=headers), 409, "invalid_state")
        assert (project / "app.py").read_bytes() == SOURCE


def test_expired_approvals_fail_closed_and_free_the_worker(tmp_path: Path) -> None:
    project = _project(tmp_path)
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    store = AgentRunStore(
        tmp_path / "private-data" / "agent" / "runs.sqlite", clock=lambda: now[0],
    )
    app = _app(tmp_path, run_store=store)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        first = client.post(ROOT, headers=headers, json=_start(project, model="approval")).json()["id"]
        _run_done(client, headers, first, "waiting_approval")
        wait_until(lambda: not app.state.agent_workflow._live[first].driving)
        now[0] += timedelta(seconds=301)
        _assert_code(client.post(f"{ROOT}/{first}/approvals/1", headers=headers, json={
            "decision": "allow_once",
        }), 409, "approval_expired")
        assert client.get(f"{ROOT}/{first}", headers=headers).json()["status"] == "failed"

        second = client.post(ROOT, headers=headers, json=_start(project, model="approval"))
        assert second.status_code == 202
        second_id = second.json()["id"]
        _run_done(client, headers, second_id, "waiting_approval")
        wait_until(lambda: not app.state.agent_workflow._live[second_id].driving)
        now[0] += timedelta(seconds=301)
        assert client.get(f"{ROOT}/{second_id}", headers=headers).json()["status"] == "failed"
        events = parse_sse_events(client.get(f"{ROOT}/{second_id}/events", headers=headers).text)
        assert any(event["event"] == "approval" and event["data"].get("decision") == "expired"
                   for event in events)
        third = client.post(ROOT, headers=headers, json=_start(project))
        assert third.status_code == 202
        _run_done(client, headers, third.json()["id"])


def test_accept_requires_explicit_confirmation_and_discard_keeps_checkout(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        created = client.post(ROOT, headers=headers, json=_start(project))
        assert created.status_code == 202
        run_id = created.json()["id"]
        _run_done(client, headers, run_id)
        wait_until(lambda: not app.state.agent_workflow._live[run_id].driving)
        target = f"{ROOT}/{run_id}"
        _assert_code(client.post(target + "/accept", headers=headers, json={
            "paths": ["app.py"], "confirmed": False,
        }), 403, "consent_required")
        _assert_code(client.post(target + "/accept", headers=headers, json={
            "paths": ["../secret.py"], "confirmed": True,
        }), 422, "invalid_path")
        _assert_code(client.post(target + "/accept", headers=headers, json={
            "paths": ["missing.py"], "confirmed": True,
        }), 422, "invalid_selection")
        assert (project / "app.py").read_bytes() == SOURCE
        accepted = client.post(target + "/accept", headers=headers, json={
            "paths": ["app.py"], "confirmed": True,
        })
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"paths": ["app.py"]}
        assert (project / "app.py").read_bytes() == STAGED
        assert client.get(target, headers=headers).json()["undo_available"] is True
        assert client.get(ROOT, headers=headers).json()["runs"][0]["undo_available"] is True
        assert client.get(target + "/changeset", headers=headers).json()["changes"] == []
        discarded = client.post(target + "/discard", headers=headers)
        assert discarded.status_code == 200
        assert discarded.json() == {"discarded": True}
        assert client.get(target, headers=headers).json()["undo_available"] is True
        _assert_code(client.get(target + "/changeset", headers=headers), 409, "copy_unavailable")
        _assert_code(client.post(target + "/discard", headers=headers), 409, "copy_unavailable")
        assert (project / "app.py").read_bytes() == STAGED


def test_partial_accept_keeps_staged_changes_and_exposes_journal_to_history(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        run_id = client.post(ROOT, headers=headers, json=_start(project)).json()["id"]
        _run_done(client, headers, run_id)
        wait_until(lambda: not app.state.agent_workflow._live[run_id].driving)
        target = f"{ROOT}/{run_id}"
        copy = app.state.agent_workflow._live[run_id].copy
        (copy.path / "pending.py").write_text("pending = True\n", encoding="utf-8")
        assert client.get(target, headers=headers).json()["undo_available"] is False
        assert client.post(target + "/accept", headers=headers, json={
            "paths": ["app.py"], "confirmed": True,
        }).status_code == 200
        staged = client.get(target + "/changeset", headers=headers).json()["changes"]
        assert [change["path"] for change in staged] == ["pending.py"]
        assert client.get(target, headers=headers).json()["undo_available"] is True
        assert (project / "pending.py").exists() is False
        assert (project / "app.py").read_bytes() == STAGED


def test_discard_without_accept_does_not_modify_original_and_stale_accept_fails(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        first = client.post(ROOT, headers=headers, json=_start(project)).json()["id"]
        _run_done(client, headers, first)
        wait_until(lambda: not app.state.agent_workflow._live[first].driving)
        assert client.post(f"{ROOT}/{first}/discard", headers=headers).status_code == 200
        assert (project / "app.py").read_bytes() == SOURCE
        second = client.post(ROOT, headers=headers, json=_start(project)).json()["id"]
        _run_done(client, headers, second)
        wait_until(lambda: not app.state.agent_workflow._live[second].driving)
        (project / "app.py").write_bytes(b"external change\n")
        _assert_code(client.post(f"{ROOT}/{second}/accept", headers=headers, json={
            "paths": ["app.py"], "confirmed": True,
        }), 409, "stale_source")
        assert (project / "app.py").read_bytes() == b"external change\n"



def test_undo_requires_confirmation_and_restores_accepted_paths(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        run_id = client.post(ROOT, headers=headers, json=_start(project)).json()["id"]
        _run_done(client, headers, run_id)
        wait_until(lambda: not app.state.agent_workflow._live[run_id].driving)
        target = f"{ROOT}/{run_id}"
        _assert_code(client.post(target + "/undo", headers=headers, json={
            "confirmed": False,
        }), 403, "consent_required")
        _assert_code(client.post(target + "/undo", headers=headers, json={}), 403, "consent_required")
        _assert_code(client.post(target + "/undo", headers=headers, json={
            "confirmed": True,
        }), 409, "checkpoint_missing")
        assert (project / "app.py").read_bytes() == SOURCE
        assert client.get(target, headers=headers).json()["undo_available"] is False

        accepted = client.post(target + "/accept", headers=headers, json={
            "paths": ["app.py"], "confirmed": True,
        })
        assert accepted.status_code == 200
        assert (project / "app.py").read_bytes() == STAGED
        assert client.get(target, headers=headers).json()["undo_available"] is True
        _assert_code(client.post(target + "/undo", headers=headers, json={
            "confirmed": False,
        }), 403, "consent_required")
        assert (project / "app.py").read_bytes() == STAGED
        restored = client.post(target + "/undo", headers=headers, json={"confirmed": True})
        assert restored.status_code == 200, restored.text
        assert restored.json() == {"paths": ["app.py"]}
        assert (project / "app.py").read_bytes() == SOURCE
        assert client.get(target, headers=headers).json()["undo_available"] is False
        _assert_code(client.post(target + "/undo", headers=headers, json={
            "confirmed": True,
        }), 409, "checkpoint_missing")


def test_undo_refuses_unexpected_source_hash_without_writing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = session_headers(client, app)
        run_id = client.post(ROOT, headers=headers, json=_start(project)).json()["id"]
        _run_done(client, headers, run_id)
        wait_until(lambda: not app.state.agent_workflow._live[run_id].driving)
        target = f"{ROOT}/{run_id}"
        assert client.post(target + "/accept", headers=headers, json={
            "paths": ["app.py"], "confirmed": True,
        }).status_code == 200
        external = b"external edits must survive\n"
        (project / "app.py").write_bytes(external)
        _assert_code(client.post(target + "/undo", headers=headers, json={
            "confirmed": True,
        }), 409, "stale_source")
        assert (project / "app.py").read_bytes() == external


def test_undo_is_owner_scoped_even_after_discard(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    with TestClient(app) as client:
        alice = session_headers(client, app)
        run_id = client.post(ROOT, headers=alice, json=_start(project)).json()["id"]
        _run_done(client, alice, run_id)
        wait_until(lambda: not app.state.agent_workflow._live[run_id].driving)
        target = f"{ROOT}/{run_id}"
        assert client.post(target + "/accept", headers=alice, json={
            "paths": ["app.py"], "confirmed": True,
        }).status_code == 200
        assert client.post(target + "/discard", headers=alice).status_code == 200

        assert client.get(target, headers=alice).json()["undo_available"] is True
        bob = _second_session(client, app)
        _assert_code(client.get(target, headers=bob), 404, "not_found")
        _assert_code(client.post(target + "/undo", headers=bob, json={
            "confirmed": True,
        }), 404, "not_found")
        _assert_code(client.post(target + "/accept", headers=alice, json={
            "paths": ["app.py"], "confirmed": True,
        }), 409, "copy_unavailable")
        assert (project / "app.py").read_bytes() == STAGED

        # Discarding only the disposable copy cannot discard the journal.
        restored = client.post(target + "/undo", headers=alice, json={"confirmed": True})
        assert restored.status_code == 200, restored.text
        assert restored.json() == {"paths": ["app.py"]}
        assert (project / "app.py").read_bytes() == SOURCE


def test_owner_isolation_across_different_bearer_sessions(tmp_path: Path) -> None:
    project = _project(tmp_path)
    app = _app(tmp_path)
    # The installation owner is identical across both sessions by design;
    # the agent ledger deliberately scopes ownership to session_id instead.
    with TestClient(app) as client:
        alice = session_headers(client, app)
        run_id = client.post(ROOT, headers=alice, json=_start(project)).json()["id"]
        _run_done(client, alice, run_id)
        bob = _second_session(client, app)
        assert client.get(ROOT, headers=bob).json() == {"runs": []}
        for method, suffix, payload in (
            ("get", "", None),
            ("get", "/events", None),
            ("get", "/changeset", None),
            ("post", "/approvals/1", {"decision": "deny"}),
            ("post", "/cancel", None),
            ("post", "/accept", {"paths": ["app.py"], "confirmed": True}),
            ("post", "/undo", {"confirmed": True}),
            ("post", "/discard", None),
            ("post", "/resume", None),
        ):
            response = client.request(method, f"{ROOT}/{run_id}{suffix}", headers=bob, json=payload)
            assert response.status_code in {404, 409}, response.text
            assert response.json()["detail"]["code"] in {"not_found", "copy_unavailable"}
        assert client.get(f"{ROOT}/{run_id}", headers=alice).status_code == 200
        assert (project / "app.py").read_bytes() == SOURCE


def test_routes_and_sse_are_in_authenticated_openapi_contract(tmp_path: Path) -> None:
    app = _app(tmp_path)
    spec = app.openapi()
    for path, method in (
        (ROOT, "post"), (ROOT, "get"),
        (ROOT + "/{run_id}", "get"),
        (ROOT + "/{run_id}/events", "get"),
        (ROOT + "/{run_id}/approvals/{seq}", "post"),
        (ROOT + "/{run_id}/cancel", "post"),
        (ROOT + "/{run_id}/changeset", "get"),
        (ROOT + "/{run_id}/accept", "post"),
        (ROOT + "/{run_id}/undo", "post"),
        (ROOT + "/{run_id}/discard", "post"),
        (ROOT + "/{run_id}/resume", "post"),
    ):
        assert spec["paths"][path][method]["security"] == [{"CortexSession": []}]
    assert spec["paths"][ROOT + "/{run_id}/events"]["get"]["responses"]["200"]["content"] == {
        "text/event-stream": {"schema": {"$ref": "#/components/schemas/AgentSSEEvent"}}
    }
    assert spec["components"]["schemas"]["AgentSettings"]["properties"]["enabled"]["default"] is False
