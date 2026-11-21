"""Visible, bounded task plans for opt-in agent runs.

Plan items are data, never directions to execute tools. The run ledger's
existing result.summary field holds the JSON document for the latest 'plan'
step; its other fields remain short, code-owned metadata.
"""

from __future__ import annotations

from contextvars import ContextVar
import json
from typing import Literal, TypedDict, cast

from ..loop import AgentLoop, LoopResult, _History, _State
from ..protocol import ToolCall, ToolSpec
from ..run_store import AgentRunStore, RunStatus
from ..tool_registry import ToolContext, ToolInterrupted, ToolRegistry, ToolResult

PlanStatus = Literal["pending", "in_progress", "done"]
_STATUSES = frozenset({"pending", "in_progress", "done"})
_NUDGE = "The task plan has unfinished items. Continue the work and update the plan before giving a final answer."


class _PlanItem(TypedDict):
    text: str
    status: PlanStatus


def _valid_items(raw: object) -> list[_PlanItem] | None:
    """Validate even direct registry calls, which bypass protocol JSON Schema."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        return None
    items: list[_PlanItem] = []
    in_progress = 0
    for candidate in raw:
        if not isinstance(candidate, dict) or set(candidate) != {"text", "status"}:
            return None
        text: object = candidate["text"]
        status: object = candidate["status"]
        if not isinstance(text, str) or not 1 <= len(text) <= 200:
            return None
        if not isinstance(status, str) or status not in _STATUSES:
            return None
        if status == "in_progress":
            in_progress += 1
            if in_progress > 1:
                return None
        items.append({"text": text, "status": cast(PlanStatus, status)})
    return items


def register_plan_tool(
    registry: ToolRegistry, run_store: AgentRunStore, run_id: str, *, owner: str = "local",
) -> None:
    """Offer a data-only update_plan tool for the specified owner-scoped run."""
    registry.register(
        ToolSpec(
            name="update_plan",
            description="Publish a visible task plan or update item statuses. Does not execute tasks.",
            json_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "minLength": 1, "maxLength": 200},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                            },
                            "required": ["text", "status"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        ),
        lambda call, context: _update_plan(call, context, run_store, run_id, owner),
    )


def _update_plan(
    call: ToolCall, context: ToolContext, run_store: AgentRunStore, run_id: str, owner: str,
) -> ToolResult:
    items = _valid_items(call.arguments.get("items"))
    if set(call.arguments) != {"items"} or items is None:
        return ToolResult(
            "Invalid plan: use 1-20 items, each with 1-200 characters and a valid status; "
            "at most one item may be in_progress.",
            summary="Plan rejected",
            success=False,
        )
    if context.stopped():
        raise ToolInterrupted("plan update interrupted")
    # A structured JSON string fits the ledger's existing, size-bounded
    # result.summary descriptor. No raw tool argument is written to args_json.
    payload = json.dumps({"items": items}, ensure_ascii=True, separators=(",", ":"))
    run_store.append_step(
        run_id,
        owner=owner,
        kind="plan",
        summary=f"Plan updated ({len(items)} items)",
        tool="update_plan",
        result={"summary": payload, "count": len(items)},
    )
    return ToolResult("Plan updated.", summary="Plan updated")


def _has_unfinished_plan(run_store: AgentRunStore, run_id: str, owner: str) -> bool:
    plans = (step for step in reversed(run_store.list_steps(run_id, owner=owner)) if step.kind == "plan")
    latest = next(plans, None)
    if latest is None:
        return False
    raw = latest.result.get("summary")
    if not isinstance(raw, str):
        return True
    try:
        payload: object = json.loads(raw)
    except (TypeError, ValueError):
        return True
    if not isinstance(payload, dict) or set(payload) != {"items"}:
        return True
    items = _valid_items(payload["items"])
    return items is None or any(item["status"] != "done" for item in items)


def plan_guard(loop: AgentLoop) -> None:
    """Nudge once per run when a final reply leaves a recorded plan unfinished.

    AgentLoop's before_final callback is notification-only: it cannot defer a
    final response. Pair it with a per-instance final-stop interception to
    continue the same bounded loop once. No global monkeypatch or new authority
    is introduced; cancellation, context limits, and call budgets still apply.
    """
    if getattr(loop, "_plan_guard_installed", False):
        return

    previous_before_final = loop.before_final
    original_stop = loop._stop
    drive = loop._drive
    checked_final: ContextVar[bool] = ContextVar("plan_checked_final", default=False)
    nudged: set[str] = set()

    def before_final() -> None:
        if previous_before_final is not None:
            previous_before_final()
        checked_final.set(True)

    def guarded_stop(
        run_id: str, state: _State, status: RunStatus, reason: str, *, answer: str | None = None,
    ) -> LoopResult:
        checked = checked_final.get()
        if checked:
            checked_final.set(False)
        if checked and status == "completed" and reason == "final_answer" and run_id not in nudged:
            try:
                unfinished = _has_unfinished_plan(loop.run_store, run_id, loop.owner)
            except Exception:
                return original_stop(run_id, state, "failed", "hook_error")
            if unfinished:
                nudged.add(run_id)
                # Only code-owned, fixed wording enters the model prompt.
                # Plan text is neither repeated here nor interpreted.
                state.history.append(_History("user", _NUDGE))
                return drive(run_id, state)
        return original_stop(run_id, state, status, reason, answer=answer)

    loop.before_final = before_final
    setattr(loop, "_stop", guarded_stop)
    setattr(loop, "_plan_guard_installed", True)
