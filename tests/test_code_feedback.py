"""Repair hints must describe edits the validator actually accepts.

A hint that names a rule the sandbox does not have is worse than no hint: the
model follows it, is rejected a second time for the same reason, and the single
repair turn is spent. Every hint here is checked against the real validator
rather than against the wording someone intended.
"""

from __future__ import annotations

import re

from cortex_backend.execution.code_execution import (
    CodeExecutionError,
    validate_code_source,
)
from cortex_backend.services.code_feedback import (
    MAX_OBSERVATION_CHARS,
    REJECTION_MESSAGES,
    REPAIR_HINTS,
    describe_rejection,
    format_execution_observation,
    head_tail_truncate,
    repair_prompt,
)


def test_every_hint_has_user_facing_copy_to_go_with_it() -> None:
    """A hint with no message would leave the user with nothing to read.

    The reverse is allowed: a refusal can be explainable to a person and still
    have no correction worth sending back (``not_offered`` is decided for the
    whole turn before the model answers).
    """

    assert not set(REPAIR_HINTS) - set(REJECTION_MESSAGES)
    for code in set(REJECTION_MESSAGES) - set(REPAIR_HINTS):
        assert describe_rejection(code).repairable is False, (
            f"{code!r} is marked repairable but offers the model no instruction"
        )


def test_every_code_the_sandbox_raises_has_user_facing_copy() -> None:
    """A code with no sentence would surface to the user as a bare identifier."""

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "backend"
        / "cortex_backend"
        / "execution"
        / "code_execution.py"
    ).read_text(encoding="utf-8")
    raised = set(re.findall(r'CodeExecutionError\(\s*"([a-z_]+)"', source))

    assert raised, "the scrape found no error codes; the pattern has drifted"
    assert not raised - set(REJECTION_MESSAGES)


def test_the_loop_hints_state_the_real_per_range_cap() -> None:
    """A single range is capped at 10000, well below the total-work cap.

    Without the per-range number, a model told only to keep 'total iterations
    under 100000' writes range(50000), is rejected as bounded_range_required,
    and reads a hint describing what it already did.
    """

    with_50k = "for i in range(50000):\n    pass"
    try:
        validate_code_source(with_50k)
    except CodeExecutionError as exc:
        assert exc.code == "bounded_range_required"
    else:  # pragma: no cover
        raise AssertionError("range(50000) is expected to be rejected")

    assert "10000" in REPAIR_HINTS["bounded_range_required"]
    assert "10000" in REPAIR_HINTS["loop_work_too_large"]
    # The advice itself must validate.
    validate_code_source("for i in range(10000):\n    pass")


def test_the_multiplication_hint_is_not_written_only_about_sequences() -> None:
    """sequence_too_large fires on plain arithmetic too."""

    try:
        validate_code_source("x = 3 * 200000")
    except CodeExecutionError as exc:
        assert exc.code == "sequence_too_large"
    else:  # pragma: no cover
        raise AssertionError("a large integer multiplier is expected to be rejected")

    hint = REPAIR_HINTS["sequence_too_large"].casefold()
    assert "number" in hint
    assert REJECTION_MESSAGES["sequence_too_large"].casefold().count("list or") <= 1


def test_the_exponent_hint_does_not_endorse_a_negative_power() -> None:
    """'1000 or less' reads as allowing -1, which the validator rejects."""

    try:
        validate_code_source("x = 2 ** -1")
    except CodeExecutionError as exc:
        assert exc.code == "exponent_too_large"
    else:  # pragma: no cover
        raise AssertionError("a negative exponent is expected to be rejected")

    assert "between 0 and 1000" in REPAIR_HINTS["exponent_too_large"]
    validate_code_source("x = 2 ** 1000")


def test_refusals_no_edit_can_lift_are_not_marked_repairable() -> None:
    for code in (
        "process_capability_unavailable",
        "source_too_large",
        "payload_too_large",
        "multiple_requests",
        "not_offered",
        # The run workspace is resolved before capabilities are consulted, so
        # dropping the filesystem grant cannot change the outcome.
        "workspace_invalid",
    ):
        assert describe_rejection(code).repairable is False, code


def test_ordinary_subset_violations_are_repairable() -> None:
    for code in ("imports_not_allowed", "unbounded_loop", "call_not_allowed"):
        assert describe_rejection(code).repairable is True, code


def test_an_unknown_code_still_produces_usable_copy() -> None:
    rejection = describe_rejection("something_new_from_a_later_release")

    assert rejection.message
    assert rejection.repairable is False


def test_a_runtime_failure_is_not_described_as_never_having_run() -> None:
    """The model can see its own output; opening with a false claim wastes it."""

    ran = repair_prompt(describe_rejection("runtime_error"))
    refused = repair_prompt(describe_rejection("imports_not_allowed"))

    assert "started running" in ran
    assert "before it ran" not in ran
    assert "before it ran" in refused


def test_the_format_instruction_is_last_in_every_repair_prompt() -> None:
    """Recency is the strongest lever available on a small model."""

    for code in sorted(REPAIR_HINTS):
        prompt = repair_prompt(describe_rejection(code))
        assert prompt.rstrip().endswith("block and no other text."), code


def test_an_observation_never_comes_back_empty() -> None:
    """Ambiguity stalls small models; silence must still say something."""

    observation = format_execution_observation(status="succeeded")

    assert "no output" in observation.casefold()


def test_an_observation_stays_within_its_stated_budget() -> None:
    observation = format_execution_observation(
        status="failed",
        stdout="o" * 40000,
        stderr="e" * 40000,
        value={"key": "v" * 9000},
        truncated=True,
        duration_ms=42,
        error="runtime_error",
    )

    assert len(observation) <= MAX_OBSERVATION_CHARS
    assert observation.startswith("Local code run finished: failed")
    assert "Failure: runtime_error" in observation


def test_the_tail_of_an_error_survives_truncation() -> None:
    """Tracebacks end with the line that explains the failure."""

    stderr = "noise\n" * 5000 + "ZeroDivisionError: division by zero"

    observation = format_execution_observation(status="failed", stderr=stderr)

    assert "ZeroDivisionError: division by zero" in observation


def test_truncation_never_returns_more_than_it_was_given_room_for() -> None:
    for limit in range(0, 90):
        text = "abcdefghij" * 40
        out, truncated = head_tail_truncate(text, limit)
        assert truncated is True
        assert len(out) <= max(limit, 0), (limit, len(out))


def test_short_text_is_returned_untouched() -> None:
    out, truncated = head_tail_truncate("short", 100)

    assert out == "short"
    assert truncated is False
