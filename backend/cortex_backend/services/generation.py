"""Headless orchestration of one immutable generation snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import Event
from typing import Any, Protocol

from cortex_backend.core.generation import (
    GenerationSnapshot,
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
    ) -> str:
        """Format the retained history for the model prompt."""

    def generate(
        self,
        *,
        query: str,
        chat_history: str,
        permanent_memories: list[str],
        memories_enabled: bool,
        user_system_instructions: str | None,
        options: dict[str, Any],
    ) -> tuple[str, str | None, MemoryCommand]:
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
    ) -> GenerationServiceResult:
        """Generate from one immutable snapshot and emit owned progress."""
        sink = progress_sink or NullProgressSink()
        self._check_cancelled(cancellation_event)
        self._publish(sink, snapshot, "analysis", "Analyzing the request...")

        permanent_memories = (
            list(self._memory_loader()) if snapshot.memories_enabled else []
        )
        num_ctx = int(snapshot.model_options.get("num_ctx", 4096))
        if snapshot.memories_enabled:
            self._publish(sink, snapshot, "thoughts", "Gathering thoughts...")
            engine = self._engine_factory(snapshot)
            permanent_memories = engine.fit_memories_to_context(
                permanent_memories,
                query=snapshot.user_input,
                user_system_instructions=snapshot.user_system_instructions,
                num_ctx=num_ctx,
            )
        else:
            self._publish(sink, snapshot, "thoughts", "Gathering thoughts...")
            engine = self._engine_factory(snapshot)

        self._check_cancelled(cancellation_event)
        history_messages = [
            dict(message) for message in self._history_loader(snapshot.thread_id)
        ]
        if history_messages and history_messages[-1].get("role") == "user":
            history_messages.pop()
        chat_history = engine.fit_history_to_context(
            history_messages,
            query=snapshot.user_input,
            permanent_memories=permanent_memories,
            memories_enabled=snapshot.memories_enabled,
            user_system_instructions=snapshot.user_system_instructions,
            num_ctx=num_ctx,
        )

        self._publish(sink, snapshot, "final_response", "START_FINAL_ANIMATION")
        self._check_cancelled(cancellation_event)
        response, thoughts, memory_command = engine.generate(
            query=snapshot.user_input,
            chat_history=chat_history,
            permanent_memories=permanent_memories,
            memories_enabled=snapshot.memories_enabled,
            user_system_instructions=snapshot.user_system_instructions,
            options=dict(snapshot.model_options),
        )
        if not isinstance(memory_command, MemoryCommand):
            raise ModelOperationError(
                "Generation returned an invalid memory command.",
                operation="generation",
            )
        if not snapshot.memories_enabled:
            memory_command = MemoryCommand()

        if snapshot.translation_enabled:
            self._check_cancelled(cancellation_event)
            self._publish(
                sink,
                snapshot,
                "translation",
                f"Translating to {snapshot.target_language}...",
            )
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
