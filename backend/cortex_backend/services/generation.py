"""Headless orchestration of one immutable generation snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import inspect
import logging
from threading import Event
import time
from typing import Any, Protocol

from cortex_backend.core.generation import (
    CodeExecutionProposal,
    CodeProposalRejection,
    FixedPromptPlan,
    GenerationAttachment,
    GenerationSnapshot,
    GenerationStats,
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
    prompt_too_long_message,
)

from .chat import ChatDomainError
from .history_window import (
    HistoryWindowReport,
    describe_history_window,
    safe_label,
    with_attachment_notes,
)
from .progress import NullProgressSink, ProgressEvent, ProgressPhase, ProgressSink
from .token_budget import NEAR_FULL_CONTEXT


def _call_with_optional_kwargs(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call ``func(*args, **kwargs)``, dropping ``kwargs`` if its signature
    does not accept them.

    This is how the generation use case probes an engine's real interface
    (e.g. whether ``translate_text`` accepts the newer ``options`` keyword)
    without depending on a version flag. The naive way to write that probe
    is ``try: func(*args, **kwargs) except TypeError: func(*args)`` -- but
    ``TypeError`` is also what Python raises from *inside* a function body
    for an ordinary bug (a bad response shape, an unpacking mismatch, and
    so on). If the callable had already done real, possibly non-idempotent
    work (a real network call to a model) before hitting that bug, the
    naive version would silently call it a *second* time, masking the bug
    as a benign "wrong overload" and duplicating a call that was never
    meant to run twice.

    To tell the two apart, the candidate call is validated ahead of time
    with :meth:`inspect.Signature.bind`, which raises ``TypeError`` only
    for the argument-binding failure itself -- before ``func`` has run at
    all. Once binding is known to succeed, ``func`` is invoked unguarded,
    so any ``TypeError`` it raises while doing real work propagates
    normally instead of being mistaken for a signature mismatch and
    retried.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        # func can't be introspected (e.g. some C-implemented callables).
        # There is no safe way to probe its signature ahead of the call, so
        # make the call directly rather than risk swallowing a real error.
        return func(*args, **kwargs)
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        return func(*args)
    return func(*args, **kwargs)


# Live deltas are coalesced to this size, or this age, whichever comes first.
# 80 characters matches the slice size the API replay used, so the client-side
# rendering is unchanged for a fast model; 80 milliseconds is well below the
# threshold at which text stops looking live, so a slow model still streams
# token by token.
_DELTA_FLUSH_CHARS = 80
_DELTA_FLUSH_SECONDS = 0.08

# What the user is told when the model ran into the context ceiling. Kept here,
# not in the API layer, because the service is what decides that it happened.
TRUNCATED_ANSWER_MESSAGE = (
    "The answer was cut short by the context limit. "
    "Start a new chat, or raise the context window in Settings."
)


# Attachments named in a truncation notice before the rest are counted.
_MAX_NAMED_ATTACHMENTS = 5


def _history_notice(report: HistoryWindowReport) -> str:
    """What the user is told about the part of the conversation the model was not shown."""
    parts: list[str] = []
    if report.omitted_exchanges:
        count = report.omitted_exchanges
        parts.append(
            f"The conversation is longer than the model's context window, so the {count} oldest "
            f"{'exchange was' if count == 1 else 'exchanges were'} left out of this reply."
        )
    if report.shortened_newest:
        parts.append(
            "The previous answer was too long to keep whole, so only its beginning and end were kept."
        )
    parts.append("Raise the context window in Settings, or start a new chat, to keep more.")
    return " ".join(parts)


def _prompt_trim_notice(plan: FixedPromptPlan) -> str:
    """What the user is told when the memory or code-task instructions did not fit the window.

    "Memory", not "your saved memories": it is the memory instructions that go,
    and they go whether or not anything has been saved yet.
    """
    left_out = []
    if plan.dropped_memories:
        left_out.append("Memory")
    if plan.dropped_code_contract:
        left_out.append("the local code-task instructions")
    them = "it" if left_out == ["Memory"] else "them"
    return (
        f"{' and '.join(left_out)} did not fit the model's context window, "
        f"so this reply was written without {them}. "
        f"Raise the context window in Settings to include {them}."
    )


class GenerationEngine(Protocol):
    """Model-facing operations required by the generation use case.

    This is the whole surface the use case calls, and both implementations --
    the production SynthesisAgent and the deterministic test double -- provide
    all of it. The service used to probe several of these members with
    ``getattr`` for the benefit of "older adapters" that have never existed in
    this repository; declaring them here is what let those probes go.
    """

    last_code_proposal: CodeExecutionProposal | None
    last_code_rejection: CodeProposalRejection | None

    def set_status_callback(self, callback: Callable[[str], None] | None) -> None:
        """Receive startup progress while ``generate`` blocks.

        ``None`` detaches it. The chat client behind a real engine is
        process-wide, so a callback left installed keeps the turn that made it
        -- and everything its closure holds -- alive past that turn's end.
        """

    def plan_fixed_prompt(
        self,
        *,
        query: str,
        user_system_instructions: str | None,
        memories_enabled: bool,
        code_execution_eligible: bool,
        bypass_system_prompt: bool = False,
        host_observations: str | None = None,
        num_ctx: int,
        model: str | None = None,
    ) -> FixedPromptPlan:
        """Settle which optional parts of the fixed prompt fit, or whether it fits at all.

        Asked before memories, history and attachments are sized, because they
        are all fitted to the room the fixed part leaves. The memory
        instructions and the code-task contract are dropped from a turn whose
        window cannot hold them, and the plan says so; ``fits`` is ``False``
        when even the system prompt and the message do not fit, in which case
        the turn is refused rather than sent to a runtime that would truncate
        it without saying so.
        """

    def fit_memories_to_context(
        self,
        memories: list[str],
        *,
        query: str,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        host_observations: str | None = None,
        model: str | None = None,
    ) -> list[str]:
        """Fit permanent memories into the configured context budget.

        ``model`` names the model the prompt is for, so an estimate learned from
        that model's own token counts can be used instead of a fixed ratio.
        """

    def fit_history_to_context(
        self,
        messages: list[dict[str, Any]],
        *,
        query: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        host_observations: str | None = None,
        attachments: Sequence[GenerationAttachment] = (),
        model: str | None = None,
    ) -> str:
        """Format the retained history for the model prompt."""

    def fit_history(
        self,
        messages: list[dict[str, Any]],
        *,
        query: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        host_observations: str | None = None,
        attachments: Sequence[GenerationAttachment] = (),
        model: str | None = None,
    ) -> tuple[str, Sequence[Mapping[str, Any]]]:
        """Return the flattened transcript and the structured history together.

        One call returns both renderings because choosing which exchanges fit
        is the expensive part and must not be done twice. Retention is
        contiguous -- the newest whole exchanges that fit -- and where older
        ones were left out the structured form carries a note that says so.
        """

    def generate_chat_title(
        self,
        chat_history: str,
        *,
        options: dict[str, Any] | None = None,
        cancellation_event: Event | None = None,
    ) -> str | None:
        """Title a thread using the chat model's own context sizing.

        ``cancellation_event`` matters as much here as it does for ``generate``:
        a title the caller has given up on must stop generating, or it keeps
        the model runtime busy while the user's next turn waits behind it.
        """

    def fit_attachments_to_context(
        self,
        attachments: Sequence[GenerationAttachment],
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
        host_observations: str | None = None,
        model: str | None = None,
    ) -> tuple[GenerationAttachment, ...]:
        """Bound attachment reference text to fit the configured context."""

    def generate(
        self,
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        options: dict[str, Any],
        attachments: Sequence[GenerationAttachment] = (),
        cancellation_event: Event | None = None,
        history_messages: Sequence[Mapping[str, Any]] | None = None,
        host_observations: str | None = None,
        on_delta: Callable[[str, str], None] | None = None,
    ) -> tuple[str, str | None, MemoryCommand, GenerationStats | None]:
        """Generate a response and validated memory command.

        ``on_delta`` receives ``(kind, text)`` for each piece of output as the
        model produces it, where ``kind`` is ``"content"`` or ``"thinking"``.
        Implementations that cannot stream simply never call it; the return
        value is the same either way. ``"notice"`` is the one kind that is not
        model output: a sentence for the user about what the engine did before
        asking the model (today, dropping more history after measuring the
        prompt), delivered as a status notice rather than as answer text.
        """

    def translate_text(
        self,
        text: str,
        target_language: str,
        *,
        options: dict[str, Any] | None = None,
        cancellation_event: Event | None = None,
    ) -> TranslationResult:
        """Translate a generated response when requested.

        ``cancellation_event`` matters as much here as it does for ``generate``:
        translation is a second full model call, and without it Stop cannot be
        observed until the model finishes on its own.
        """

HistoryLoader = Callable[[str], Sequence[Mapping[str, Any]]]
MemoryLoader = Callable[[], Sequence[str]]
EngineFactory = Callable[[GenerationSnapshot], GenerationEngine]


@dataclass(frozen=True, slots=True)
class GenerationServiceResult:
    """Successful output from the headless generation use case."""

    response: str
    thoughts: str | None
    memory_command: MemoryCommand
    code_execution_proposal: CodeExecutionProposal | None = None
    # Set when the model asked to run code and the harness refused. Carried
    # beside the answer so the API can explain the refusal instead of leaving
    # the user with a silently missing task.
    code_execution_rejection: CodeProposalRejection | None = None
    stats: GenerationStats | None = None
    # Set when translation was requested and did not produce a usable result.
    # The answer in ``response`` is then the untranslated one: the turn still
    # succeeded, and the API reports the post-process failure beside it.
    translation_error: str | None = None
    # True when the engine published this answer token by token as it arrived.
    # The API replays the finished text as deltas only when it did not, so a
    # non-streaming engine still drives the same client-side rendering.
    streamed: bool = False


class GenerationService:
    """Run generation without depending on Qt, signals, or a UI object."""

    def __init__(
        self,
        *,
        history_loader: HistoryLoader,
        memory_loader: MemoryLoader,
        engine_factory: EngineFactory,
    ):
        self._history_loader = history_loader
        self._memory_loader = memory_loader
        self._engine_factory = engine_factory

    @staticmethod
    def _plan_fixed_prompt(
        engine: GenerationEngine, snapshot: GenerationSnapshot, num_ctx: int
    ) -> FixedPromptPlan:
        return engine.plan_fixed_prompt(
            query=snapshot.user_input,
            user_system_instructions=snapshot.user_system_instructions,
            memories_enabled=snapshot.memories_enabled,
            code_execution_eligible=snapshot.code_execution_eligible,
            bypass_system_prompt=snapshot.bypass_system_prompt,
            host_observations=snapshot.host_observations,
            num_ctx=num_ctx,
            model=snapshot.model,
        )

    def ensure_prompt_fits(self, snapshot: GenerationSnapshot) -> None:
        """Refuse a message the model's context window cannot hold, before the turn exists.

        Called at admission, ahead of the user turn being saved and the job
        starting, so the refusal reaches the person who typed it as a rejected
        request: the message is still in the composer to shorten, nothing has
        been added to the chat, and the runtime is never called. Only the
        fixed part of the prompt is judged -- what is optional (memories, the
        code-task instructions) is dropped later, and history and attachments
        shrink to fit -- so this fails only for a message that could never be
        sent.
        """
        num_ctx = int(snapshot.model_options.get("num_ctx", 8192))
        plan = self._plan_fixed_prompt(self._engine_factory(snapshot), snapshot, num_ctx)
        if not plan.fits:
            raise ChatDomainError(prompt_too_long_message(num_ctx), code="invalid_input")

    def generate(
        self,
        snapshot: GenerationSnapshot,
        *,
        progress_sink: ProgressSink | None = None,
        cancellation_event: Event | None = None,
        history_messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> GenerationServiceResult:
        """Generate from one immutable snapshot and emit owned progress."""
        sink = progress_sink or NullProgressSink()
        self._check_cancelled(cancellation_event)
        self._publish(sink, snapshot, "analysis", "Analyzing the request...")

        # A real snapshot always carries num_ctx (GENERATION_OVERRIDE_FIELDS
        # guarantees it); this fallback only matters for callers that build
        # model_options by hand, so it stays in step with GenerationSettings'
        # own default rather than reintroducing the old, too-small one.
        num_ctx = int(snapshot.model_options.get("num_ctx", 8192))
        self._publish(sink, snapshot, "thoughts", "Gathering thoughts...")
        engine = self._engine_factory(snapshot)
        # Before anything else is sized: memories, history and attachments are
        # all fitted to what the fixed part leaves, so a fixed part that is too
        # big is settled first, and a message that cannot fit is refused here,
        # ahead of any call to the runtime.
        plan = self._plan_fixed_prompt(engine, snapshot, num_ctx)
        if not plan.fits:
            raise ModelOperationError(
                prompt_too_long_message(num_ctx),
                operation="generation",
                error_details="prompt_too_long",
            )
        if plan.dropped_memories or plan.dropped_code_contract:
            # A narrower turn is a different admission: the engine is built from
            # it so the prompt it assembles is the one history was sized for,
            # and a memory command or code proposal the model was never asked
            # for is not acted on.
            snapshot = replace(
                snapshot,
                memories_enabled=plan.memories_enabled,
                code_execution_eligible=plan.code_execution_eligible,
            )
            engine = self._engine_factory(snapshot)
            # Flags and a size only: nothing of the message or the memories.
            logging.info(
                "Left memories=%s and the code contract=%s out of a turn that did not fit a %d-token window.",
                plan.dropped_memories,
                plan.dropped_code_contract,
                num_ctx,
            )
            self._publish(
                sink,
                snapshot,
                "prompt_trimmed",
                _prompt_trim_notice(plan),
                data={
                    "notice": True,
                    "dropped_memories": plan.dropped_memories,
                    "dropped_code_contract": plan.dropped_code_contract,
                },
            )
        permanent_memories = (
            list(self._memory_loader()) if snapshot.memories_enabled else []
        )
        if snapshot.memories_enabled:
            permanent_memories = engine.fit_memories_to_context(
                permanent_memories,
                query=snapshot.user_input,
                user_system_instructions=snapshot.user_system_instructions,
                num_ctx=num_ctx,
                code_execution_eligible=snapshot.code_execution_eligible,
                bypass_system_prompt=snapshot.bypass_system_prompt,
                host_observations=snapshot.host_observations,
                model=snapshot.model,
            )

        # Lets an engine backed by a locally-managed llama.cpp runtime report
        # its own startup progress -- binary download, model load -- while
        # generate() below blocks.
        engine.set_status_callback(
            lambda message: self._publish(sink, snapshot, "loading_model", message)
        )
        try:

            self._check_cancelled(cancellation_event)
            loaded_history = (
                history_messages
                if history_messages is not None
                else self._history_loader(snapshot.thread_id)
            )
            working_history = [dict(message) for message in loaded_history]
            if working_history and working_history[-1].get("role") == "user":
                working_history.pop()

            # Reserve room for attachments *before* history claims the whole
            # budget: fit them first against a placeholder (history is not known
            # yet), giving an attached document priority over old chat turns,
            # then let history size itself around that reservation below. The
            # attachments passed to engine.generate() further down are re-fit
            # against the real, now-correctly-sized chat_history -- this pass
            # only determines how much room history should leave.
            reserved_attachments: Sequence[GenerationAttachment] = ()
            if snapshot.attachments:
                reserved_attachments = engine.fit_attachments_to_context(
                    snapshot.attachments,
                    query=snapshot.user_input,
                    chat_history="No history available.",
                    permanent_memories=permanent_memories,
                    memories_enabled=snapshot.memories_enabled,
                    user_system_instructions=snapshot.user_system_instructions,
                    num_ctx=num_ctx,
                    code_execution_eligible=snapshot.code_execution_eligible,
                    bypass_system_prompt=snapshot.bypass_system_prompt,
                    host_observations=snapshot.host_observations,
                    model=snapshot.model,
                )
                self._announce_truncated_attachments(
                    sink, snapshot, snapshot.attachments, reserved_attachments
                )

            history_kwargs: dict[str, Any] = {
                "query": snapshot.user_input,
                "permanent_memories": permanent_memories,
                "memories_enabled": snapshot.memories_enabled,
                "user_system_instructions": snapshot.user_system_instructions,
                "num_ctx": num_ctx,
                "code_execution_eligible": snapshot.code_execution_eligible,
                "bypass_system_prompt": snapshot.bypass_system_prompt,
                "host_observations": snapshot.host_observations,
                "model": snapshot.model,
            }
            if reserved_attachments:
                history_kwargs["attachments"] = reserved_attachments
            # Preferred shape: the same retained exchanges as real user/assistant
            # turns, which is what a chat-tuned model's template expects and what
            # lets a local runtime reuse its cache across turns. One call returns
            # both renderings because choosing which exchanges fit is the expensive
            # part and must not be done twice. Engines that do not offer it (the
            # narrower fakes in the test suite, and any older adapter) keep the
            # flattened transcript.
            chat_history, structured_history = engine.fit_history(
                working_history, **history_kwargs
            )
            self._announce_history_window(sink, snapshot, working_history, structured_history)

            self._check_cancelled(cancellation_event)
            generate_kwargs: dict[str, Any] = {
                "query": snapshot.user_input,
                "chat_history": chat_history,
                "permanent_memories": permanent_memories,
                "memories_enabled": snapshot.memories_enabled,
                "user_system_instructions": snapshot.user_system_instructions,
                "options": dict(snapshot.model_options),
                "host_observations": snapshot.host_observations,
            }
            # Keep the legacy headless engine protocol compatible for callers that
            # do not use attachments or cancellation; real engines receive the
            # resolved payload.
            if snapshot.attachments:
                # The already-fitted text, so what the user was told was cut is
                # what the model receives. The engine sizes them again against
                # the real history, which leaves text that fits unchanged.
                generate_kwargs["attachments"] = reserved_attachments
            if cancellation_event is not None:
                generate_kwargs["cancellation_event"] = cancellation_event
            generate_kwargs["history_messages"] = structured_history

            # Publish the model's output as it arrives. Both runtimes already
            # consume a token stream; before this the text was joined, returned,
            # and only then replayed to the client in fixed-size slices, so the
            # user watched a spinner for the whole generation and then saw the
            # answer appear at once. On a local model at a few tokens a second
            # that is the difference between the app looking hung and looking
            # alive.
            streamed = False
            # One SSE event per model token is roughly twenty times the event
            # volume of the old 80-character replay, and the job registry
            # retains a bounded number of events per job. Coalescing bounds
            # that for a fast stream without costing a slow one anything: the
            # flush rule is size *or* age, checked as each piece arrives, so no
            # timer thread is needed.
            #
            # Measured over a 500-token answer: at 100 tokens a second this
            # emits 59 events instead of 500; at 5 tokens a second -- a typical
            # local model -- every token still exceeds the age bound and goes
            # out on its own, which is exactly the case where the user needs
            # the feedback. So this is a ceiling on the fast path, not a delay
            # on the slow one.
            pending: dict[str, str] = {}
            last_flush = time.monotonic()

            def flush_deltas() -> None:
                nonlocal streamed, last_flush
                for kind, text in list(pending.items()):
                    if text and self._publish_delta(sink, snapshot, kind, text):
                        streamed = True
                pending.clear()
                last_flush = time.monotonic()

            def publish_delta(kind: str, text: str) -> None:
                if not text:
                    return
                if kind == "notice":
                    # The engine speaking to the user, not the model. Sent at
                    # once and after anything already buffered, so it never
                    # overtakes the text it follows.
                    flush_deltas()
                    self._publish(
                        sink, snapshot, "history_truncated", text, data={"notice": True}
                    )
                    return
                pending[kind] = pending.get(kind, "") + text
                buffered = sum(len(value) for value in pending.values())
                if (
                    buffered >= _DELTA_FLUSH_CHARS
                    or time.monotonic() - last_flush >= _DELTA_FLUSH_SECONDS
                ):
                    flush_deltas()

            generate_kwargs["on_delta"] = publish_delta
            try:
                response, thoughts, memory_command, stats = engine.generate(
                    **generate_kwargs,
                )
            finally:
                # Whatever is still buffered belongs to the user, including on
                # the failure and cancellation paths.
                flush_deltas()
            if not isinstance(memory_command, MemoryCommand):
                raise ModelOperationError(
                    "Generation returned an invalid memory command.",
                    operation="generation",
                )
            # An answer the context ceiling cut off looks exactly like a
            # finished one, and on a reasoning model it can be empty: the
            # thinking used up what was left. The runtime says so
            # (``done_reason`` / ``finish_reason``); tell the user, beside the
            # answer, while there is still a live stream to tell them on. The
            # stats saved with the message carry the same reason.
            if stats is not None and stats.stop_reason == "length":
                self._publish(
                    sink,
                    snapshot,
                    "answer_truncated",
                    TRUNCATED_ANSWER_MESSAGE,
                    data={"truncated": True, "stop_reason": stats.stop_reason},
                )
            self._announce_full_context(sink, snapshot, stats, num_ctx)
            if not snapshot.memories_enabled:
                memory_command = MemoryCommand()

            proposal = engine.last_code_proposal
            if not snapshot.code_execution_eligible or not isinstance(
                proposal, CodeExecutionProposal
            ):
                proposal = None
            rejection = engine.last_code_rejection
            if not isinstance(rejection, CodeProposalRejection) or proposal is not None:
                rejection = None

            translation_error: str | None = None
            if snapshot.translation_enabled:
                self._check_cancelled(cancellation_event)
                self._publish(
                    sink,
                    snapshot,
                    "translation",
                    f"Translating to {snapshot.target_language}...",
                )
                # Translation is a post-process over an answer that already
                # exists. Raising here discarded it: the turn is only persisted
                # after generate() returns, so the user paid for a full
                # generation and got "Translation failed. Please try again."
                # with no answer at all -- and on a machine near its memory
                # limit, loading the second model is the call most likely to
                # fail. Keep the untranslated answer and report the failure
                # beside it.
                try:
                    translation_result = _call_with_optional_kwargs(
                        engine.translate_text,
                        response,
                        snapshot.target_language,
                        options=dict(snapshot.model_options),
                        cancellation_event=cancellation_event,
                    )
                except ModelOperationError as exc:
                    translation_error = str(exc)
                    translation_result = None
                if translation_error is not None:
                    pass
                elif not isinstance(translation_result, TranslationResult):
                    translation_error = "Translation returned an invalid result."
                elif not translation_result.success:
                    translation_error = (
                        translation_result.error or "Translation failed. Please try again."
                    )
                elif not (translation_result.text or "").strip():
                    translation_error = "Translation returned an empty result."
                else:
                    response = translation_result.text or ""
                    if streamed:
                        # The untranslated answer is already on the user's
                        # screen, published live as it was written. Say what
                        # replaces it; a client that has never heard of the
                        # event keeps showing the original until the saved
                        # (translated) message loads at the end of the turn.
                        self._publish(
                            sink,
                            snapshot,
                            "content_replace",
                            "Translated response available.",
                            data={"content": response},
                        )

                if translation_error is not None:
                    self._publish(
                        sink,
                        snapshot,
                        "translation_failed",
                        f"Could not translate to {snapshot.target_language}. "
                        "Showing the original answer.",
                    )

            # A Stop pressed during translation still cancels the turn, as it
            # does everywhere else. Keeping a finished answer across a
            # cancellation is a separate, larger change: the API runner
            # discards the result whenever the cancel event is set (see
            # _start_generation_job), so it has to be fixed there and in the
            # persistence path, not here.
            self._check_cancelled(cancellation_event)

            return GenerationServiceResult(
                response=response,
                thoughts=thoughts,
                memory_command=memory_command,
                code_execution_proposal=proposal,
                code_execution_rejection=rejection,
                stats=stats,
                translation_error=translation_error,
                streamed=streamed,
            )
        finally:
            # The chat client is process-wide while the engine is built per
            # turn, so this callback outlived the turn that installed it. It
            # closes over the snapshot -- attachments included -- which stayed
            # referenced for the life of the process, and any later status
            # message reached a finished turn: generate_chat_title builds a
            # fresh engine and installs no callback of its own, so a model
            # load during titling published "loading_model" against the job
            # that had already completed.
            engine.set_status_callback(None)

    def generate_chat_title(
        self,
        snapshot: GenerationSnapshot,
        response: str,
        *,
        cancellation_event: Event | None = None,
    ) -> str | None:
        """Generate an optional title after response content is available.

        This is deliberately separate from :meth:`generate`: the API can
        publish the answer deltas and persist the assistant turn before the
        lightweight title model runs.  A title-model outage therefore cannot
        stall or invalidate an otherwise successful response.

        ``cancellation_event`` is how the caller abandons a title that is
        taking too long: the API sets it when its own time limit runs out, and
        the engine stops the model call instead of letting it run to the end.
        """
        engine = self._engine_factory(snapshot)
        title_kwargs: dict[str, Any] = {
            # The title reuses the chat model, so it must also reuse the
            # chat's context sizing -- otherwise the runtime is asked for
            # a differently-configured copy of a model it already has
            # loaded, and reloads it.
            "options": dict(snapshot.model_options),
        }
        # Forwarded only when set, the same way ``generate`` does it, so an
        # engine that has no use for it keeps its narrower signature.
        if cancellation_event is not None:
            title_kwargs["cancellation_event"] = cancellation_event
        try:
            return engine.generate_chat_title(
                self._title_history(snapshot.user_input, response),
                **title_kwargs,
            )
        except Exception as exc:  # defensive boundary for optional work
            logging.warning(
                "Cortex chat title generation failed (%s).",
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _title_history(user_input: str, response: str) -> str:
        """Format a bounded first-turn transcript for the optional title model."""
        # User input is capped by the API, but keeping title prompts small is
        # still important for local models and avoids sending accidental large
        # payloads to a second model call.
        max_content = 4000
        return (
            f"User: {str(user_input)[:max_content]}\n"
            f"Assistant: {str(response)[:max_content]}"
        )

    @classmethod
    def _announce_history_window(
        cls,
        sink: ProgressSink,
        snapshot: GenerationSnapshot,
        original: Sequence[Mapping[str, Any]],
        retained: Sequence[Mapping[str, Any]],
    ) -> None:
        """Tell the user when the model will not see the whole conversation.

        Worked out by comparing what the engine kept with what the thread holds,
        so it needs nothing from the engine beyond the history it already
        returns. A history that fits produces no event at all.
        """
        report = describe_history_window(with_attachment_notes(original), retained)
        if not report.truncated:
            return
        cls._publish(
            sink,
            snapshot,
            "history_truncated",
            _history_notice(report),
            data={
                "notice": True,
                "omitted_exchanges": report.omitted_exchanges,
                "shortened_newest": report.shortened_newest,
            },
        )

    @classmethod
    def _announce_truncated_attachments(
        cls,
        sink: ProgressSink,
        snapshot: GenerationSnapshot,
        original: Sequence[GenerationAttachment],
        fitted: Sequence[GenerationAttachment],
    ) -> None:
        """Name every document whose text was cut to fit the context window."""
        names = [
            safe_label(before.filename) or "an attachment"
            for before, after in zip(original, fitted, strict=False)
            if before.text_content != after.text_content
        ]
        if not names:
            return
        shown = ", ".join(names[:_MAX_NAMED_ATTACHMENTS])
        if len(names) > _MAX_NAMED_ATTACHMENTS:
            shown += f" and {len(names) - _MAX_NAMED_ATTACHMENTS} more"
        cls._publish(
            sink,
            snapshot,
            "attachment_truncated",
            f"Part of the attached text did not fit the context window and was cut short: {shown}. "
            "Raise the context window in Settings to include more.",
            data={"notice": True, "truncated_attachments": names},
        )

    @classmethod
    def _announce_full_context(
        cls,
        sink: ProgressSink,
        snapshot: GenerationSnapshot,
        stats: GenerationStats | None,
        num_ctx: int,
    ) -> None:
        """Say so when the runtime reports a prompt that filled the whole window.

        Cortex sizes a prompt to leave room for the answer, so a prompt that
        used the window up means the estimate was wrong, and a runtime that
        truncates on its own (Ollama drops from the front) has then discarded
        the oldest part of it -- the system prompt first -- without an error.
        This cannot prevent that turn, but it stops it being silent.
        """
        if stats is None or not isinstance(stats.prompt_eval_count, int):
            return
        if stats.prompt_eval_count < NEAR_FULL_CONTEXT * max(256, num_ctx):
            return
        cls._publish(
            sink,
            snapshot,
            "context_full",
            "This conversation filled the model's context window, so the runtime may have "
            "discarded the oldest part of it. Raise the context window in Settings, or start a new chat.",
            data={
                "notice": True,
                "prompt_tokens": stats.prompt_eval_count,
                "context_tokens": num_ctx,
            },
        )

    @staticmethod
    def _publish(
        sink: ProgressSink,
        snapshot: GenerationSnapshot,
        phase: ProgressPhase,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        sink.publish(
            ProgressEvent(
                job_id=snapshot.job_id,
                thread_id=snapshot.thread_id,
                phase=phase,
                message=message,
                data=data,
            )
        )

    @staticmethod
    def _publish_delta(
        sink: ProgressSink,
        snapshot: GenerationSnapshot,
        kind: str,
        text: str,
    ) -> bool:
        """Publish one piece of live model output; report whether it was.

        ``kind`` comes from the chat client and is "content" or "thinking";
        anything else is ignored rather than guessed at, so an unfamiliar
        stream cannot inject text into the answer.
        """
        if kind == "content":
            phase: ProgressPhase = "content_delta"
            message = "Response content available."
        elif kind == "thinking":
            phase = "thinking_delta"
            message = "Reasoning available."
        else:
            return False
        sink.publish(
            ProgressEvent(
                job_id=snapshot.job_id,
                thread_id=snapshot.thread_id,
                phase=phase,
                message=message,
                data={"delta": text},
            )
        )
        return True

    @staticmethod
    def _check_cancelled(cancellation_event: Event | None) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise RuntimeError("generation cancelled")
