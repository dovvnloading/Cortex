"""Counterexamples the validator's property tests found, pinned as plain cases.

``test_code_validator_properties.py`` states what must hold for every input; when
it finds an input that breaks a promise, the smallest such input is written down
here so the regression survives a change to the generators (and reads without
Hypothesis).
"""

from __future__ import annotations

import pytest

from cortex_backend.execution.code_execution import (
    CodeExecutionError,
    CodeExecutionRequest,
    validate_code_source,
)

# Unpaired surrogates: a JSON escape such as "\ud800" decodes to one, and no codec
# can encode it. Built with chr() so the tests never depend on how a source file
# spells them.
_LONE_SURROGATES = tuple(chr(code) for code in (0xD800, 0xDBFF, 0xDC00, 0xDFFF))


def _code(source: str) -> str | None:
    """The rejection code for ``source``, or ``None`` when it is accepted."""

    try:
        validate_code_source(source)
    except CodeExecutionError as error:
        return error.code
    return None


@pytest.mark.parametrize(
    "source",
    [
        _LONE_SURROGATES[0],
        "x = '" + _LONE_SURROGATES[3] + "'\n",
        "print(1)\n" + _LONE_SURROGATES[2],
    ],
    ids=["only-a-surrogate", "inside-a-string", "after-valid-code"],
)
def test_text_that_cannot_be_encoded_is_a_malformed_program_not_a_crash(source: str) -> None:
    """A JSON escape such as ``\\ud800`` decodes to a str no codec can encode.

    ``validate_code_source`` sized it with ``str.encode`` and the
    ``UnicodeEncodeError`` escaped instead of the typed rejection.
    """

    assert _code(source) == "syntax_invalid"
    with pytest.raises(CodeExecutionError) as refused:
        CodeExecutionRequest(owner="tester", request_id="request-1", source=source, intent_summary="probe")
    assert refused.value.code == "syntax_invalid"


@pytest.mark.parametrize(
    "source",
    [
        "for i in range(11):\n    t = [p for p in range(10000)]\n",
        "for i in range(11):\n    t = sum(p for p in range(10000))\n",
        "for i in range(2):\n    for j in range(6):\n        t = {p for p in range(10000)}\n",
        "t = [[q for q in range(1000)] for p in range(101)]\n",
        "t = [[q for q in range(10000) for r in range(2)] for p in range(6)]\n",
    ],
)
def test_a_comprehension_counts_toward_the_work_of_every_loop_around_it(source: str) -> None:
    """Each range was within its own limit, and only the generators of one
    comprehension were multiplied, so 110,000 or 101,000 iterations got through
    a contract that promises no more than 100,000 for nested loops.
    """

    assert _code(source) == "loop_work_too_large"


@pytest.mark.parametrize(
    "source",
    [
        "for i in range(10):\n    t = [p for p in range(10000)]\n",
        "t = [[q for q in range(1000)] for p in range(100)]\n",
        "for i in range(10):\n    t = [[q for q in range(10)] for p in range(1000)]\n",
        "for i in range(0):\n    t = [p for p in range(10000)]\n",
    ],
)
def test_nested_work_exactly_at_the_limit_is_still_accepted(source: str) -> None:
    assert _code(source) is None
