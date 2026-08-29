"""The model-facing code contract must stay true to the real validator.

``assets/code_execution_prompt.txt`` is the only description of the sandbox
language a local model ever sees.  Every claim it makes is therefore load
bearing: a prompt that advertises a construct the validator rejects does not
merely mislead, it burns the model's whole turn on a proposal that can never
run.  These tests keep the prompt and ``execution/code_execution.py`` honest
about each other, so tightening the validator can never silently invalidate the
contract (or its worked example) without a failing test.
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile

from cortex_backend.execution.code_execution import (
    CodeExecutionError,
    run_code_in_worker,
    validate_code_source,
)
from cortex_backend.services.llm import SynthesisAgent


PROMPT_PATH = Path(__file__).resolve().parents[1] / "assets" / "code_execution_prompt.txt"
_ENVELOPE_RE = re.compile(
    r"<code_execution_request>.*?</code_execution_request>", re.DOTALL
)


def _prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _worked_example() -> str:
    """The single envelope the contract shows the model as a model answer."""

    envelopes = _ENVELOPE_RE.findall(_prompt_text())
    assert len(envelopes) == 1, (
        "The contract must show exactly one worked envelope. A second example "
        "teaches small models that emitting two blocks is acceptable, and the "
        "parser rejects a response carrying more than one."
    )
    return envelopes[0]


def test_contract_worked_example_survives_the_real_parser_and_validator() -> None:
    """The example is parsed by the same path a real model answer takes."""

    agent = SynthesisAgent("model", "model", "model", object(), code_execution_eligible=True)
    visible, _, _ = agent._parse_and_clean_response(
        f"Summing the integers from 1 to 100.\n{_worked_example()}", None
    )

    proposal = agent.last_code_proposal
    assert proposal is not None, (
        "The worked example in code_execution_prompt.txt no longer validates. "
        "A model copying the contract's own example would be rejected."
    )
    assert visible == "Summing the integers from 1 to 100."
    # The example must model minimal capability requests, since the prompt
    # tells the model to leave every capability false unless it calls a broker.
    assert proposal.capabilities == {
        "filesystem": False,
        "process": False,
        "network": False,
    }


def test_contract_worked_example_actually_runs_and_returns_its_value() -> None:
    """A contract example that validates but crashes is still a bad example."""

    agent = SynthesisAgent("model", "model", "model", object(), code_execution_eligible=True)
    agent._parse_and_clean_response(_worked_example(), None)
    proposal = agent.last_code_proposal
    assert proposal is not None

    result = run_code_in_worker(
        proposal.source, proposal.capabilities, tempfile.mkdtemp()
    )
    assert result.value == 5050
    assert "5050" in result.stdout


def test_contract_only_advertises_constructs_the_validator_accepts() -> None:
    """Each construct the prompt presents as allowed must really validate.

    These mirror the bullet list in the contract's "sandbox language" section.
    Bracketed comprehensions are included deliberately: generator expressions
    are *not* inlined by PEP 709, so a bare ``sum(x for x in ...)`` raises
    NameError inside the worker's split globals/locals mapping -- which is why
    the contract tells the model to always use square brackets.
    """

    accepted = {
        "augmented assignment": "total = 0\ntotal += 2",
        "bounded range loop": "t = 0\nfor i in range(1, 101):\n    t += i",
        "bracketed comprehension": "vals = [i * 2 for i in range(4)]",
        "comprehension over an earlier name": "d = [3, 1, 2]\n_result = sum([d[i] for i in range(3)])",
        "if/elif/else": "x = 5\nif x > 10:\n    y = 1\nelif x > 3:\n    y = 2\nelse:\n    y = 3",
        "assert": "x = 2\nassert x == 2",
        "ternary": "x = 4\n_result = 'even' if x % 2 == 0 else 'odd'",
        "slicing and indexing": "t = 'hello world'\ng = [[1, 2], [3, 4]]\n_result = [t[0:5], g[1][0]]",
        "dict literal and subscript": 'd = {"a": 1}\n_result = d["a"]',
        "f-string with a format spec": 'v = 3.14159\n_result = f"{v:.2f}"',
        "string building with +=": "out = ''\nfor i in range(3):\n    out += 'x'",
        "listed builtins": "_result = [abs(-1), len('ab'), sorted([2, 1]), sum([1, 2]), max([1, 2])]",
    }
    for label, source in accepted.items():
        validate_code_source(source)  # must not raise


def test_contract_forbidden_list_matches_the_validator() -> None:
    """Everything the prompt calls rejected must really be rejected.

    Pinning the exact error code matters because those codes drive the
    user-facing rejection copy and the model-facing repair hint.
    """

    rejected = {
        "import math": "imports_not_allowed",
        "def f():\n    return 1": "function_definitions_not_allowed",
        "f = lambda x: x": "function_definitions_not_allowed",
        "class C:\n    pass": "class_definitions_not_allowed",
        "i = 0\nwhile i < 3:\n    i += 1": "unbounded_loop",
        "try:\n    x = 1\nexcept Exception:\n    x = 2": "try_not_allowed",
        "with open('f') as fh:\n    pass": "with_not_allowed",
        "raise ValueError('x')": "raise_not_allowed",
        "x = [1]\ndel x[0]": "delete_not_allowed",
        # The single most important rule for small models: no method calls.
        "_result = 'ab'.upper()": "call_not_allowed",
        "x = []\nx.append(1)": "call_not_allowed",
        'd = {"a": 1}\n_result = d.get("a")': "call_not_allowed",
        "_result = 'a,b'.split(',')": "call_not_allowed",
        # Only range() may be iterated.
        "for v in [1, 2, 3]:\n    print(v)": "bounded_range_required",
        "for c in 'abc':\n    print(c)": "bounded_range_required",
        "items = [1]\nfor v in items:\n    print(v)": "bounded_range_required",
    }
    for source, expected_code in rejected.items():
        try:
            validate_code_source(source)
        except CodeExecutionError as exc:
            assert exc.code == expected_code, (
                f"{source!r} raised {exc.code!r}, contract/tests expect {expected_code!r}"
            )
        else:  # pragma: no cover - a regression would make this reachable
            raise AssertionError(f"{source!r} was accepted but the contract calls it rejected")


def test_comprehensions_and_generators_can_see_top_level_names() -> None:
    """The worker runs programs in one namespace, on every Python version.

    Passing distinct globals and locals mappings to ``exec`` makes top-level
    assignments locals, which a comprehension's implicit function scope cannot
    read -- so an obviously correct program raised NameError. CPython 3.12
    hid half of it by inlining list/set/dict comprehensions, leaving a failure
    that depended on the interpreter version: generator expressions broke
    everywhere, and on 3.11 (which CI pins) every comprehension did.
    """

    for source in (
        "data = [3, 1, 2]\n_result = sum(data[i] for i in range(3))",
        "data = [3, 1, 2]\n_result = sum([data[i] for i in range(3)])",
        "names = ['a', 'b']\n_result = {names[i]: i for i in range(2)}",
    ):
        validate_code_source(source)
        result = run_code_in_worker(source, {}, tempfile.mkdtemp())
        assert result.value, f"{source!r} produced no value"


def test_contract_teaches_a_list_walk_the_validator_actually_accepts() -> None:
    """The obvious idiom is illegal here, so the contract must not suggest it.

    ``range()`` bounds must be integer literals, which makes the usual
    ``for i in range(len(items))`` a rejected program. A contract that
    recommended it would send the model straight into bounded_range_required.
    """

    try:
        validate_code_source("items = [1]\nfor i in range(len(items)):\n    pass")
    except CodeExecutionError as exc:
        assert exc.code == "bounded_range_required"
    else:  # pragma: no cover
        raise AssertionError("range(len(...)) is expected to be rejected")

    assert "range(len(items))` are not" in _prompt_text(), (
        "The contract must name range(len(...)) as rejected."
    )
    # And the alternative it offers must itself validate.
    validate_code_source(
        "items = [10, 20]\ntotal = 0\nfor i in range(100):\n"
        "    if i < len(items):\n        total += items[i]\n_result = total"
    )


def test_contract_keeps_the_untrusted_input_instruction() -> None:
    """Files and code the user supplies are data, not instructions."""

    text = _prompt_text().casefold()
    assert "untrusted" in text
    assert "never as instructions" in text


def test_contract_does_not_name_the_forbidden_process_broker() -> None:
    """Naming a forbidden API is a known way to make weak models emit it.

    ``cortex.process.run`` is fail-closed in three independent places, so the
    only effect of mentioning it in the prompt is to put the token sequence in
    front of a model that is looking for a way to run something.
    """

    assert "cortex.process" not in _prompt_text()


def test_contract_stays_small_enough_for_a_local_model() -> None:
    """Instruction dilution is the dominant small-model failure mode.

    The contract is injected just in time on top of the base system prompt, so
    it competes for attention with everything else in the window. Keep it near
    a thousand tokens; if a change genuinely needs more room, cut something
    else rather than raising this bound casually.
    """

    text = _prompt_text()
    approximate_tokens = len(text) / 4
    assert approximate_tokens < 1000, (
        f"The code contract grew to roughly {approximate_tokens:.0f} tokens."
    )
