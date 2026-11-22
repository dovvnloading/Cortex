"""Owner-scoped, single-worker bridge from the local API to the agent loop.

The ledger stores metadata only. Goals, model output, approval requests and
work-copy objects remain in memory; a restart interrupts live runs rather than
silently re-approving an operation. No tool can write to the real checkout.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
from pathlib import Path
from threading import RLock
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from cortex_backend.agent.context_instructions import load_project_instructions
from cortex_backend.agent.context_repo_map import build_repo_map
from cortex_backend.agent.loop import AgentLoop, LoopBudgets, LoopResult
from cortex_backend.agent.model_profiles import (
    DEFAULT_PROBE_CONTEXT_SIZE,
    ModelProfile,
    ProbeStopped,
    ProfileSchemaError,
    ProfileStore,
    probe_model,
)
from cortex_backend.agent.protocol import ProtocolPath, ToolCall, ToolSpec
from cortex_backend.agent.run_store import (
    AgentRun, AgentRunStore, AgentStep, ApprovalExpiredError,
    ApprovalUnavailableError, InvalidRunTransitionError,
)
from cortex_backend.agent.tool_registry import (
    NeedsApproval, ToolContext, ToolInterrupted, ToolRegistry, ToolResult,
)
from cortex_backend.agent.tools.checks import register_check_tools
from cortex_backend.agent.tools.files import register_file_tools
from cortex_backend.agent.tools.git import register_git_tools
from cortex_backend.agent.tools.plan import plan_guard, register_plan_tool
from cortex_backend.agent.tools.shell import ShellTools, register_shell_tools
from cortex_backend.agent.tools.web import WebFetchError, fetch_url
from cortex_backend.agent.workcopy import WorkCopy, WorkCopyChange, WorkCopyError, restore
from cortex_backend.core.paths import AppPathError, AppPaths
from cortex_backend.services.workspace_checkpoint import (
    WorkspaceCheckpointError, WorkspaceCheckpointStore,
)
from cortex_backend.services.chat_client import is_cloud_model


logger = logging.getLogger(__name__)

EventKind = Literal["step", "output", "approval", "status"]
_TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


class AgentWorkflowError(RuntimeError):
    """Only the code, never an input path or model response, reaches the API."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The agent operation could not be completed.")


class AgentLoopLike(Protocol):
    def run(self, run_id: str, *, goal: str | None = None) -> LoopResult: ...
    def resume(self, run_id: str) -> LoopResult: ...
    def cancel(self, run_id: str) -> None: ...


LoopFactory = Callable[..., AgentLoopLike]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    id: int
    kind: EventKind
    run_id: str
    data: dict[str, object]


@dataclass(slots=True)
class _LiveRun:
    run_id: str
    owner: str
    copy: WorkCopy
    loop: AgentLoopLike
    events: list[AgentEvent] = field(default_factory=list)
    next_event_id: int = 1
    last_step_seq: int = 0
    driving: bool = False
    shell: ShellTools | None = None
    terminal: bool = False
    model: str = ""
    # Measure the model on the worker before its first run (once per model).
    needs_probe: bool = False
    model_check: dict[str, object] | None = None
    cancel_requested: bool = False


_WEB_FETCH = ToolSpec(
    "web_fetch", "Fetch a public HTTP(S) reference only after approval for this exact URL.", {
        "type": "object",
        "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 2048}},
        "required": ["url"], "additionalProperties": False,
    },
)


def _register_web_fetch(registry: ToolRegistry) -> None:
    def handle(call: ToolCall, context: ToolContext) -> ToolResult | NeedsApproval:
        url = call.arguments.get("url")
        if not isinstance(url, str) or not 1 <= len(url) <= 2048:
            return ToolResult("Invalid web URL.", success=False)
        if not context.approved:
            # URL (including its query) is shown only in the ephemeral approval
            # request; it is never persisted in the run's metadata ledger.
            return NeedsApproval({
                "tool": "web_fetch", "url": url,
                "summary": "Fetch this exact public URL once.",
            })
        if context.stopped():
            raise ToolInterrupted("Web fetch interrupted.")
        try:
            return ToolResult(fetch_url(url, approved=True), summary="Web reference fetched")
        except (WebFetchError, ValueError, PermissionError, TimeoutError):
            return ToolResult("Web fetch refused.", success=False)

    registry.register(_WEB_FETCH, handle)


def _stored_profile(data_dir: Path, model: str) -> tuple[ModelProfile | None, bool]:
    """The model's saved profile, and whether the store could be read at all.

    An unreadable or unsupported store counts as unreadable, so the caller
    neither trusts it nor measures over it (save() refuses to overwrite it).
    """
    try:
        profiles = ProfileStore(data_dir).load()
    except (OSError, ProfileSchemaError):
        logger.warning("Agent model profiles unavailable; using safe defaults.")
        return None, False
    return next((profile for profile in profiles if profile.model == model), None), True


def _usable_profile(profile: ModelProfile | None) -> ModelProfile | None:
    # A run uses the wire format the model was measured on whenever it managed
    # a basic tool call there. Whether it passed every check decides only the
    # warning the user sees; an unmeasured model keeps the prompt-only format.
    return profile if profile is not None and profile.passed.get("single_call") is True else None


def _model_check(profile: ModelProfile | None) -> dict[str, object] | None:
    if profile is None:
        return None
    return {"recommended": profile.recommended, "failed_checks": list(profile.failed_checks)}


def _profile_settings(
    profile: ModelProfile | None, budgets: dict[str, int | float],
) -> tuple[ProtocolPath, dict[str, int | float], dict[str, int]]:
    """Wire format and limits from a usable profile, else the safe defaults.

    A measured num_ctx is a token count. The agent's character budget uses a
    conservative four-characters-per-token bound, never an increased ceiling.
    """
    selected = _usable_profile(profile)
    protocol_path: ProtocolPath = selected.protocol_path if selected is not None else "plain_json"
    context_size = selected.context_size if selected is not None else DEFAULT_PROBE_CONTEXT_SIZE
    narrowed = dict(budgets)
    narrowed["max_context_chars"] = min(int(budgets["max_context_chars"]), context_size * 4)
    # Also bound the actual model request to the run's narrower context budget.
    model_ctx = min(context_size, max(1, int(narrowed["max_context_chars"]) // 4))
    return protocol_path, narrowed, {"num_ctx": model_ctx}


class AgentRuns:
    """Process-owned coordinator; at most one active or approval-parked run.

    The one-worker executor prevents a second model loop from executing at the
    same time. The active slot is reserved *before* constructing a work copy,
    and stays reserved while the loop awaits approval.
    """

    def __init__(
        self, *, data_dir: Path | None = None,
        run_store: AgentRunStore | None = None,
        chat_client: Any = None,
        loop_factory: LoopFactory | None = None,
        checkpoint_store: WorkspaceCheckpointStore | None = None,
        profile_models: bool = False,
    ) -> None:
        self._data_dir = data_dir
        self._store = run_store
        self._client = chat_client
        # When true, a local model without a saved profile is measured with
        # the capability probe on its first run, and the result is cached.
        self._profile_models = profile_models
        self._loop_factory = loop_factory or AgentLoop
        self._checkpoint_store = checkpoint_store
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cortex-agent")
        self._lock = RLock()
        self._live: dict[str, _LiveRun] = {}
        self._active_id: str | None = None
        self._closed = False

    def _directory(self) -> Path:
        try:
            paths = (
                AppPaths.from_data_dir(self._data_dir)
                if self._data_dir is not None else AppPaths.for_current_user()
            )
            return paths.data_dir
        except (AppPathError, OSError, ValueError) as exc:
            raise AgentWorkflowError("agent_unavailable") from exc

    def _ledger(self) -> AgentRunStore:
        # Initialization can mark previous-process runs interrupted. Make it
        # exactly once for this app, even under concurrent GET and POST calls.
        with self._lock:
            store = self._store
            if store is None:
                try:
                    store = AgentRunStore(self._directory() / "agent" / "runs.sqlite")
                except Exception as exc:
                    raise AgentWorkflowError("agent_unavailable") from exc
                self._store = store
            return store

    def _owned(self, run_id: str, owner: str) -> AgentRun:
        try:
            run = self._ledger().get_run(run_id, owner=owner)
        except Exception as exc:
            raise AgentWorkflowError("agent_unavailable") from exc
        if run is None:
            raise AgentWorkflowError("not_found")
        return run

    def _expire_active(self) -> None:
        """Release a parked run when its one-shot approval has expired.

        No worker is running while approval is parked. Expiry is a durable
        store transition, not permission to replay an unapproved tool call.
        """
        with self._lock:
            run_id = self._active_id
            if run_id is None:
                return
            managed = self._live.get(run_id)
            if managed is None or managed.driving:
                return
            run = self._owned(run_id, managed.owner)
            if run.status != "waiting_approval":
                return
            try:
                expired = self._ledger().expire_approvals(owner=managed.owner)
            except Exception as exc:
                raise AgentWorkflowError("agent_unavailable") from exc
            for expired_id, seq in expired:
                if expired_id != run_id:
                    continue
                current = self._owned(run_id, managed.owner)
                self._emit(managed, "approval", {"seq": seq, "decision": "expired"})
                self._emit(managed, "status", {
                    "status": current.status, "stop_reason": current.stop_reason,
                })
                if current.status in _TERMINAL:
                    managed.terminal = True
                    if managed.shell is not None:
                        managed.shell.close()
                    self._active_id = None

    def _copy_for(self, run_id: str, owner: str) -> _LiveRun:
        self._owned(run_id, owner)
        managed = self._live.get(run_id)
        if managed is None or managed.owner != owner:
            raise AgentWorkflowError("copy_unavailable")
        return managed

    @staticmethod
    def _budgets(value: Mapping[str, int | float]) -> dict[str, int | float]:
        limits = LoopBudgets().narrowed(value)
        return {
            "max_model_calls": limits.max_model_calls,
            "max_tool_calls": limits.max_tool_calls,
            "max_seconds": limits.max_seconds,
            "max_context_chars": limits.max_context_chars,
            "max_steps": limits.max_steps,
        }

    def _emit(self, managed: _LiveRun, kind: EventKind, data: dict[str, object]) -> None:
        # A run can record at most 128 steps with the default bounded loop.
        # Retain the complete in-process replay window across reconnects.
        managed.events.append(AgentEvent(managed.next_event_id, kind, managed.run_id, data))
        managed.next_event_id += 1

    def _publish_steps(self, managed: _LiveRun) -> None:
        steps = self._ledger().list_steps(managed.run_id, owner=managed.owner)
        for step in steps:
            if step.seq <= managed.last_step_seq:
                continue
            self._emit(managed, "step", {
                "seq": step.seq, "kind": step.kind,
                "summary": step.summary, "tool": step.tool,
            })
            managed.last_step_seq = step.seq

    def _shell_output(self, run_id: str, text: str, tool_call_id: str) -> None:
        """Forward shell output with command provenance, never as an agent answer."""
        with self._lock:
            managed = self._live.get(run_id)
            if managed is not None and not managed.terminal:
                self._emit(managed, "output", {
                    "text": text, "source": "command", "tool_call_id": tool_call_id,
                })

    def _after_tool(self, run_id: str) -> Callable[[AgentStep], None]:
        def publish(_step: AgentStep) -> None:
            with self._lock:
                managed = self._live.get(run_id)
                if managed is not None:
                    self._publish_steps(managed)
        return publish

    def start(
        self, *, owner: str, project_root: str, goal: str, mode: str,
        model: str, budgets: Mapping[str, int | float],
        confirmed_project: bool = False, confirmed_cloud: bool = False,
    ) -> AgentRun:
        with self._lock:
            if confirmed_project is not True:
                raise AgentWorkflowError("consent_required")
            if is_cloud_model(model) and confirmed_cloud is not True:
                raise AgentWorkflowError("cloud_consent_required")
            if self._closed:
                raise AgentWorkflowError("agent_unavailable")
            self._expire_active()
            if self._active_id is not None:
                raise AgentWorkflowError("busy")
            if self._client is None:
                raise AgentWorkflowError("model_unavailable")
            if mode not in {"plan_only", "ask", "auto_sandboxed"}:
                raise AgentWorkflowError("invalid_mode")
            try:
                normalized_budgets = self._budgets(budgets)
            except (TypeError, ValueError) as exc:
                raise AgentWorkflowError("invalid_budgets") from exc
            directory = self._directory()
            profile, store_readable = _stored_profile(directory, model)
            protocol_path, normalized_budgets, model_options = _profile_settings(
                profile, normalized_budgets,
            )
            # Cloud models are never probed: that would send extra requests
            # to a provider the user approved for this run's work only.
            needs_probe = (
                self._profile_models and store_readable and profile is None and not is_cloud_model(model)
            )
            store = self._ledger()
            run_id = uuid4().hex
            # Reserve before any filesystem I/O; another request cannot copy
            # a second project and start another worker during this operation.
            self._active_id = run_id
            copy: WorkCopy | None = None
            shell: ShellTools | None = None
            try:
                prepared = WorkCopy.create(
                    project_root, run_id, confirmed=True, data_dir=directory,
                    checkpoint_store=self._checkpoint_store, owner=owner,
                )
                copy = prepared
                created = store.create_run(
                    owner=owner, project_root=prepared.project_root, workcopy=prepared.path,
                    mode=mode, model=model, budgets=normalized_budgets, run_id=run_id,
                )
                registry = ToolRegistry()
                register_file_tools(
                    registry, prepared.path, read_only=mode == "plan_only", require_approval=True,
                )
                register_plan_tool(registry, store, run_id, owner=owner)
                _register_web_fetch(registry)
                if mode != "plan_only":
                    register_git_tools(registry, prepared.path, run_id, require_approval=True)
                    shell = register_shell_tools(
                        registry, prepared.path, run_id, mode=mode,
                        on_output=lambda text, tool_call_id: self._shell_output(run_id, text, tool_call_id),
                    )
                    register_check_tools(registry, prepared.path, shell)
                providers: Sequence[Callable[[AgentRun], str]] = (
                    lambda _run: build_repo_map(prepared.path, 700),
                    lambda _run: load_project_instructions(prepared.path),
                )
                loop = self._loop_factory(
                    store, self._client, model, registry, normalized_budgets,
                    owner=owner, goal=goal, after_tool=self._after_tool(run_id),
                    context_providers=providers,
                    protocol_path=protocol_path, model_options=model_options,
                )
                if isinstance(loop, AgentLoop):
                    plan_guard(loop)
                managed = _LiveRun(
                    run_id, owner, prepared, loop, shell=shell, model=model,
                    needs_probe=needs_probe, model_check=_model_check(profile),
                )
                self._live[run_id] = managed
                self._emit(managed, "status", {"status": "pending"})
                self._submit(managed, resume=False)
                return created
            except Exception:
                self._active_id = None
                self._live.pop(run_id, None)
                if shell is not None:
                    shell.close()
                if copy is not None:
                    try:
                        copy.discard()
                    except WorkCopyError as exc:
                        logger.warning("Agent work-copy cleanup failed (%s).", exc.code)
                raise

    def _submit(self, managed: _LiveRun, *, resume: bool) -> None:
        if self._closed or managed.driving:
            raise AgentWorkflowError("busy")
        managed.driving = True
        try:
            self._executor.submit(self._drive, managed, resume)
        except RuntimeError as exc:
            managed.driving = False
            raise AgentWorkflowError("agent_unavailable") from exc

    def _measure_model(self, managed: _LiveRun) -> None:
        """Probe an unmeasured model once, on the worker, before its first run.

        The pending run's work copy already exists; only model traffic with
        synthetic content happens here. A probe in which every check failed is
        not cached (a stopped or unreachable runtime looks the same), so the
        next run measures again. Stop is honoured between checks.
        """
        managed.needs_probe = False
        with self._lock:
            self._emit(managed, "status", {"status": "pending", "phase": "model_check"})
        try:
            profile = probe_model(
                self._client, managed.model, {"num_ctx": DEFAULT_PROBE_CONTEXT_SIZE},
                should_stop=lambda: managed.cancel_requested,
            )
        except ProbeStopped:
            return
        except Exception as exc:
            logger.warning("Agent model check failed (%s).", type(exc).__name__)
            return
        if any(profile.passed.values()):
            try:
                ProfileStore(self._directory()).upsert(profile)
            except (OSError, ProfileSchemaError, AgentWorkflowError) as exc:
                logger.warning("Agent model profile not saved (%s).", type(exc).__name__)
        managed.model_check = _model_check(profile)
        usable = _usable_profile(profile)
        if usable is not None and hasattr(managed.loop, "protocol_path"):
            # Same context size as the defaults the loop was built with, so
            # only the wire format changes; the loop has not started yet.
            cast(Any, managed.loop).protocol_path = usable.protocol_path

    def _drive(self, managed: _LiveRun, resume: bool) -> None:
        result: LoopResult | None = None
        try:
            if not resume and managed.needs_probe:
                self._measure_model(managed)
            with self._lock:
                self._emit(managed, "status", {
                    "status": "running",
                    **({"model_check": managed.model_check} if managed.model_check is not None else {}),
                })
            result = managed.loop.resume(managed.run_id) if resume else managed.loop.run(managed.run_id)
        except Exception as exc:
            logger.warning("Agent worker failed (%s).", type(exc).__name__)
            # A model/tool exception may contain a prompt, path, or source
            # bytes. Never log it or return it in a response.
            try:
                store = self._ledger()
                current = store.get_run(managed.run_id, owner=managed.owner)
                if current is not None:
                    if current.status == "pending":
                        store.transition(managed.run_id, owner=managed.owner, status="running")
                        current = store.get_run(managed.run_id, owner=managed.owner)
                    if current is not None and current.status in {"running", "waiting_approval"}:
                        store.transition(
                            managed.run_id, owner=managed.owner,
                            status="failed", stop_reason="agent_error",
                        )
            except Exception as exc:
                logger.warning("Agent worker status recovery failed (%s).", type(exc).__name__)
        finally:
            with self._lock:
                try:
                    self._publish_steps(managed)
                    if result is not None and result.answer is not None:
                        self._emit(managed, "output", {"text": result.answer[:16000], "source": "agent"})
                    if result is not None and result.approval_request is not None:
                        # The pending call stays in memory and never appears in
                        # the ledger. The owner sees the concrete proposed edit.
                        self._emit(managed, "approval", {
                            "request": result.approval_request,
                            "seq": managed.last_step_seq,
                        })
                    current = self._owned(managed.run_id, managed.owner)
                    self._emit(managed, "status", {
                        "status": current.status, "stop_reason": current.stop_reason,
                    })
                    if current.status in _TERMINAL:
                        self._active_id = None
                except Exception as exc:
                    logger.warning("Agent event publishing failed (%s).", type(exc).__name__)
                    # A failure to read status is NOT proof that a worker has
                    # finished. Retain the slot, even if this blocks new work,
                    # rather than accidentally admit two active runs.
                finally:
                    # Cleanup must run even when event publishing fails. A
                    # parked approval keeps its dev servers; terminal runs do not.
                    try:
                        final_run = self._owned(managed.run_id, managed.owner)
                        terminal = final_run.status in _TERMINAL
                    except AgentWorkflowError:
                        terminal = result is None or result.status in _TERMINAL
                    if terminal:
                        managed.terminal = True
                        if managed.shell is not None:
                            managed.shell.close()
                        self._active_id = None
                    managed.driving = False

    def list_runs(self, *, owner: str) -> list[AgentRun]:
        self._expire_active()
        return self._ledger().list_runs(owner=owner)

    def get(self, run_id: str, *, owner: str) -> AgentRun:
        self._expire_active()
        return self._owned(run_id, owner)

    def undo_available(self, run_id: str, *, owner: str) -> bool:
        """Report an owner-bound checkpoint, never local acceptance UI state.

        Existence is an availability hint; confirmed undo still preflights
        the source hashes and refuses unexpected changes.
        """
        with self._lock:
            run = self._owned(run_id, owner)
            managed = self._live.get(run_id)
            if run.status not in _TERMINAL or (managed is not None and managed.driving):
                return False
            try:
                database = self._directory() / "workspace_checkpoints.sqlite"
                if self._checkpoint_store is None and not database.is_file():
                    return False
                store = self._checkpoint_store or WorkspaceCheckpointStore(database)
                checkpoint = store.get(owner=owner, checkpoint_id=f"agent-workcopy-{run_id}")
                return checkpoint.state in {"active", "pending"} and bool(checkpoint.files)
            except Exception:
                # Missing, unreadable or corrupt checkpoints must fail closed.
                return False

    def events(
        self, run_id: str, *, owner: str, after_id: int,
    ) -> tuple[list[AgentEvent], bool]:
        with self._lock:
            self._expire_active()
            run = self._owned(run_id, owner)
            managed = self._live.get(run_id)
            if managed is None:
                # A process restart invalidates the in-memory goal and
                # approvals. The durable step metadata is still replayable.
                steps = self._ledger().list_steps(run_id, owner=owner)
                historical = [
                    AgentEvent(i, "step", run_id, {
                        "seq": step.seq, "kind": step.kind,
                        "summary": step.summary, "tool": step.tool,
                    }) for i, step in enumerate(steps, start=1)
                ]
                historical.append(AgentEvent(
                    len(historical) + 1, "status", run_id,
                    {"status": run.status, "stop_reason": run.stop_reason},
                ))
                return [event for event in historical if event.id > after_id], True
            return (
                [event for event in managed.events if event.id > after_id],
                run.status in _TERMINAL | {"waiting_approval"} and not managed.driving,
            )

    def approval(
        self, run_id: str, *, owner: str, seq: int, decision: str,
    ) -> AgentRun:
        with self._lock:
            managed = self._copy_for(run_id, owner)
            if managed.driving:
                raise AgentWorkflowError("busy")
            if decision not in {"allow_once", "deny"}:
                raise AgentWorkflowError("invalid_decision")
            try:
                self._ledger().decide_approval(
                    run_id, owner=owner, step_seq=seq,
                    decision="approved" if decision == "allow_once" else "rejected",
                )
            except ApprovalExpiredError as exc:
                self._emit(managed, "approval", {"seq": seq, "decision": "expired"})
                self._emit(managed, "status", {
                    "status": "failed", "stop_reason": "approval_expired",
                })
                managed.terminal = True
                if managed.shell is not None:
                    managed.shell.close()
                self._active_id = None
                raise AgentWorkflowError("approval_expired") from exc
            except (ApprovalUnavailableError, InvalidRunTransitionError) as exc:
                raise AgentWorkflowError("approval_unavailable") from exc
            self._emit(managed, "approval", {"seq": seq, "decision": decision})
            return self._owned(run_id, owner)

    def resume(self, run_id: str, *, owner: str) -> AgentRun:
        with self._lock:
            managed = self._copy_for(run_id, owner)
            run = self._owned(run_id, owner)
            if self._active_id != run_id or managed.driving:
                raise AgentWorkflowError("busy" if managed.driving else "invalid_state")
            if run.status != "running":
                raise AgentWorkflowError("approval_required" if run.status == "waiting_approval" else "invalid_state")
            self._submit(managed, resume=True)
            return run

    def cancel(self, run_id: str, *, owner: str) -> AgentRun:
        with self._lock:
            managed = self._copy_for(run_id, owner)
            current = self._owned(run_id, owner)
            if current.status in _TERMINAL:
                if not managed.driving:
                    managed.terminal = True
                    if managed.shell is not None:
                        managed.shell.close()
                return current
            managed.cancel_requested = True
            managed.loop.cancel(run_id)
            current = self._owned(run_id, owner)
            if current.status in _TERMINAL and not managed.driving:
                managed.terminal = True
                if managed.shell is not None:
                    managed.shell.close()
                self._emit(managed, "status", {"status": current.status, "stop_reason": current.stop_reason})
                self._active_id = None
            return current

    def changeset(self, run_id: str, *, owner: str) -> dict[str, WorkCopyChange]:
        with self._lock:
            return self._copy_for(run_id, owner).copy.changeset()

    def accept(self, run_id: str, *, owner: str, paths: Sequence[str], confirmed: bool) -> tuple[str, ...]:
        with self._lock:
            managed = self._copy_for(run_id, owner)
            if self._owned(run_id, owner).status not in _TERMINAL or managed.driving:
                raise AgentWorkflowError("busy")
            return managed.copy.accept(paths, confirmed=confirmed)

    def undo(self, run_id: str, *, owner: str, confirmed: bool) -> tuple[str, ...]:
        """Restore only journaled originals after explicit, owner-scoped consent.

        The recovery journal validates all current source hashes before writing.
        After a discard or process restart the live work copy is gone, but the
        owner-scoped checkpoint remains recoverable.
        """
        if confirmed is not True:
            raise AgentWorkflowError("consent_required")
        with self._lock:
            run = self._owned(run_id, owner)
            managed = self._live.get(run_id)
            if run.status not in _TERMINAL or (managed is not None and managed.driving):
                raise AgentWorkflowError("busy")
            if managed is not None and managed.owner != owner:
                raise AgentWorkflowError("not_found")
            try:
                if managed is not None:
                    return managed.copy.undo(confirmed=True)
                return restore(
                    run.project_root, run_id, confirmed=True,
                    data_dir=self._directory(), owner=owner,
                )
            except WorkspaceCheckpointError as exc:
                raise AgentWorkflowError(exc.code) from exc

    def discard(self, run_id: str, *, owner: str) -> None:
        with self._lock:
            managed = self._copy_for(run_id, owner)
            if self._owned(run_id, owner).status not in _TERMINAL or managed.driving:
                raise AgentWorkflowError("busy")
            if managed.shell is not None:
                managed.shell.close()
            managed.copy.discard()
            self._live.pop(run_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            managed = self._live.get(self._active_id) if self._active_id is not None else None
            shells = [item.shell for item in self._live.values() if item.shell is not None]
        for shell in shells:
            shell.close()
        if managed is not None:
            try:
                managed.loop.cancel(managed.run_id)
            except Exception as exc:
                logger.warning("Agent shutdown cancellation failed (%s).", type(exc).__name__)
        # Model transport is closed by the application after this step. Never
        # wait indefinitely for an uncooperative network/model call.
        self._executor.shutdown(wait=False, cancel_futures=True)
