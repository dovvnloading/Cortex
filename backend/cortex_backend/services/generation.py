"""Headless orchestration of one immutable generation snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
from threading import Event
from typing import Any, Protocol

from cortex_backend.core.generation import (
    CodeExecutionProposal,
    CodeProposalRejection,
    GenerationAttachment,
    GenerationSnapshot,
    GenerationStats,
    MemoryCommand,
    ModelOperationError,
    TranslationResult,
)

from .progress import NullProgressSink, ProgressEvent, ProgressPhase, ProgressSink


class GenerationEngine(Protocol):
    """Model-facing operations required by the generation use case."""

    def fit_memories_to_context(
        self,
        memories: list[str],
        *,
        query: str,
        user_system_instructions: str | None,
        num_ctx: int,
        code_execution_eligible: bool | None = None,
        bypass_system_prompt: bool = False,
    ) -> list[str]:
        """Fit permanent memories into the configured context budget."""

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
        attachments: Sequence[GenerationAttachment] = (),
    ) -> str:
        """Format the retained history for the model prompt."""

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
    ) -> tuple[str, str | None, MemoryCommand, GenerationStats | None]:
        """Generate a response and validated memory command."""

    def translate_text(self, text: str, target_language: str) -> TranslationResult:
        """Translate a generated response when requested."""

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

        permanent_memories = (
            list(self._memory_loader()) if snapshot.memories_enabled else []
        )
        # A real snapshot always carries num_ctx (GENERATION_OVERRIDE_FIELDS
        # guarantees it); this fallback only matters for callers that build
        # model_options by hand, so it stays in step with GenerationSettings'
        # own default rather than reintroducing the old, too-small one.
        num_ctx = int(snapshot.model_options.get("num_ctx", 8192))
        self._publish(sink, snapshot, "thoughts", "Gathering thoughts...")
        engine = self._engine_factory(snapshot)
        # ``fit_history`` marks an engine that understands the newer prompt
        # shape, which is also the one that renders host observations. Older
        # engines neither accept nor render them, so the extra argument is
        # withheld rather than probed for.
        observation_kwargs: dict[str, Any] = (
            {"host_observations": snapshot.host_observations}
            if callable(getattr(engine, "fit_history", None))
            else {}
        )
        if snapshot.memories_enabled:
            permanent_memories = engine.fit_memories_to_context(
                permanent_memories,
                query=snapshot.user_input,
                user_system_instructions=snapshot.user_system_instructions,
                num_ctx=num_ctx,
                code_execution_eligible=snapshot.code_execution_eligible,
                bypass_system_prompt=snapshot.bypass_system_prompt,
                **observation_kwargs,
            )

        # Optional: lets an engine (e.g. one backed by a locally-managed
        # llama.cpp runtime) report its own startup progress -- binary
        # download, model load -- while generate() below blocks. Most
        # engines don't need this and simply won't define the setter.
        status_setter = getattr(engine, "set_status_callback", None)
        if callable(status_setter):
            status_setter(
                lambda message: self._publish(sink, snapshot, "loading_model", message)
            )

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
        fit_attachments = getattr(engine, "fit_attachments_to_context", None)
        if snapshot.attachments and callable(fit_attachments):
            reserved_attachments = fit_attachments(
                snapshot.attachments,
                query=snapshot.user_input,
                chat_history="No history available.",
                permanent_memories=permanent_memories,
                memories_enabled=snapshot.memories_enabled,
                user_system_instructions=snapshot.user_system_instructions,
                num_ctx=num_ctx,
                code_execution_eligible=snapshot.code_execution_eligible,
                bypass_system_prompt=snapshot.bypass_system_prompt,
                **observation_kwargs,
            )

        history_kwargs: dict[str, Any] = {
            "query": snapshot.user_input,
            "permanent_memories": permanent_memories,
            "memories_enabled": snapshot.memories_enabled,
            "user_system_instructions": snapshot.user_system_instructions,
            "num_ctx": num_ctx,
            "code_execution_eligible": snapshot.code_execution_eligible,
            "bypass_system_prompt": snapshot.bypass_system_prompt,
            **observation_kwargs,
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
        structured_history: Sequence[Mapping[str, Any]] | None = None
        fit_history = getattr(engine, "fit_history", None)
        if callable(fit_history):
            chat_history, structured_history = fit_history(
                working_history, **history_kwargs
            )
        else:
            chat_history = engine.fit_history_to_context(
                working_history, **history_kwargs
            )

        self._check_cancelled(cancellation_event)
        generate_kwargs: dict[str, Any] = {
            "query": snapshot.user_input,
            "chat_history": chat_history,
            "permanent_memories": permanent_memories,
            "memories_enabled": snapshot.memories_enabled,
            "user_system_instructions": snapshot.user_system_instructions,
            "options": dict(snapshot.model_options),
            **observation_kwargs,
        }
        # Keep the legacy headless engine protocol compatible for callers that
        # do not use attachments or cancellation; real engines receive the
        # resolved payload.
        if snapshot.attachments:
            generate_kwargs["attachments"] = snapshot.attachments
        if cancellation_event is not None:
            generate_kwargs["cancellation_event"] = cancellation_event
        if structured_history is not None:
            # Safe to pass unguarded: the two are one capability on the engine.
            # An engine that can select structured history is by construction
            # one whose generate() accepts it, so there is no signature to
            # probe -- and retrying on TypeError would be actively harmful,
            # because a TypeError raised from *inside* a working generate()
            # would be indistinguishable from a signature mismatch and would
            # silently run the model a second time.
            generate_kwargs["history_messages"] = structured_history
        response, thoughts, memory_command, stats = engine.generate(
            **generate_kwargs,
        )
        if not isinstance(memory_command, MemoryCommand):
            raise ModelOperationError(
                "Generation returned an invalid memory command.",
                operation="generation",
            )
        if not snapshot.memories_enabled:
            memory_command = MemoryCommand()

        proposal = getattr(engine, "last_code_proposal", None)
        if not snapshot.code_execution_eligible or not isinstance(
            proposal, CodeExecutionProposal
        ):
            proposal = None
        rejection = getattr(engine, "last_code_rejection", None)
        if not isinstance(rejection, CodeProposalRejection) or proposal is not None:
            rejection = None

        if snapshot.translation_enabled:
            self._check_cancelled(cancellation_event)
            self._publish(
                sink,
                snapshot,
                "translation",
                f"Translating to {snapshot.target_language}...",
            )
            try:
                translation_result = engine.translate_text(
                    response,
                    snapshot.target_language,
                    options=dict(snapshot.model_options),
                )
            except TypeError:
                translation_result = engine.translate_text(
                    response,
                    snapshot.target_language,
                )
            if not isinstance(translation_result, TranslationResult):
                raise ModelOperationError(
                    "Translation returned an invalid result.",
                    operation="translation",
                )
            if not translation_result.success:
                raise ModelOperationError(
                    translation_result.error or "Translation failed. Please try again.",
                    operation="translation",
                )
            response = translation_result.text or ""

        self._check_cancelled(cancellation_event)

        return GenerationServiceResult(
            response=response,
            thoughts=thoughts,
            memory_command=memory_command,
            code_execution_proposal=proposal,
            code_execution_rejection=rejection,
            stats=stats,
        )

    def generate_chat_title(
        self,
        snapshot: GenerationSnapshot,
        response: str,
    ) -> str | None:
        """Generate an optional title after response content is available.

        This is deliberately separate from :meth:`generate`: the API can
        publish the answer deltas and persist the assistant turn before the
        lightweight title model runs.  A title-model outage therefore cannot
        stall or invalidate an otherwise successful response.
        """
        engine = self._engine_factory(snapshot)
        title_generator = getattr(engine, "generate_chat_title", None)
        if not callable(title_generator):
            return None
        try:
            return title_generator(
                self._title_history(snapshot.user_input, response),
                # The title reuses the chat model, so it must also reuse the
                # chat's context sizing -- otherwise the runtime is asked for
                # a differently-configured copy of a model it already has
                # loaded, and reloads it.
                options=dict(snapshot.model_options),
            )
        except TypeError:
            # An engine predating the options parameter (older adapters, and
            # the narrower fakes in the test suite) still titles correctly.
            return title_generator(self._title_history(snapshot.user_input, response))
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

    @staticmethod
    def _publish(
        sink: ProgressSink,
        snapshot: GenerationSnapshot,
        phase: ProgressPhase,
        message: str,
    ) -> None:
        sink.publish(
            ProgressEvent(
                job_id=snapshot.job_id,
                thread_id=snapshot.thread_id,
                phase=phase,
                message=message,
            )
        )

    @staticmethod
    def _check_cancelled(cancellation_event: Event | None) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise RuntimeError("generation cancelled")
