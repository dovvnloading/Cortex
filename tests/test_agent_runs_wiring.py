"""Live AgentRuns wiring: registry authority, one-shot consent, plans and profiles."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from cortex_backend.agent.loop import AgentLoop, LoopResult
from cortex_backend.agent.model_profiles import DEFAULT_PROBE_CONTEXT_SIZE, ModelProfile, ProfileStore
from cortex_backend.agent.protocol import RuntimeCapabilities, ToolCall, ToolSpec, call_model
from cortex_backend.agent.tool_registry import (
    NeedsApproval, ToolContext, ToolRegistry, ToolResult, UnregisteredToolError,
)
from cortex_backend.services import agent_runs
from cortex_backend.services.agent_runs import AgentRuns
from support import wait_until

OWNER = "local-owner"
READ_TOOLS = {"list_dir", "glob", "grep", "read_file"}
WRITE_TOOLS = {"write_file", "edit_file", "delete_file", "move_file"}
GIT_TOOLS = {"git_status", "git_diff", "git_log", "git_show", "git_commit"}
SHELL_TOOLS = {"run_command", "start_background", "read_background", "stop_background"}
SHARED_TOOLS = {"update_plan", "web_fetch"}
CHECK_TOOLS = {"run_checks"}


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    return root


def _start(
    manager: AgentRuns, project: Path, *,
    mode: str = "ask", model: str = "unprofiled",
    budgets: Mapping[str, int | float] | None = None,
):
    return manager.start(
        owner=OWNER, project_root=str(project), goal="Inspect the disposable project.",
        mode=mode, model=model, budgets=budgets or {}, confirmed_project=True,
    )


def _await_status(manager: AgentRuns, run_id: str, status: str) -> None:
    wait_until(
        lambda: manager.get(run_id, owner=OWNER).status == status
        and not manager._live[run_id].driving,
        describe=f"agent run {status}",
    )


def _approval(manager: AgentRuns, run_id: str) -> tuple[int, dict[str, object]]:
    events, _ = manager.events(run_id, owner=OWNER, after_id=0)
    requests = [event for event in events if event.kind == "approval" and "request" in event.data]
    assert requests
    last = requests[-1]
    request = last.data["request"]
    assert isinstance(request, dict)
    return int(last.data["seq"]), request


class _CaptureLoop:
    """Collect the live registry/settings while completing without model traffic."""

    created: list[_CaptureLoop] = []

    def __init__(
        self, store: Any, _client: object, model: str,
        registry: ToolRegistry, budgets: Mapping[str, int | float], *,
        owner: str, protocol_path: str, model_options: dict[str, int],
        **_kwargs: object,
    ) -> None:
        self.store = store
        self.owner = owner
        self.model = model
        self.registry = registry
        self.budgets = dict(budgets)
        self.protocol_path = protocol_path
        self.model_options = model_options
        self.created.append(self)

    def run(self, run_id: str, *, goal: str | None = None) -> LoopResult:
        self.store.transition(run_id, owner=self.owner, status="running")
        self.store.transition(run_id, owner=self.owner, status="completed", stop_reason="final_answer")
        return LoopResult(run_id, "completed", "final_answer", "Finished", 0, 0)

    def resume(self, run_id: str) -> LoopResult:
        raise AssertionError(f"unexpected resume: {run_id}")

    def cancel(self, run_id: str) -> None:
        pass


def _capture(tmp_path: Path) -> AgentRuns:
    _CaptureLoop.created = []
    return AgentRuns(
        data_dir=tmp_path / "data", chat_client=object(), loop_factory=_CaptureLoop,
    )


@pytest.mark.parametrize("mode", ["plan_only", "ask", "auto_sandboxed"])
def test_every_mode_offers_exactly_its_allowed_tools(tmp_path: Path, mode: str) -> None:
    manager = _capture(tmp_path)
    try:
        run = _start(manager, _project(tmp_path), mode=mode)
        _await_status(manager, run.id, "completed")
        registry = _CaptureLoop.created[-1].registry
        expected = READ_TOOLS | SHARED_TOOLS
        if mode != "plan_only":
            expected |= WRITE_TOOLS | GIT_TOOLS | SHELL_TOOLS | CHECK_TOOLS
        assert {spec.name for spec in registry.specs()} == expected
        assert "list_files" not in expected and "replace_file" not in expected
        result = registry.execute(
            ToolCall("read", "read_file", {"path": "app.py"}),
            ToolContext(Event(), float("inf")),
        )
        assert isinstance(result, ToolResult) and result.success
    finally:
        manager.close()


def test_plan_only_never_registers_a_workcopy_writer_or_process(tmp_path: Path) -> None:
    manager = _capture(tmp_path)
    try:
        run = _start(manager, _project(tmp_path), mode="plan_only")
        _await_status(manager, run.id, "completed")
        registry = _CaptureLoop.created[-1].registry
        for name in WRITE_TOOLS | GIT_TOOLS | SHELL_TOOLS | CHECK_TOOLS:
            with pytest.raises(UnregisteredToolError):
                registry.execute(ToolCall("unregistered", name, {}), ToolContext(Event(), float("inf")))
        assert (Path(run.workcopy) / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    finally:
        manager.close()


@pytest.mark.parametrize("mode", ["ask", "auto_sandboxed"])
def test_all_file_mutations_require_separate_approval_even_in_auto_mode(
    tmp_path: Path, mode: str,
) -> None:
    manager = _capture(tmp_path)
    try:
        run = _start(manager, _project(tmp_path), mode=mode)
        _await_status(manager, run.id, "completed")
        registry = _CaptureLoop.created[-1].registry
        calls = {
            "write_file": {"path": "app.py", "content": "value = 2\n", "approved": True},
            "edit_file": {"path": "app.py", "old": "1", "new": "2"},
            "delete_file": {"path": "app.py"},
            "move_file": {"source": "app.py", "destination": "moved.py"},
        }
        for name, arguments in calls.items():
            outcome = registry.execute(
                ToolCall("unapproved", name, arguments),
                ToolContext(Event(), float("inf")),
            )
            assert isinstance(outcome, NeedsApproval)
            assert outcome.request["tool"] == name
            assert (Path(run.workcopy) / "app.py").read_text(encoding="utf-8") == "value = 1\n"
        assert not (Path(run.workcopy) / "moved.py").exists()
        git = registry.execute(
            ToolCall("commit", "git_commit", {"message": "Model suggested commit"}),
            ToolContext(Event(), float("inf")),
        )
        assert isinstance(git, NeedsApproval) and git.request["tool"] == "git_commit"
    finally:
        manager.close()


def _tool(name: str, arguments: dict[str, object]) -> str:
    return json.dumps({
        "text": "", "finish": "tool_calls",
        "tool_calls": [{"name": name, "arguments": arguments}],
    })


def _final(message: str = "Done") -> str:
    return json.dumps({"text": message, "finish": "stop", "tool_calls": []})


class _JsonClient:
    tool_protocol_capabilities = RuntimeCapabilities(json_schema=True)

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> dict[str, object]:
        self.requests.append(kwargs)
        assert self.responses, "unexpected extra model call"
        return {"message": {"content": self.responses.pop(0)}}


def test_web_fetch_needs_approval_for_each_exact_url_even_in_auto_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "https://example.org/reference?q=first%2Fsecond"
    second = "https://example.org/other?token=two"
    fetched: list[tuple[str, bool]] = []

    def fake_fetch(url: str, *, approved: bool) -> str:
        fetched.append((url, approved))
        assert approved is True
        return "BEGIN UNTRUSTED REFERENCE DATA\nReference only"

    monkeypatch.setattr(agent_runs, "fetch_url", fake_fetch)
    client = _JsonClient(
        _tool("web_fetch", {"url": first}),
        _tool("web_fetch", {"url": second}),
        _final(),
    )
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=client)
    try:
        run = _start(manager, _project(tmp_path), mode="auto_sandboxed")
        _await_status(manager, run.id, "waiting_approval")
        seq, request = _approval(manager, run.id)
        assert request["tool"] == "web_fetch" and request["url"] == first
        assert fetched == []

        manager.approval(run.id, owner=OWNER, seq=seq, decision="allow_once")
        manager.resume(run.id, owner=OWNER)
        _await_status(manager, run.id, "waiting_approval")
        seq2, request2 = _approval(manager, run.id)
        assert seq2 > seq
        assert request2["url"] == second
        assert fetched == [(first, True)]

        manager.approval(run.id, owner=OWNER, seq=seq2, decision="allow_once")
        manager.resume(run.id, owner=OWNER)
        _await_status(manager, run.id, "completed")
        assert fetched == [(first, True), (second, True)]
        ledger = manager._ledger().db_path.read_bytes()
        assert first.encode() not in ledger and second.encode() not in ledger
    finally:
        manager.close()


def test_live_file_write_stages_only_after_its_own_approval(tmp_path: Path) -> None:
    client = _JsonClient(
        _tool("write_file", {"path": "app.py", "content": "value = 2\n"}),
        _final(),
    )
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=client)
    project = _project(tmp_path)
    try:
        run = _start(manager, project, mode="auto_sandboxed")
        _await_status(manager, run.id, "waiting_approval")
        seq, request = _approval(manager, run.id)
        assert request["tool"] == "write_file"
        assert request["path"] == "app.py"
        assert (Path(run.workcopy) / "app.py").read_text(encoding="utf-8") == "value = 1\n"
        assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"

        manager.approval(run.id, owner=OWNER, seq=seq, decision="allow_once")
        manager.resume(run.id, owner=OWNER)
        _await_status(manager, run.id, "completed")
        assert (Path(run.workcopy) / "app.py").read_text(encoding="utf-8") == "value = 2\n"
        assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    finally:
        manager.close()


def test_web_denial_and_model_supplied_approval_cannot_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    monkeypatch.setattr(agent_runs, "fetch_url", lambda url, *, approved: called.append(url))
    client = _JsonClient(
        _tool("web_fetch", {"url": "https://example.org/blocked"}),
        _final(),
    )
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=client)
    try:
        run = _start(manager, _project(tmp_path))
        _await_status(manager, run.id, "waiting_approval")
        seq, _ = _approval(manager, run.id)
        manager.approval(run.id, owner=OWNER, seq=seq, decision="deny")
        manager.resume(run.id, owner=OWNER)
        _await_status(manager, run.id, "completed")
        assert called == []
    finally:
        manager.close()

    injected = _JsonClient(
        _tool("web_fetch", {"url": "https://example.org/never", "approved": True}),
        _final(),
    )
    manager = AgentRuns(data_dir=tmp_path / "other-data", chat_client=injected)
    try:
        run = _start(manager, _project(tmp_path))
        _await_status(manager, run.id, "completed")
        assert called == []
        assert all(step.kind != "tool" for step in manager._ledger().list_steps(run.id, owner=OWNER))
    finally:
        manager.close()


def test_live_plan_updates_are_persisted_and_guard_nudges_an_unfinished_plan(tmp_path: Path) -> None:
    pending = [{"text": "Inspect", "status": "in_progress"}]
    done = [{"text": "Inspect", "status": "done"}]
    client = _JsonClient(
        _tool("update_plan", {"items": pending}), _final("Too soon"),
        _tool("update_plan", {"items": done}), _final("Finished"),
    )
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=client)
    try:
        run = _start(manager, _project(tmp_path), mode="plan_only")
        _await_status(manager, run.id, "completed")
        loop = manager._live[run.id].loop
        assert isinstance(loop, AgentLoop)
        assert loop._plan_guard_installed is True
        plans = [step for step in manager._ledger().list_steps(run.id, owner=OWNER) if step.kind == "plan"]
        assert len(plans) == 2
        assert json.loads(str(plans[-1].result["summary"])) == {"items": done}
        assert len(client.requests) == 4
    finally:
        manager.close()


def _profile(model: str, *, context_size: int = 1024) -> ModelProfile:
    return ModelProfile(
        model=model, protocol_path="constrained_json",
        passed={"single_call": True, "ordered_calls": True, "injection_resistance": True},
        failed_checks=(), recommended=True,
        measured_at=datetime.now(timezone.utc).isoformat(),
        context_size=context_size, elapsed_ms=1,
    )


def test_profile_selects_wire_format_and_narrows_limits_unknown_uses_safe_default(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    ProfileStore(data_dir).save([_profile("measured")])
    manager = _capture(tmp_path)
    try:
        project = _project(tmp_path)
        known = _start(manager, project, model="measured")
        _await_status(manager, known.id, "completed")
        chosen = _CaptureLoop.created[-1]
        assert chosen.protocol_path == "constrained_json"
        assert chosen.model_options == {"num_ctx": 1024}
        assert chosen.budgets["max_context_chars"] == 4096

        unknown = _start(manager, project, model="unknown-model")
        _await_status(manager, unknown.id, "completed")
        fallback = _CaptureLoop.created[-1]
        assert fallback.protocol_path == "plain_json"
        assert fallback.model_options == {"num_ctx": DEFAULT_PROBE_CONTEXT_SIZE}
        assert fallback.budgets["max_context_chars"] == DEFAULT_PROBE_CONTEXT_SIZE * 4

        narrow = _start(manager, project, model="measured", budgets={"max_context_chars": 800})
        _await_status(manager, narrow.id, "completed")
        limited = _CaptureLoop.created[-1]
        assert limited.budgets["max_context_chars"] == 800
        assert limited.model_options == {"num_ctx": 200}
    finally:
        manager.close()


class _ProbeClient:
    """Answers the capability probe's seven scripted exchanges on native tools."""

    tool_protocol_capabilities = RuntimeCapabilities(native_tools=True, json_schema=True)

    def __init__(self, *, injection_answer: str = "HARBOR-17", fail: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self._fail = fail
        self._replies = [
            ("probe_read", {"key": "single"}), "CORAL-7",
            ("probe_read", {"key": "ordered"}), ("probe_verify", {"marker": "LILAC-29"}), "verified",
            ("probe_read", {"key": "injection"}), injection_answer,
        ]
        self.on_request: Any = None

    def chat(self, **kwargs: Any) -> dict[str, object]:
        self.requests.append(kwargs)
        if self.on_request is not None:
            self.on_request()
        if self._fail:
            raise ConnectionError("runtime unavailable")
        reply = self._replies[(len(self.requests) - 1) % len(self._replies)]
        if isinstance(reply, str):
            return {"message": {"content": reply, "tool_calls": []}, "done_reason": "stop"}
        name, arguments = reply
        return {"message": {"content": "", "tool_calls": [{
            "id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments},
        }]}, "done_reason": "stop"}


def _profiling(tmp_path: Path, client: object, *, enabled: bool = True) -> AgentRuns:
    _CaptureLoop.created = []
    return AgentRuns(
        data_dir=tmp_path / "data", chat_client=client, loop_factory=_CaptureLoop,
        profile_models=enabled,
    )


def _status_events(manager: AgentRuns, run_id: str) -> list[dict[str, object]]:
    events, _ = manager.events(run_id, owner=OWNER, after_id=0)
    return [event.data for event in events if event.kind == "status"]


def test_unmeasured_local_model_is_probed_once_then_cached(tmp_path: Path) -> None:
    client = _ProbeClient()
    manager = _profiling(tmp_path, client)
    try:
        project = _project(tmp_path)
        first = _start(manager, project, model="local-model")
        _await_status(manager, first.id, "completed")
        assert len(client.requests) == 7
        assert _CaptureLoop.created[-1].protocol_path == "native_tools"
        statuses = _status_events(manager, first.id)
        assert {"status": "pending", "phase": "model_check"} in statuses
        assert {"status": "running", "model_check": {"recommended": True, "failed_checks": []}} in statuses
        (saved,) = ProfileStore(tmp_path / "data").load()
        assert saved.model == "local-model" and saved.recommended is True

        second = _start(manager, project, model="local-model")
        _await_status(manager, second.id, "completed")
        assert len(client.requests) == 7, "a cached profile must not be measured again"
        assert _CaptureLoop.created[-1].protocol_path == "native_tools"
        assert {"status": "pending", "phase": "model_check"} not in _status_events(manager, second.id)
    finally:
        manager.close()


def test_unrecommended_model_runs_on_its_measured_format_with_a_warning(tmp_path: Path) -> None:
    client = _ProbeClient(injection_answer="OVERRIDDEN")
    manager = _profiling(tmp_path, client)
    try:
        run = _start(manager, _project(tmp_path), model="obedient-model")
        _await_status(manager, run.id, "completed")
        assert _CaptureLoop.created[-1].protocol_path == "native_tools"
        assert {"status": "running", "model_check": {
            "recommended": False, "failed_checks": ["injection_resistance"],
        }} in _status_events(manager, run.id)
        (saved,) = ProfileStore(tmp_path / "data").load()
        assert saved.recommended is False and saved.failed_checks == ("injection_resistance",)
    finally:
        manager.close()


def test_a_probe_that_fails_every_check_is_not_cached(tmp_path: Path) -> None:
    client = _ProbeClient(fail=True)
    manager = _profiling(tmp_path, client)
    try:
        project = _project(tmp_path)
        run = _start(manager, project, model="offline-model")
        _await_status(manager, run.id, "completed")
        assert _CaptureLoop.created[-1].protocol_path == "plain_json"
        assert {"status": "running", "model_check": {
            "recommended": False,
            "failed_checks": ["single_call", "ordered_calls", "injection_resistance"],
        }} in _status_events(manager, run.id)
        assert ProfileStore(tmp_path / "data").load() == []
        calls = len(client.requests)
        again = _start(manager, project, model="offline-model")
        _await_status(manager, again.id, "completed")
        assert len(client.requests) > calls, "an uncached failure is measured again"
    finally:
        manager.close()


def test_profiling_off_cloud_models_and_unreadable_stores_never_probe(tmp_path: Path) -> None:
    client = _ProbeClient()
    for name in ("off", "cloud", "corrupt"):
        (tmp_path / name).mkdir()
    off = _profiling(tmp_path / "off", client, enabled=False)
    try:
        run = _start(off, _project(tmp_path / "off"), model="local-model")
        _await_status(off, run.id, "completed")
    finally:
        off.close()
    cloud = _profiling(tmp_path / "cloud", client)
    try:
        run = cloud.start(
            owner=OWNER, project_root=str(_project(tmp_path / "cloud")), goal="Inspect.",
            mode="ask", model="cloud:openai:gpt-4o", budgets={},
            confirmed_project=True, confirmed_cloud=True,
        )
        _await_status(cloud, run.id, "completed")
    finally:
        cloud.close()
    corrupt_dir = tmp_path / "corrupt"
    store = ProfileStore(corrupt_dir / "data")
    store.path.parent.mkdir(parents=True)
    store.path.write_text('{"schema_version":999,"profiles":[]}', encoding="utf-8")
    corrupt = _profiling(corrupt_dir, client)
    try:
        run = _start(corrupt, _project(corrupt_dir), model="local-model")
        _await_status(corrupt, run.id, "completed")
        assert store.path.read_text(encoding="utf-8") == '{"schema_version":999,"profiles":[]}'
    finally:
        corrupt.close()
    assert client.requests == []
    assert all(loop.protocol_path == "plain_json" for loop in _CaptureLoop.created)


def test_stop_during_the_probe_ends_it_without_saving_a_profile(tmp_path: Path) -> None:
    client = _ProbeClient()
    manager = _profiling(tmp_path, client)
    run_ids: list[str] = []
    client.on_request = lambda: manager.cancel(run_ids[0], owner=OWNER) if len(client.requests) == 1 else None
    try:
        run = _start(manager, _project(tmp_path), model="local-model")
        run_ids.append(run.id)
        wait_until(lambda: not manager._live[run.id].driving, describe="worker finished")
        # The first check finishes its two exchanges; nothing after the stop.
        assert len(client.requests) == 2
        assert ProfileStore(tmp_path / "data").load() == []
        assert _CaptureLoop.created[-1].protocol_path == "plain_json"
    finally:
        manager.close()


def test_corrupt_profile_store_falls_back_without_widening(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = ProfileStore(data_dir).path
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version":999,"profiles":[]}', encoding="utf-8")
    manager = _capture(tmp_path)
    try:
        run = _start(manager, _project(tmp_path))
        _await_status(manager, run.id, "completed")
        assert _CaptureLoop.created[-1].protocol_path == "plain_json"
        assert _CaptureLoop.created[-1].budgets["max_context_chars"] == 4 * DEFAULT_PROBE_CONTEXT_SIZE
    finally:
        manager.close()


def test_protocol_choice_never_claims_unsupported_adapter_capabilities() -> None:
    class Client:
        tool_protocol_capabilities = RuntimeCapabilities(native_tools=True, json_schema=True)

        def __init__(self) -> None:
            self.request: dict[str, Any] = {}

        def chat(self, **kwargs: Any) -> dict[str, object]:
            self.request = kwargs
            return {"message": {"content": _final()}}

    spec = ToolSpec("read", "Read", {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    })
    client = Client()
    result = call_model(
        client, model="test", messages=[{"role": "user", "content": "test"}],
        tools=[spec], options={}, protocol_path="plain_json",
    )
    assert result.finish == "stop"
    assert "tools" not in client.request and "format" not in client.request

    call_model(
        client, model="test", messages=[{"role": "user", "content": "test"}],
        tools=[spec], options={}, protocol_path="constrained_json",
    )
    assert "format" in client.request and "tools" not in client.request

    client.tool_protocol_capabilities = RuntimeCapabilities()
    call_model(
        client, model="test", messages=[{"role": "user", "content": "test"}],
        tools=[spec], options={}, protocol_path="native_tools",
    )
    assert "tools" not in client.request and "format" not in client.request


def test_shell_notices_reach_the_user_as_cortex_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    real_register = agent_runs.register_shell_tools

    def register(*args: Any, **kwargs: Any) -> Any:
        captured["on_notice"] = kwargs["on_notice"]
        return real_register(*args, **kwargs)

    class NoticeLoop(_CaptureLoop):
        def run(self, run_id: str, *, goal: str | None = None) -> LoopResult:
            captured["on_notice"]("The sandbox cannot use python from a synthetic folder.")
            return super().run(run_id, goal=goal)

    monkeypatch.setattr(agent_runs, "register_shell_tools", register)
    _CaptureLoop.created = []
    manager = AgentRuns(data_dir=tmp_path / "data", chat_client=object(), loop_factory=NoticeLoop)
    try:
        run = _start(manager, _project(tmp_path), mode="ask")
        _await_status(manager, run.id, "completed")
        events, _ = manager.events(run.id, owner=OWNER, after_id=0)
        outputs = [event.data for event in events if event.kind == "output"]
        assert {"text": "The sandbox cannot use python from a synthetic folder.", "source": "cortex"} in outputs
    finally:
        manager.close()
