"""Explicit, fail-closed dispatch boundary for opted-in agent tools.

A handler is trusted application code, not a model-supplied callable. Its first
invocation must not perform an approval-gated effect before returning
``NeedsApproval``. A later invocation receives ``context.approved`` only after
the run ledger records the corresponding user's one-shot decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event
from time import monotonic

from .protocol import ToolCall, ToolSpec


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Observation for the next model turn; content is never persisted."""

    content: str
    summary: str = "Tool completed"
    success: bool = True


@dataclass(frozen=True, slots=True)
class NeedsApproval:
    """Suspend the call until a user decides; request stays in memory.

    The ledger records only the registered tool name and a fixed reason code,
    never the raw request, arguments, model response, or file content.
    """

    request: dict[str, object]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Cancellation and deadline hints for cooperative tool handlers."""

    cancellation_event: Event
    deadline: float
    approved: bool = False
    clock: Callable[[], float] = monotonic

    def stopped(self) -> bool:
        return self.cancellation_event.is_set() or self.clock() >= self.deadline


class UnregisteredToolError(ValueError):
    """The requested tool was not offered by the application."""


class ToolInterrupted(RuntimeError):
    """The tool operation was stopped by cancellation or its deadline."""


ToolHandler = Callable[[ToolCall, ToolContext], ToolResult | NeedsApproval]


class ToolRegistry:
    """Only explicitly registered names may invoke handlers.

    Register a ``ToolSpec`` and the associated callable as a single entry so
    offered schemas and executable names cannot diverge. Handlers must check
    ``ToolContext.stopped()`` during long operations; this synchronous registry
    cannot forcibly terminate an uncooperative external process.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if not spec.name or spec.name in self._entries:
            raise ValueError("tool name must be nonempty and unique")
        if not callable(handler):
            raise TypeError("tool handler must be callable")
        self._entries[spec.name] = (spec, handler)

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._entries.values()]

    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult | NeedsApproval:
        entry = self._entries.get(call.name)
        if entry is None:
            raise UnregisteredToolError("tool is not registered")
        if context.stopped():
            raise ToolInterrupted("cancelled or deadline elapsed")
        _, handler = entry
        result = handler(call, context)
        if context.stopped():
            raise ToolInterrupted("cancelled or deadline elapsed")
        if not isinstance(result, (ToolResult, NeedsApproval)):
            raise TypeError("tool handlers must return ToolResult or NeedsApproval")
        return result
