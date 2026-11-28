"""The scratch calculator is exact within its stated bounds (#595, #616, #617).

- Fractional literals are read as written, not through a binary float (#595).
- Every rounding happens at the calculator's 34 digits, never at the caller's
  Decimal precision, including negation, bounds checks and rendering (#616).
- ``//`` and ``%`` mean what they mean in the Python syntax the calculator
  accepts: floor division, and a remainder with the divisor's sign (#617).
"""

from __future__ import annotations

from decimal import Context, Decimal, localcontext

import pytest

from cortex_backend.execution.recipes import (
    evaluate_calculator,
    evaluate_check,
    parse_calculator,
    parse_check,
)
from cortex_backend.execution.scratch_compute import evaluate_scratch_expression


def value(expression: str) -> str:
    return evaluate_scratch_expression(expression).value


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("10000000000000000.1 - 10000000000000000", "0.1"),
        ("0.123456789012345678", "0.123456789012345678"),
        ("-0.123456789012345678", "-0.123456789012345678"),
        ("1.5e3 + 0.25", "1500.25"),
        ("1_000.5 * 2", "2001"),
        (".5 + 1.", "1.5"),
    ],
)
def test_fractional_literals_are_read_as_written(expression: str, expected: str) -> None:
    assert value(expression) == expected


@pytest.mark.parametrize("ambient", [28, 10, 3])
def test_the_caller_precision_never_rounds_a_result(ambient: int) -> None:
    with localcontext(Context(prec=ambient)):
        assert value("10000000000000000 + 0.000000000001") == "10000000000000000.000000000001"
        assert value("-(10000000000000000 + 0.000000000001)") == "-10000000000000000.000000000001"
        assert value("abs(-10000000000000000.000000000001)") == "10000000000000000.000000000001"


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("-7 // 3", "-3"),
        ("-7 % 3", "2"),
        ("7 // -3", "-3"),
        ("7 % -3", "-2"),
        ("-7 // -3", "2"),
        ("-7 % -3", "-1"),
        ("7 // 3", "2"),
        ("7 % 3", "1"),
        ("-6 // 3", "-2"),
        ("-6 % 3", "0"),
        ("-7.5 // 2", "-4"),
        ("-7.5 % 2", "0.5"),
        ("7.5 % -2", "-0.5"),
    ],
)
def test_floor_division_and_modulo_follow_python(expression: str, expected: str) -> None:
    assert value(expression) == expected
    assert Decimal(expected) == Decimal(str(eval(expression)))  # noqa: S307 - literal arithmetic


def test_division_by_zero_is_still_refused() -> None:
    from cortex_backend.execution.scratch_compute import ScratchComputeError

    for expression in ("1 // 0", "1 % 0"):
        with pytest.raises(ScratchComputeError):
            evaluate_scratch_expression(expression)


@pytest.mark.parametrize("ambient", [28, 10])
def test_the_typed_calculator_ignores_the_caller_precision(ambient: int) -> None:
    plan = parse_calculator(
        {
            "schema_version": "calculation.v1",
            "operation": "multiply",
            "operands": ["123456789.123456789", "987654321.987654321"],
        }
    )
    with localcontext(Context(prec=34)):
        expected = Decimal("123456789.123456789") * Decimal("987654321.987654321")
    with localcontext(Context(prec=ambient)):
        result = evaluate_calculator(plan)
    assert result == expected
    assert len(result.as_tuple().digits) > 28


def test_an_approximate_check_compares_the_exact_difference() -> None:
    # The exact difference, 246913.578024691355, is just above the tolerance;
    # rounded to ten digits it would fall below it.
    plan = parse_check(
        {
            "schema_version": "check.v1",
            "operation": "is_close",
            "left": "123456.789012345678",
            "right": "-123456.789012345677",
            "tolerance": "246913.578024691354",
        }
    )
    with localcontext(Context(prec=10)):
        assert evaluate_check(plan) is False
