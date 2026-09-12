"""Typed progress events shared by headless services and UI adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


ProgressPhase = Literal[
    "analysis",
    "thoughts",
    "loading_model",
    "translation",
    # Translation is a post-process over an answer that already exists, so a
    # failure there is reported rather than raised: the answer is kept and the
    # user is told it was not translated.
    "translation_failed",
    # Live model output, published as it arrives. api/routes.py maps these to
    # generation.content_delta / generation.thinking_delta, the event names the
    # frontend already renders incrementally.
    "content_delta",
    "thinking_delta",
]


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One safe, owned progress update for an interactive generation job."""

    job_id: str
    thread_id: str
    phase: ProgressPhase
    message: str
    # Structured payload for phases that carry more than a status line -- the
    # token deltas above put their text here. Kept out of ``message`` so the
    # human-readable status and the machine-readable content stay separate.
    data: Mapping[str, Any] | None = None


@runtime_checkable
class ProgressSink(Protocol):
    """Consumer boundary for typed service progress."""

    def publish(self, event: ProgressEvent) -> None:
        """Publish one progress event without owning transport concerns."""


class NullProgressSink:
    """Default sink for callers that do not need progress updates."""

    def publish(self, event: ProgressEvent) -> None:
        del event
