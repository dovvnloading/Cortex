"""The README tour must show Cortex doing what it actually does.

``tools/screenshots/tour_server.py`` scripts the model replies behind the
animated README tour, and nothing else: Agent mode, its work copy and
approvals, and the restricted interpreter are the real ones. These tests hold
the script to that, so a tour that would fail, or would show output Cortex
does not produce, fails here instead of on camera.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "screenshots"))

import showcase_server  # noqa: E402
import tour_server  # noqa: E402

from cortex_backend.execution.repository import ExecutionRepository  # noqa: E402
from cortex_backend.services.agent_runs import AgentRuns  # noqa: E402
from cortex_backend.testing import DurableFakeCoordinator  # noqa: E402
from support import wait_until  # noqa: E402

OWNER = "local-owner"


def _calls() -> list[dict]:
    return [call for reply in tour_server.AGENT_REPLIES for call in json.loads(reply)["tool_calls"]]


def test_the_streamed_pieces_add_up_to_the_saved_answer() -> None:
    assert "".join(tour_server._tokens(tour_server.ANSWER)) == tour_server.ANSWER
    assert "".join(tour_server._tokens(tour_server.THOUGHTS)) == tour_server.THOUGHTS


def test_the_answer_spells_out_the_byte_order_mark() -> None:
    # A literal U+FEFF renders as nothing; the answer has to show the escape.
    assert "﻿" not in tour_server.ANSWER
    assert "\\ufeff" in tour_server.ANSWER


def test_every_scripted_reply_is_a_complete_protocol_message() -> None:
    for reply in tour_server.AGENT_REPLIES:
        message = json.loads(reply)
        assert set(message) == {"text", "finish", "tool_calls"}
        assert message["finish"] in {"tool_calls", "stop"}
    assert json.loads(tour_server.AGENT_REPLIES[-1])["finish"] == "stop"


def test_the_scripted_edit_matches_the_sample_project_exactly_once() -> None:
    edits = [call["arguments"] for call in _calls() if call["name"] == "edit_file"]
    assert len(edits) == 1
    source = tour_server.PROJECT_FILES[edits[0]["path"]]
    # edit_file refuses an edit whose text is missing or ambiguous.
    assert source.count(edits[0]["old"]) == 1


def test_the_tour_run_changes_only_the_work_copy_until_it_is_accepted(tmp_path: Path) -> None:
    project = tour_server.write_sample_project(tmp_path)
    gate = tour_server.Gate()
    gate.release(len(tour_server.AGENT_REPLIES) + 5)
    manager = AgentRuns(
        data_dir=tmp_path / "data",
        chat_client=tour_server.ScriptedAgentModel(tour_server.AGENT_REPLIES, gate),
    )

    def settled(run_id: str, status: str) -> bool:
        return manager.get(run_id, owner=OWNER).status == status and not manager._live[run_id].driving

    try:
        run = manager.start(
            owner=OWNER, project_root=str(project), goal=tour_server.AGENT_GOAL,
            mode="ask", model="qwen3:8b", budgets={}, confirmed_project=True,
        )
        wait_until(lambda: settled(run.id, "waiting_approval"), describe="tour edit approval")
        events, _ = manager.events(run.id, owner=OWNER, after_id=0)
        approval = [event for event in events if event.kind == "approval" and "request" in event.data][-1]
        assert approval.data["request"]["tool"] == "edit_file"

        manager.approval(run.id, owner=OWNER, seq=int(approval.data["seq"]), decision="allow_once")
        manager.resume(run.id, owner=OWNER)
        wait_until(lambda: settled(run.id, "completed"), describe="tour run completed")

        staged = (Path(run.workcopy) / "importer.py").read_text(encoding="utf-8")
        assert 'encoding="utf-8-sig",' in staged
        original = (project / "importer.py").read_text(encoding="utf-8")
        assert original == tour_server.PROJECT_FILES["importer.py"]
    finally:
        manager.close()


def test_the_tray_shows_what_the_interpreter_prints_once_allowed(tmp_path: Path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    coordinator = DurableFakeCoordinator(repository)
    showcase_server.stage_execution_tasks(coordinator, include_completed=False)
    owner = repository.installation_principal_id

    assert repository.get_job("code-orders-above-average", owner=owner) is None
    assert tour_server.finish_approved_task(coordinator, "code-loan-payment") == {
        "finished": False, "reason": "not_approved",
    }

    repository.decide_approval("code-loan-payment", owner=owner, decision="approved")
    finished = tour_server.finish_approved_task(coordinator, "code-loan-payment")

    assert finished["stdout"] == "monthly payment: 489.15\ntotal interest: 4349.22\n"
    job = repository.get_job("code-loan-payment", owner=owner)
    assert job is not None and job.status == "succeeded"


def test_every_demo_model_reports_its_own_size() -> None:
    assert set(showcase_server.MODEL_DETAILS) == set(showcase_server.INSTALLED_MODELS)
