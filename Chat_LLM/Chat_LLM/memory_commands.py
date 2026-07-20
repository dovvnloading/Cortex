"""Apply validated model memory actions at the UI boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from generation_types import MemoryCommand


@dataclass(frozen=True)
class MemoryActionResult:
    """Summary of actions applied after user confirmation."""

    added_count: int = 0
    cleared: bool = False
    clear_skipped: bool = False


def apply_memory_command(
    memory_manager,
    command: MemoryCommand | None,
    *,
    confirm_clear: Callable[[], bool],
) -> MemoryActionResult:
    """Apply additions and conditionally destructive actions.

    The callback is intentionally required even when a clear request is present;
    model output never receives authority to erase permanent memory directly.
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

    for memo in command.additions:
        memory_manager.add_memo(memo)

    return MemoryActionResult(
        added_count=len(command.additions),
        cleared=cleared,
        clear_skipped=clear_skipped,
    )
