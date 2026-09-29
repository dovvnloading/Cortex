"""The validator's static size bounds refuse programs that are certain to run out of memory or time.

Each program below used to validate cleanly, so the user was asked to approve
it and the failure arrived from the worker afterwards, after burning a core for
seconds. The validator now refuses it up front, with a code that tells the tray
which bound it crossed.

The bounds are heuristics. They see only what literals decide, so a value that
travels through a name is not tracked; the worker's line budget, wall clock and
job object stay the limits that always hold. The programs in
``_UNTRACKED_GROWTH`` are the ones the bounds deliberately do not see: the
validator accepts them, and a test below runs each in a real worker to show
that a limit stops it.
"""

from __future__ import annotations

import sys
from threading import Event
import time

import pytest

from cortex_backend.execution.code_execution import (
    MAX_CODE_SOURCE_BYTES,
    CodeCapabilities,
    CodeExecutionError,
    CodeExecutionRequest,
    validate_code_source,
)
from cortex_backend.execution.local_code_attempt import LocalCodeAttempt
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
    "percent template width behind a mapping key": (
        '_result = "%(n)10001d" % {"n": 1}',
        "format_width_too_large",
    ),
    "percent template width after a run of unclosed keys": (
        '_result = "' + "%(" * 500 + '%10001d" % 1',
        "format_width_too_large",
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
    "a percent template with a mapping key": '_result = "%(n)5d items" % {"n": 3}',
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


def test_a_long_expression_is_too_complex_on_every_interpreter() -> None:
    """A chain far past the depth limit that every supported parser still accepts.

    Sizes are worked out bottom-up, so a deep chain cannot recurse past the
    depth limit. 200 terms is well over ``MAX_CODE_AST_DEPTH`` and far inside
    the depth at which any supported interpreter's own parser gives up, so the
    validator's own bound is the one that must fire.
    """

    source = "x = " + " * ".join(["2"] * 200)

    with pytest.raises(CodeExecutionError) as refused:
        validate_code_source(source)

    assert refused.value.code == "source_too_complex"


def test_a_very_long_expression_is_refused_not_a_crash() -> None:
    """Past the parser's own depth limit the refusal is still a fail-closed code.

    Which limit fires first is the interpreter's decision. CPython 3.12 stops
    inside ``ast.parse`` at about 3000 chained terms (``syntax_invalid``);
    3.14 parses far deeper and the validator's depth bound answers instead
    (``source_too_complex``). Either way the program is refused with a stable
    code and nothing escapes as an unhandled RecursionError or MemoryError.
    """

    source = "x = " + " * ".join(["2"] * 3_000)

    with pytest.raises(CodeExecutionError) as refused:
        validate_code_source(source)

    assert refused.value.code in {"source_too_complex", "syntax_invalid"}


def test_a_hostile_percent_template_is_checked_in_linear_time() -> None:
    """A run of unclosed mapping keys must not make the template check quadratic.

    Every ``%(`` used to scan to the end of the template looking for a ``)``
    that never came, so a template of them at the source-size limit cost about
    1.8 s of validator CPU, and a request validated it twice. The bound is
    generous next to the few milliseconds a linear scan takes and well under
    what the quadratic one cost, and it counts CPU time so a busy machine does
    not decide the outcome.
    """

    source = '_result = "' + "%(" * 32_000 + '" % 1'
    assert MAX_CODE_SOURCE_BYTES - 4_096 < len(source.encode("utf-8")) <= MAX_CODE_SOURCE_BYTES

    started = time.process_time()
    validate_code_source(source)
    CodeExecutionRequest(
        owner="a" * 64,
        request_id="hostile-template",
        source=source,
        intent_summary="A synthetic template that must not stall validation.",
    )
    elapsed = time.process_time() - started

    assert elapsed < 0.5, f"validating a hostile percent template took {elapsed:.2f}s of CPU"


def test_growth_carried_through_a_name_is_not_tracked() -> None:
    """The one limit of these bounds, pinned so it is never mistaken for a defence.

    No single expression here is too large, so the program validates. The
    worker's line budget, the parent's wall clock and the job object's memory
    limit are what stop it if it is approved and run.
    """

    validate_code_source('big = "a" * 1000\nfor i in range(3):\n    big = big * 1000')


# Programs whose growth the bounds cannot see, and why. Each one is accepted by
# the validator on purpose; none is a consent bypass, because an approved
# program still runs under the worker's memory limit, wall clock and line
# budget. The test below pins that both halves stay true.
_UNTRACKED_GROWTH = {
    # Only list(), tuple(), set(), sorted() and range() calls carry a size.
    "a call's result repeated twice": "_result = len(str(1) * 100000 * 100000)",
    # An f-string's length depends on what is formatted into it.
    "an f-string repeated twice": '_result = len(f"a{1}" * 100000 * 100000)',
    # A name is unknown, and a list display is not refused for a name count
    # the way a string literal is (sequence_bound_required).
    "a list repeated by a name": "n = 10 ** 9\nx = [0] * n\n_result = len(x)",
    # Each augmented assignment is checked alone; their product is not.
    "a string repeated by two augmented assignments": (
        'x = "ab"\nx *= 100000\nx *= 100000\n_result = len(x)'
    ),
}
# Whatever stops such a program, it is one of these and never a result. The
# job object's memory limit makes the allocation fail (memory_limit); the
# process dying, the wall clock and the line budget are the backstops.
_STOPPED_BY_A_WORKER_LIMIT = {"memory_limit", "worker_failed", "worker_timeout", "runtime_limit"}


@pytest.mark.parametrize("source", list(_UNTRACKED_GROWTH.values()), ids=list(_UNTRACKED_GROWTH))
def test_growth_the_bounds_cannot_see_is_accepted_and_stopped_by_the_worker(
    source: str, tmp_path
) -> None:
    validate_code_source(source)

    attempt = LocalCodeAttempt(timeout_seconds=10.0, startup_timeout_seconds=60.0)
    try:
        with pytest.raises(CodeExecutionError) as stopped:
            attempt.evaluate(source, CodeCapabilities(), str(tmp_path), Event())
    finally:
        attempt.close()

    assert stopped.value.code in _STOPPED_BY_A_WORKER_LIMIT


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


_PERCENT_ON_A_NON_STRING = {
    "an integer modulo": "_result = 5 % 3",
    "a negative modulo": "_result = -5 % 3",
    "a float modulo": "_result = 1.5 % 2",
    "a bool modulo": "_result = True % 3",
    "a None left operand": "_result = None % 3",
    "a modulo of a sum": "_result = (1 + 2) % 2",
    "a modulo by a float": "_result = 7 % 0.5",
    "a modulo by zero (a runtime error, not the validator's)": "_result = 7 % 0",
    "an augmented modulo": "x = 10\nx %= 3\n_result = x",
    "a modulo through a name": "x = 5\n_result = x % 2",
    "an integer left of a template-looking right operand": '_result = 5 % "%99999999d"',
    "a chained modulo": "_result = 100 % 7 % 3",
}


@pytest.mark.parametrize("source", list(_PERCENT_ON_A_NON_STRING.values()), ids=list(_PERCENT_ON_A_NON_STRING))
def test_a_percent_with_a_non_string_left_operand_validates_without_crashing(source: str) -> None:
    """Only a string left operand is a printf-style template and is scanned as one.

    An earlier review asked whether ``5 % 3`` could crash the validator, which
    reads ``left.value`` for a template. The template check is guarded by the
    operand being a string, so a number, ``None`` or a bool is plain arithmetic.
    None of these may raise anything, not even a validation error.
    """

    validate_code_source(source)


def test_a_percent_on_a_bytes_literal_is_refused_by_the_constant_rule_not_by_a_crash() -> None:
    with pytest.raises(CodeExecutionError) as refused:
        validate_code_source('_result = b"%99999999d" % 1')
    assert refused.value.code == "constant_not_allowed"


def test_a_string_left_operand_is_still_scanned_as_a_template() -> None:
    with pytest.raises(CodeExecutionError) as refused:
        validate_code_source('_result = "%99999999d" % 1')
    assert refused.value.code == "format_width_too_large"
