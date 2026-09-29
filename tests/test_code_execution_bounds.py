"""The bounds on what an approved program may spend, and what each one really measures.

Two layers bound a program. The validator refuses, before the user is asked, a
program whose size or work it can bound statically. The worker then runs what
was approved under a line budget and a wall clock, inside the parent's job
object. These tests pin the unit each bound counts, so a bound cannot drift into
promising more than it delivers.
"""

from __future__ import annotations

from collections import Counter
import sys

import pytest

from cortex_backend.execution import code_execution
from cortex_backend.execution.code_execution import CodeExecutionError, run_code_in_worker


def _trace_events(source: str) -> tuple[Counter[str], int]:
    """Run ``source`` under the real guard, recording every event it was shown."""

    events: Counter[str] = Counter()

    class Recording(code_execution._LineGuard):
        def trace(self, frame, event, arg):  # type: ignore[no-untyped-def]
            if frame.f_code.co_filename == "<cortex-code>":
                events[event] += 1
            return super().trace(frame, event, arg)

    guard = Recording()
    previous = sys.gettrace()
    try:
        sys.settrace(guard.trace)
        exec(compile(source, "<cortex-code>", "exec"), {"__builtins__": {"range": range}})
    finally:
        sys.settrace(previous)
    return events, guard.lines


def test_the_trace_hook_delivers_lines_not_instructions() -> None:
    """The budget is counted in lines because that is all the interpreter reports.

    This was once documented as an instruction watchdog. On the interpreter it
    ran on, no opcode event was ever delivered, and a 100-iteration loop cost
    about two events per iteration -- so the "2,000,000 instructions" budget was
    really ten times looser than it read. If a future interpreter changes what
    the hook delivers, this fails and the budget gets re-derived rather than
    quietly meaning something else.
    """

    events, lines = _trace_events("total = 0\nfor i in range(100):\n    total += i\n")

    assert "opcode" not in events
    assert set(events) <= {"call", "line", "return"}
    assert lines == events["line"]
    assert 100 <= lines <= 400, f"expected one to four line events per iteration, saw {lines}"


def test_a_program_that_runs_past_the_line_budget_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(code_execution, "MAX_CODE_TRACE_LINES", 50)

    with pytest.raises(CodeExecutionError) as stopped:
        run_code_in_worker("total = 0\nfor i in range(1000):\n    total += i\n")

    assert stopped.value.code == "runtime_limit"


def test_a_program_past_its_deadline_is_stopped_at_the_next_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(code_execution, "MAX_CODE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(CodeExecutionError) as stopped:
        run_code_in_worker("total = 0\nfor i in range(1000):\n    total += i\n")

    assert stopped.value.code == "runtime_limit"


def test_a_program_inside_the_line_budget_is_not_stopped() -> None:
    result = run_code_in_worker("total = 0\nfor i in range(1000):\n    total += i\n_result = total")

    assert result.value == 499_500
