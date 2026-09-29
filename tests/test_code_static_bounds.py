"""The validator's static size bounds refuse programs that are certain to run out of memory or time.

Each program below used to validate cleanly, so the user was asked to approve
it and the failure arrived from the worker afterwards, after burning a core for
seconds. The validator now refuses it up front, with a code that tells the tray
which bound it crossed.

The bounds are heuristics. They see only what literals decide, so a value that
travels through a name is not tracked; the worker's line budget, wall clock and
job object stay the limits that always hold.
"""

from __future__ import annotations

import sys

import pytest

from cortex_backend.execution.code_execution import (
    CodeExecutionError,
    CodeExecutionRequest,
    validate_code_source,
)
from cortex_backend.services.code_feedback import (
    REJECTION_MESSAGES,
    REPAIR_HINTS,
    describe_rejection,
)


_ESCAPES = {
    "chained repeat of a string": ('_result = "a" * 100000 * 100000', "sequence_too_large"),
    "repeat then join past the item cap": (
        "_result = [0] * 60000 + [0] * 60000",
        "sequence_too_large",
    ),
    "repeat of a list built from a range": (
        "_result = list(range(10000)) * 100000",
        "sequence_too_large",
    ),
    "comprehension nested in a comprehension": (
        "_result = [[y for y in range(10000)] for x in range(10000)]",
        "loop_work_too_large",
    ),
    "nested comprehensions each under the cap": (
        "_result = [[y for y in range(400)] for x in range(400)]",
        "loop_work_too_large",
    ),
    "comprehension inside a loop": (
        "for i in range(1000):\n    row = [j for j in range(1000)]",
        "loop_work_too_large",
    ),
    "chained power": ("_result = ((9 ** 1000) ** 1000) ** 1000", "integer_too_large"),
    "power of a power": ("_result = (2 ** 1000) ** 1000", "integer_too_large"),
    "format width": ('_result = f"{1:>10000000}"', "format_width_too_large"),
    "format width just over the cap": ('_result = f"{1:>10001}"', "format_width_too_large"),
    "format precision": ('_result = f"{1.5:.10000000f}"', "format_width_too_large"),
    "format width chosen at run time": (
        'w = 5\n_result = f"{1:>{w}}"',
        "format_width_not_constant",
    ),
    "percent template width": ('_result = "%10000000d" % 1', "format_width_too_large"),
    "percent template width just over the cap": (
        '_result = "%10001d" % 1',
        "format_width_too_large",
    ),
    "percent template precision": ('_result = "%.10001f" % 1.5', "format_width_too_large"),
    "percent template width taken from an argument": (
        '_result = "%*d" % (5, 1)',
        "format_width_not_constant",
    ),
    "range with a computed bound": ("_result = list(range(10 ** 9))", "bounded_range_required"),
    "range past the per-range cap outside a loop": (
        "_result = list(range(10001))",
        "bounded_range_required",
    ),
    "range with a variable bound outside a loop": (
        "n = 3\n_result = list(range(n))",
        "bounded_range_required",
    ),
    "range whose work multiplies through a loop": (
        "for i in range(100):\n    t = sum(range(10000))",
        "loop_work_too_large",
    ),
    "list repeated on every pass of a loop": (
        "for i in range(1000):\n    row = [0] * 100000",
        "allocation_too_large",
    ),
    "list repeated on every pass of a comprehension": (
        "_result = [[0] * 100000 for _ in range(1000)]",
        "allocation_too_large",
    ),
    "power on an augmented assignment": ("x = 9\nx **= 1000000000", "exponent_too_large"),
    "repeat on an augmented assignment": ('s = "a"\ns *= 100001', "sequence_too_large"),
    "shift on an augmented assignment": ("x = 1\nx <<= 5", "operator_not_allowed"),
}


@pytest.mark.parametrize(("source", "code"), list(_ESCAPES.values()), ids=list(_ESCAPES))
def test_a_program_certain_to_exhaust_memory_or_time_is_refused_up_front(
    source: str, code: str
) -> None:
    with pytest.raises(CodeExecutionError) as refused:
        validate_code_source(source)
    assert refused.value.code == code

    # The same refusal stops the request before a job or an approval prompt exists.
    with pytest.raises(CodeExecutionError) as never_asked:
        CodeExecutionRequest(
            owner="a" * 64,
            request_id="static-bound",
            source=source,
            intent_summary="A synthetic program that must never reach approval.",
        )
    assert never_asked.value.code == code


_STILL_ALLOWED = {
    "a large power": "_result = 2 ** 1000",
    "a product of two large powers": "_result = 10 ** 1000 * 10 ** 1000",
    "a rule of dashes": '_result = "-" * 80 + "\\n"',
    "the longest literal repeat": "_result = [0] * 100000",
    "the most a loop may build": 'for i in range(100):\n    row = "a" * 100000',
    "a format width exactly at the cap": '_result = f"{1:>10000}"',
    "a percent width exactly at the cap": '_result = "%10000d" % 1',
    "a float format": '_result = f"{3.14159:.2f}"',
    "a padded format": "_result = f\"{'x':>10}\"",
    "a zero-padded format": '_result = f"{5:010d}"',
    "a percent template": '_result = "%5.2f items" % 2.5',
    "a literal percent sign": '_result = "100%"',
    "an escaped percent sign": '_result = "%d%%" % 5',
    "a sorted range": "_result = sorted(range(100))",
    "a small nested comprehension": "_result = [[y for y in range(10)] for x in range(10)]",
    "the widest single comprehension": "_result = [i * j for i in range(300) for j in range(300)]",
    "range work exactly at the cap": (
        "t = 0\nfor i in range(10):\n    for j in range(10):\n        t += sum(range(1000))"
    ),
    "a comprehension over the widest range": "_result = sum([i for i in range(10000)])",
    "a square": "_result = 144 ** 2",
    "a float power": "_result = 2.5 ** 100",
}


@pytest.mark.parametrize("source", list(_STILL_ALLOWED.values()), ids=list(_STILL_ALLOWED))
def test_the_static_bounds_do_not_refuse_ordinary_programs(source: str) -> None:
    validate_code_source(source)


def test_an_enormous_integer_literal_is_refused_even_when_the_interpreter_would_parse_it() -> None:
    """CPython refuses a literal over 4300 digits unless that limit is switched off.

    A user can switch it off with an environment variable, so the validator
    keeps its own bound rather than relying on the interpreter's default.
    """

    source = "x = " + "9" * 30_000
    with pytest.raises(CodeExecutionError) as by_default:
        validate_code_source(source)
    assert by_default.value.code == "syntax_invalid"

    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        with pytest.raises(CodeExecutionError) as unlimited:
            validate_code_source(source)
    finally:
        sys.set_int_max_str_digits(previous)
    assert unlimited.value.code == "integer_too_large"


def test_a_very_long_expression_is_too_complex_not_a_crash() -> None:
    """Sizes are worked out bottom-up, so a deep chain cannot recurse past the depth limit."""

    source = "x = " + " * ".join(["2"] * 3_000)

    with pytest.raises(CodeExecutionError) as refused:
        validate_code_source(source)

    assert refused.value.code == "source_too_complex"


def test_growth_carried_through_a_name_is_not_tracked() -> None:
    """The one limit of these bounds, pinned so it is never mistaken for a defence.

    No single expression here is too large, so the program validates. The
    worker's line budget, the parent's wall clock and the job object's memory
    limit are what stop it if it is approved and run.
    """

    validate_code_source('big = "a" * 1000\nfor i in range(3):\n    big = big * 1000')


def test_every_new_refusal_can_be_explained_and_repaired() -> None:
    for code in (
        "integer_too_large",
        "allocation_too_large",
        "format_width_too_large",
        "format_width_not_constant",
    ):
        assert code in REJECTION_MESSAGES
        assert code in REPAIR_HINTS
        assert describe_rejection(code).repairable is True

    # The correction each hint offers must itself be accepted.
    validate_code_source('x = 5\n_result = f"{x:>10}"')
    validate_code_source("_result = 2 ** 1000")
    validate_code_source("_result = [0] * 10000 + [1] * 10000")
