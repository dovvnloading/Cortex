"""Apply validated model memory actions at the UI boundary."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from cortex_backend.core.generation import MemoryCommand


@dataclass(frozen=True)
class MemoryActionResult:
    """Summary of actions applied after explicit user confirmation."""

    added_count: int = 0
    cleared: bool = False
    clear_skipped: bool = False
    pending_additions: tuple[str, ...] = ()


def apply_memory_command(
    memory_manager,
    command: MemoryCommand | None,
    *,
    confirm_clear: Callable[[], bool],
    confirm_additions: Callable[[tuple[str, ...]], bool] | None = None,
) -> MemoryActionResult:
    """Apply explicitly confirmed memory actions.

    The callback is intentionally required even when a clear request is present;
    model output never receives authority to erase permanent memory directly.
    Additions are proposals too: unless a caller supplies a confirmation
    callback, they are returned as pending and are not written to storage.
    """
    if not isinstance(command, MemoryCommand) or not command.has_actions:
        return MemoryActionResult()

    cleared = False
    clear_skipped = False
    if command.clear_requested:
        if confirm_clear():
            memory_manager.clear_memos()
            cleared = True
        else:
            clear_skipped = True

    pending_additions = command.additions
    if command.additions and confirm_additions is not None:
        if confirm_additions(command.additions):
            for memo in command.additions:
                memory_manager.add_memo(memo)
            pending_additions = ()

    return MemoryActionResult(
        added_count=len(command.additions) - len(pending_additions),
        cleared=cleared,
        clear_skipped=clear_skipped,
        pending_additions=pending_additions,
    )
