"""Property tests for the restricted-Python validator.

``validate_code_source`` decides which text becomes a program the user is asked
to approve and Cortex then runs, and the text comes from a model or the API, so
it is attacker-influenced. The hand-written cases in ``test_code_execution.py``
and ``test_code_harness_contract.py`` pin the contract's examples; these pin the
promises that must hold for every input:

* it never fails any way but its typed ``CodeExecutionError``, and only with a
  code Cortex has user-facing copy for;
* the same text always gets the same answer;
* a program written only from the documented constructs is accepted, and runs to
  completion inside the documented budgets;
* every documented budget (source size, line count, AST nodes and depth, range
  length, nested loop work) is exact, in both directions;
* a forbidden construct is rejected with its own code wherever it appears.

The generators are deliberately small and the examples bounded: the ``ci``
profile in ``conftest.py`` makes each run draw the same inputs.
"""

from __future__ import annotations

import ast
import time

from hypothesis import example, given, strategies as st
import pytest

from cortex_backend.execution import code_execution
from cortex_backend.execution.code_execution import (
    CodeCapabilities,
    CodeExecutionError,
    capabilities_required_by_source,
    run_code_in_worker,
    validate_code_source,
)
from cortex_backend.services.code_feedback import REJECTION_MESSAGES

# Parsing arbitrary text makes the interpreter warn about odd literals and escapes
# (``SyntaxWarning: invalid decimal literal``); the parse result is what is under test.
pytestmark = pytest.mark.filterwarnings("ignore::SyntaxWarning")

MAX_SOURCE_BYTES = code_execution.MAX_CODE_SOURCE_BYTES
MAX_NODES = code_execution.MAX_CODE_AST_NODES
MAX_DEPTH = code_execution.MAX_CODE_AST_DEPTH
MAX_RANGE = code_execution.MAX_CODE_LOOP_ITERATIONS
MAX_TOTAL_WORK = code_execution.MAX_CODE_TOTAL_ITERATIONS
MAX_SECONDS = code_execution.MAX_CODE_TIMEOUT_SECONDS
MAX_OUTPUT_BYTES = code_execution.MAX_CODE_OUTPUT_BYTES
MAX_LINES = 2_048  # the newline budget written into validate_code_source

# Unpaired surrogates: a JSON escape such as "\ud800" decodes to one, and no codec
# can encode it. Built with chr() so the tests never depend on how a source file
# spells them.
_LONE_SURROGATES = tuple(chr(code) for code in (0xD800, 0xDBFF, 0xDC00, 0xDFFF))


def _rejection(source: str) -> CodeExecutionError | None:
    """The typed rejection for ``source``, or ``None`` when it is accepted.

    Anything else the validator raises is deliberately not caught: that is the
    failure these tests exist to find.
    """

    try:
        validate_code_source(source)
    except CodeExecutionError as error:
        return error
    return None


def _code(source: str) -> str | None:
    error = _rejection(source)
    return None if error is None else error.code


def _outcome(source: str) -> tuple[str, str] | None:
    error = _rejection(source)
    return None if error is None else (error.code, str(error))


# -- Generators -------------------------------------------------------------------

# Every character Python can represent, lone surrogates included.
_ANY_TEXT = st.text(alphabet=st.characters(exclude_categories=()), max_size=120)
# Surrogates are a sliver of the code space, so give them their own generator: ASCII
# text with one unpaired surrogate in it, the shape of a mangled model reply.
_ASCII_TEXT = st.text(alphabet=st.characters(codec="ascii"), max_size=30)
_SURROGATE_TEXT = st.builds(
    lambda head, lone, tail: head + lone + tail,
    _ASCII_TEXT,
    st.sampled_from(_LONE_SURROGATES),
    _ASCII_TEXT,
)

_SOUP_PIECES = (
    "x", "y", "cortex", "fs", "net", "process", "read_text", "run", "__class__", "1", "0", "10000",
    "1e999", "0x1f", "1_0", "1j", "b'x'", "'s'", '"""', "f'{x}'", "range", "print", "len", "eval",
    "(", ")", "[", "]", "{", "}", ",", ":", ".", ";", "=", "+=", ":=", "+", "-", "*", "**", "//",
    "%", "@", "<<", "<", "==", "...", "\\", "#", "\t", "\n", "    ", " ", "for", "in", "if", "else",
    "while", "def", "lambda", "class", "import", "global", "nonlocal", "async", "await", "yield",
    "try", "with", "raise", "del", "assert", "pass", "break", "continue", "return", "and", "not",
    "is",
)
# Fragments of Python glued together at random: nearly all of it is a syntax error,
# but enough parses to reach the visitor with something odd in it.
_PYTHON_SOUP = st.builds(
    lambda pieces, separator: separator.join(pieces),
    st.lists(st.sampled_from(_SOUP_PIECES), max_size=30),
    st.sampled_from(("", " ", "\n")),
)

_DATA = ("a", "b", "c", "d")
_LOOP_VARIABLES = ("i", "j", "k")
_PRELUDE = "a = 1\nb = 2\nc = 3\nd = 4\n"
# Keeps every integer the generated programs compute small, however they loop.
_MODULUS = 10_007
_COMPARISONS = ("<", "<=", ">", ">=", "==", "!=")


class _Budget:
    """How many more statements a generated program may contain."""

    def __init__(self, statements: int) -> None:
        self.left = statements


def _expression(draw, names: tuple[str, ...], depth: int) -> str:
    """An integer expression that cannot raise, over ``names``."""

    if depth <= 0 or draw(st.integers(0, 3)) == 0:
        return draw(st.one_of(st.sampled_from(names), st.integers(0, 50).map(str)))
    kind = draw(
        st.sampled_from(
            ("binary", "floor", "modulo", "abs", "extreme", "power", "conditional", "sum", "comprehension")
        )
    )
    left = _expression(draw, names, depth - 1)
    if kind == "binary":
        return f"({left} {draw(st.sampled_from(('+', '-', '*')))} {_expression(draw, names, depth - 1)})"
    if kind == "floor":
        return f"({left} // {draw(st.integers(1, 9))})"
    if kind == "modulo":
        return f"({left} % {draw(st.integers(1, 9))})"
    if kind == "abs":
        return f"abs({left})"
    if kind == "extreme":
        return f"{draw(st.sampled_from(('min', 'max')))}({left}, {_expression(draw, names, depth - 1)})"
    if kind == "power":
        return f"({left} ** {draw(st.integers(0, 2))})"
    if kind == "conditional":
        return f"({left} if {_condition(draw, names, depth - 1)} else {_expression(draw, names, depth - 1)})"
    if kind == "sum":
        return f"sum([{left}, {_expression(draw, names, depth - 1)}, {_expression(draw, names, depth - 1)}])"
    return _comprehension(draw, names, depth - 1)


def _condition(draw, names: tuple[str, ...], depth: int) -> str:
    left = _expression(draw, names, min(depth, 1))
    right = _expression(draw, names, min(depth, 1))
    kind = draw(st.sampled_from(("compare", "membership", "negation", "conjunction", "disjunction")))
    if depth <= 0 or kind == "compare":
        return f"{left} {draw(st.sampled_from(_COMPARISONS))} {right}"
    if kind == "membership":
        return f"({left} in [{right}, {_expression(draw, names, 0)}])"
    if kind == "negation":
        return f"(not {_condition(draw, names, depth - 1)})"
    joiner = "and" if kind == "conjunction" else "or"
    return f"({_condition(draw, names, depth - 1)} {joiner} {_condition(draw, names, depth - 1)})"


def _comprehension(draw, names: tuple[str, ...], depth: int) -> str:
    """An integer built from a comprehension over a short literal range."""

    variable = "m"
    inner = (*names, variable)
    clause = f"for {variable} in range({draw(st.integers(0, 4))})"
    if draw(st.booleans()):
        clause += f" if {_condition(draw, inner, 0)}"
    element = _expression(draw, inner, depth)
    kind = draw(st.sampled_from(("list", "set", "dict", "generator")))
    if kind == "list":
        return f"sum([{element} {clause}])"
    if kind == "set":
        return f"len({{{element} {clause}}})"
    if kind == "dict":
        return f"len({{{element}: {_expression(draw, inner, 0)} {clause}}})"
    return f"sum({element} {clause})"


def _statement(draw, names: tuple[str, ...], budget: _Budget, loops: int) -> list[str]:
    """One statement (as lines) built only from documented constructs."""

    budget.left -= 1
    options = ["assign", "augmented", "annotated", "swap", "print", "tautology", "pass"]
    if loops:
        options.append("exit")
    if budget.left > 1:
        options += ["if", "if_else"]
        if loops < len(_LOOP_VARIABLES):
            options += ["for", "for"]
    kind = draw(st.sampled_from(options))
    target = draw(st.sampled_from(_DATA))
    if kind == "assign":
        return [f"{target} = ({_expression(draw, names, 3)}) % {_MODULUS}"]
    if kind == "augmented":
        operator = draw(st.sampled_from(("+=", "-=")))
        return [f"{target} {operator} ({_expression(draw, names, 2)}) % {_MODULUS}"]
    if kind == "annotated":
        return [f"{target}: int = ({_expression(draw, names, 2)}) % {_MODULUS}"]
    if kind == "swap":
        other = draw(st.sampled_from(_DATA))
        return [f"{target}, {other} = {other}, {target}"]
    if kind == "print":
        if draw(st.booleans()):
            return [f"print({_expression(draw, names, 2)})"]
        return [f'print(f"{target}={{{target}:>4}}")']
    if kind == "tautology":
        condition = _condition(draw, names, 1)
        return [f"assert {condition} or not {condition}"]
    if kind == "exit":
        return [f"if {_condition(draw, names, 1)}:", f"    {draw(st.sampled_from(('break', 'continue')))}"]
    if kind == "pass":
        return ["pass"]
    if kind == "for":
        variable = _LOOP_VARIABLES[loops]
        bounds = draw(
            st.one_of(
                st.integers(0, 4).map(lambda stop: f"{stop}"),
                st.tuples(st.integers(0, 3), st.integers(0, 6)).map(lambda pair: f"{pair[0]}, {pair[1]}"),
                st.tuples(st.integers(0, 3), st.integers(0, 8), st.integers(1, 3)).map(
                    lambda triple: f"{triple[0]}, {triple[1]}, {triple[2]}"
                ),
            )
        )
        body = _block(draw, (*names, variable), budget, loops + 1, draw(st.integers(1, 3)))
        return [f"for {variable} in range({bounds}):", *_indent(body)]
    lines = [f"if {_condition(draw, names, 1)}:", *_indent(_block(draw, names, budget, loops, draw(st.integers(1, 2))))]
    if kind == "if_else":
        lines += ["else:", *_indent(_block(draw, names, budget, loops, draw(st.integers(1, 2))))]
    return lines


def _block(draw, names: tuple[str, ...], budget: _Budget, loops: int, count: int) -> list[str]:
    lines: list[str] = []
    for _ in range(count):
        lines += _statement(draw, names, budget, loops) if budget.left > 0 else ["pass"]
    return lines


def _indent(lines: list[str], levels: int = 1) -> list[str]:
    return [f"{'    ' * levels}{line}" for line in lines]


@st.composite
def _safe_blocks(draw) -> list[str]:
    """The top-level statements of a program that is valid, total and small."""

    budget = _Budget(draw(st.integers(1, 18)))
    blocks = ["\n".join(_statement(draw, _DATA, budget, 0)) for _ in range(draw(st.integers(1, 4)))]
    blocks.append("_result = sum([a, b, c, d])")
    return blocks


def _assemble(blocks: list[str]) -> str:
    return _PRELUDE + "\n".join(blocks) + "\n"


# Programs the validator is known to accept, as material for mutation.
_CORPUS = (
    "total = 0\nfor i in range(1, 101):\n    total += i\nprint(f\"sum = {total}\")\n_result = total\n",
    "data = [3, 1, 2]\n_result = sorted([data[i] for i in range(3)])\n",
    "x = 5\nif x > 3:\n    y = 1\nelif x > 1:\n    y = 2\nelse:\n    y = 3\nassert y == 1\n",
    "names = {'a': 1}\n_result = {k: names['a'] for k in range(2)}\n",
    "text = 'hello'\n_result = text[0:2] + text[-1]\n",
    "listing = cortex.fs.listdir('.')\n_result = len(listing)\n",
    "page = cortex.net.get('https://example.com')\n_result = len(page)\n",
)


@st.composite
def _mutated_sources(draw) -> str:
    """A near-valid program: a known-good one with a few random edits."""

    source = draw(st.one_of(st.sampled_from(_CORPUS), _safe_blocks().map(_assemble)))
    for _ in range(draw(st.integers(1, 4))):
        position = draw(st.integers(0, len(source)))
        kind = draw(st.sampled_from(("insert", "delete", "replace", "truncate", "repeat")))
        piece = draw(st.sampled_from(_SOUP_PIECES))
        end = min(len(source), position + draw(st.integers(1, 6)))
        if kind == "insert":
            source = source[:position] + piece + source[position:]
        elif kind == "delete":
            source = source[:position] + source[end:]
        elif kind == "replace":
            source = source[:position] + piece + source[end:]
        elif kind == "repeat":
            source = source[:end] + source[position:end] + source[end:]
        else:
            source = source[:position]
    return source


_ARBITRARY_SOURCES = st.one_of(_ANY_TEXT, _SURROGATE_TEXT, _PYTHON_SOUP, _mutated_sources())


# -- Only a typed error, with a documented code ------------------------------------


@given(source=_ARBITRARY_SOURCES)
@example(source=_LONE_SURROGATES[0])
@example(source="x = '" + _LONE_SURROGATES[3] + "'")
@example(source="")
@example(source="\x00")
@example(source="x = " + "(" * 500 + "1" + ")" * 500)
def test_arbitrary_text_is_accepted_or_rejected_with_a_documented_code(source: str) -> None:
    error = _rejection(source)

    if error is None:
        assert validate_code_source(source) == source
        return
    assert error.code in REJECTION_MESSAGES, (
        f"the validator raised {error.code!r}, which has no user-facing message in code_feedback"
    )


@given(source=_ARBITRARY_SOURCES)
def test_an_accepted_program_never_needs_more_capabilities_than_it_names(source: str) -> None:
    if _rejection(source) is not None:
        return

    required = capabilities_required_by_source(source)

    assert isinstance(required, CodeCapabilities)
    if "cortex" not in source:
        assert required == CodeCapabilities()


_BROKER_CALLS = (
    ("filesystem", "cortex.fs.read_text('notes.txt')"),
    ("filesystem", "cortex.fs.write_text('notes.txt', 'hello')"),
    ("filesystem", "cortex.fs.listdir('.')"),
    ("network", "cortex.net.get('https://example.com')"),
    ("network", "cortex.network.get('https://example.com')"),
    ("process", "cortex.process.run(['python'])"),
)


@given(calls=st.lists(st.sampled_from(_BROKER_CALLS), max_size=6))
def test_the_capabilities_a_program_needs_are_exactly_the_brokers_it_calls(
    calls: list[tuple[str, str]],
) -> None:
    source = "".join(f"r{index} = {call}\n" for index, (_, call) in enumerate(calls)) + "pass\n"

    required = capabilities_required_by_source(source)

    wanted = {name for name, _ in calls}
    assert required == CodeCapabilities(
        filesystem="filesystem" in wanted, process="process" in wanted, network="network" in wanted
    )


# -- Deterministic ------------------------------------------------------------------


@given(source=_ARBITRARY_SOURCES, unrelated=_ARBITRARY_SOURCES)
def test_the_same_text_always_gets_the_same_answer(source: str, unrelated: str) -> None:
    first = _outcome(source)
    _outcome(unrelated)  # nothing a previous call did may leak into the next
    second = _outcome(source)

    assert first == second


# -- Documented constructs are accepted and finish inside the budgets ---------------


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> str:
    return str(tmp_path_factory.mktemp("property-worker"))


@given(blocks=_safe_blocks())
def test_a_program_of_documented_constructs_is_accepted_and_finishes_inside_the_budgets(
    workspace: str, blocks: list[str]
) -> None:
    source = _assemble(blocks)

    assert validate_code_source(source) == source
    assert capabilities_required_by_source(source) == CodeCapabilities()
    started = time.monotonic()
    result = run_code_in_worker(source, None, workspace)
    elapsed = time.monotonic() - started

    assert elapsed < MAX_SECONDS
    assert result.duration_ms <= MAX_SECONDS * 1_000
    assert len(result.stdout.encode("utf-8")) <= MAX_OUTPUT_BYTES
    assert result.stderr == ""
    assert type(result.value) is int


# -- Every documented budget is exact -----------------------------------------------


def _static_loop_work(tree: ast.AST) -> tuple[bool, int]:
    """An independent model of the documented loop limits.

    Returns whether every loop and comprehension counts over a plain
    ``range(...)`` of at most ``MAX_RANGE`` steps, and the most iterations any
    loop body or comprehension element can be asked to run -- the product of the
    lengths of the loops around it and, inside a comprehension, of the generators
    up to it.
    """

    bounded = True
    peak = 0

    def length(iterable: ast.expr) -> int | None:
        if not (
            isinstance(iterable, ast.Call)
            and isinstance(iterable.func, ast.Name)
            and iterable.func.id == "range"
            and not iterable.keywords
            and 1 <= len(iterable.args) <= 3
            and all(isinstance(arg, ast.Constant) and type(arg.value) is int for arg in iterable.args)
        ):
            return None
        try:
            steps = len(range(*(arg.value for arg in iterable.args)))
        except (ValueError, OverflowError):
            return None
        return steps if steps <= MAX_RANGE else None

    def walk(node: ast.AST, multiplier: int) -> None:
        nonlocal bounded, peak
        if isinstance(node, ast.For):
            iterables = [node.iter]
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            iterables = [generator.iter for generator in node.generators]
        else:
            iterables = []
        for iterable in iterables:
            steps = length(iterable)
            if steps is None:
                bounded = False
                continue
            multiplier *= steps
            peak = max(peak, multiplier)
        for child in ast.iter_child_nodes(node):
            walk(child, multiplier)

    walk(tree, 1)
    return bounded, peak


_RANGE_LENGTHS = (0, 1, 2, 3, 10, 31, 32, 100, 316, 317, 1_000, 10_000, 10_001)


@st.composite
def _range_call(draw) -> str:
    """``range(...)`` with a literal length that often sits on a budget boundary."""

    steps = draw(st.one_of(st.sampled_from(_RANGE_LENGTHS), st.integers(0, 12_000)))
    form = draw(st.sampled_from(("stop", "start_stop", "step")))
    if form == "stop":
        return f"range({steps})"
    if form == "start_stop":
        start = draw(st.integers(0, 5))
        return f"range({start}, {start + steps})"
    step = draw(st.integers(1, 5))
    return f"range(0, {steps * step}, {step})"


@st.composite
def _comprehension_source(draw, depth: int = 0) -> str:
    names = ("p", "q", "r")[depth:]
    generators = " ".join(
        f"for {name} in {draw(_range_call())}" for name in names[: draw(st.integers(1, 2))]
    )
    element = draw(_comprehension_source(depth + 1)) if depth < 2 and draw(st.booleans()) else names[0]
    kind = draw(st.sampled_from(("list", "set", "dict", "generator")))
    if kind == "list":
        return f"[{element} {generators}]"
    if kind == "set":
        return f"{{{element} {generators}}}"
    if kind == "dict":
        return f"{{{element}: 1 {generators}}}"
    return f"sum({element} {generators})"


@st.composite
def _loop_nests(draw) -> str:
    """A program that is nothing but loops and comprehensions, nested at random."""

    lines: list[str] = []

    def statement(depth: int, indent: int) -> None:
        pad = "    " * indent
        kind = draw(st.sampled_from(("for", "for", "comprehension", "pass")))
        if kind == "for" and depth < 3:
            lines.append(f"{pad}for {_LOOP_VARIABLES[depth]} in {draw(_range_call())}:")
            for _ in range(draw(st.integers(1, 2))):
                statement(depth + 1, indent + 1)
        elif kind == "comprehension":
            lines.append(f"{pad}t = {draw(_comprehension_source())}")
        else:
            lines.append(f"{pad}pass")

    for _ in range(draw(st.integers(1, 2))):
        statement(0, 0)
    return "\n".join(lines) + "\n"


@given(source=_loop_nests())
@example(source="for i in range(11):\n    t = [p for p in range(10000)]\n")
@example(source="for i in range(10):\n    for j in range(10):\n        t = [p for p in range(1001)]\n")
@example(source="t = [[q for q in range(1000)] for p in range(101)]\n")
def test_the_loop_limits_are_exact_in_both_directions(source: str) -> None:
    bounded, peak = _static_loop_work(ast.parse(source))
    within_limits = bounded and peak <= MAX_TOTAL_WORK

    code = _code(source)

    if within_limits:
        assert code is None, f"the documented limits allow this program but it was rejected: {code}"
    else:
        assert code in {"bounded_range_required", "loop_work_too_large"}, code


def _tree_depth(node: ast.AST) -> int:
    return 1 + max((_tree_depth(child) for child in ast.iter_child_nodes(node)), default=0)


@given(depth=st.integers(0, 60), width=st.integers(0, 5_000))
@example(depth=29, width=1)
@example(depth=30, width=1)
@example(depth=1, width=4_090)
@example(depth=1, width=4_091)
def test_the_ast_node_and_depth_budgets_are_exact(depth: int, width: int) -> None:
    source = "x = " + "[" * depth + ", ".join(["1"] * width) + "]" * depth + "\n"
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        assert _code(source) == "syntax_invalid"
        return
    nodes = sum(1 for _ in ast.walk(tree))

    code = _code(source)

    if nodes > MAX_NODES or _tree_depth(tree) > MAX_DEPTH:
        assert code == "source_too_complex"
    else:
        assert code is None


@given(lines=st.integers(MAX_LINES - 8, MAX_LINES + 8))
def test_the_line_budget_is_exact(lines: int) -> None:
    source = "pass\n" * lines

    assert _code(source) == ("source_too_complex" if source.count("\n") > MAX_LINES else None)


@given(offset=st.integers(-3, 3), wide=st.booleans())
def test_the_size_budget_is_counted_in_bytes_and_is_exact(offset: int, wide: bool) -> None:
    # "x = 1\n#" is 7 bytes; what follows is a comment, so it only has to be text.
    filler = MAX_SOURCE_BYTES + offset - 7
    padding = "\u00e9" * (filler // 2) + "a" * (filler % 2) if wide else "a" * filler
    source = "x = 1\n#" + padding
    assert len(source.encode("utf-8")) == MAX_SOURCE_BYTES + offset

    assert _code(source) == ("source_too_large" if offset > 0 else None)


# -- Forbidden constructs are rejected with their own code, wherever they are --------

_FORBIDDEN = (
    ("imports_not_allowed", "import os"),
    ("imports_not_allowed", "import os.path as p"),
    ("imports_not_allowed", "from os import path"),
    ("imports_not_allowed", "from . import sibling"),
    ("function_definitions_not_allowed", "def f(x):\n    return x"),
    ("function_definitions_not_allowed", "async def f():\n    pass"),
    ("function_definitions_not_allowed", "f = lambda x: x"),
    ("class_definitions_not_allowed", "class C:\n    pass"),
    ("unbounded_loop", "while a < 3:\n    a += 1"),
    ("unbounded_loop", "while True:\n    pass"),
    ("try_not_allowed", "try:\n    a = 1\nexcept Exception:\n    a = 2"),
    ("with_not_allowed", "with a as b:\n    pass"),
    ("raise_not_allowed", "raise ValueError"),
    ("delete_not_allowed", "del a"),
    ("syntax_not_allowed", "global g"),
    ("syntax_not_allowed", "nonlocal g"),
    ("syntax_not_allowed", "(y := 1)"),
    ("syntax_not_allowed", "z = [*a]"),
    ("attribute_not_allowed", "z = a.real"),
    ("attribute_not_allowed", "z = (1).__class__"),
    ("attribute_not_allowed", "z = ().__class__.__mro__"),
    ("attribute_not_allowed", "z = cortex.fs.enabled"),
    ("attribute_not_allowed", "z = cortex.fs.__class__"),
    ("attribute_not_allowed", "a.real = 1"),
    ("call_not_allowed", "z = 'x'.upper()"),
    ("call_not_allowed", "z = ''.join(['a'])"),
    ("call_not_allowed", "lst = []\nlst.append(1)"),
    ("call_not_allowed", "z = eval('1')"),
    ("call_not_allowed", "z = open('f')"),
    ("call_not_allowed", "z = __import__('os')"),
    ("call_not_allowed", "z = getattr(a, 'real')"),
    ("call_not_allowed", "z = (lambda: 1)()"),
    ("name_not_allowed", "z = __builtins__"),
    ("name_not_allowed", "z = __import__"),
    ("name_not_allowed", "z = open"),
    ("name_not_allowed", "z = eval"),
    ("name_not_allowed", "z = type"),
    ("bounded_range_required", "for c in 'abc':\n    pass"),
    ("bounded_range_required", "for i in range(a):\n    pass"),
    ("bounded_range_required", "for i in range(-1):\n    pass"),
    ("bounded_range_required", "for i in range(0, 5, 0):\n    pass"),
    ("bounded_range_required", f"for i in range({MAX_RANGE + 1}):\n    pass"),
    ("bounded_range_required", "z = [x for x in [1, 2]]"),
    ("loop_target_not_allowed", "for a.b in range(3):\n    pass"),
    ("loop_work_too_large", "for i in range(1000):\n    for j in range(101):\n        pass"),
    ("loop_work_too_large", "z = [(i, j) for i in range(1000) for j in range(101)]"),
    ("operator_not_allowed", "z = a @ b"),
    ("operator_not_allowed", "z = a << 1"),
    ("operator_not_allowed", "z = a | b"),
    ("operator_not_allowed", "z = ~a"),
    ("exponent_too_large", "z = a ** 1001"),
    ("exponent_too_large", "z = a ** b"),
    ("exponent_too_large", "z = a ** -1"),
    ("sequence_too_large", "z = a * 100001"),
    ("sequence_bound_required", "z = 'ab' * a"),
    ("constant_not_allowed", "z = b'x'"),
    ("constant_not_allowed", "z = 1j"),
    ("constant_not_allowed", "z = ..."),
    ("constant_not_allowed", "z = 1e999"),
)

# Each takes the forbidden statement and buries it deeper in the program.
_WRAPPERS = (
    lambda body: body,
    lambda body: "if a:\n" + "\n".join(_indent(body.split("\n"))),
    lambda body: "for i in range(3):\n" + "\n".join(_indent(body.split("\n"))),
    lambda body: "if a:\n    pass\nelse:\n" + "\n".join(_indent(body.split("\n"))),
    lambda body: "for i in range(2):\n    if b:\n" + "\n".join(_indent(body.split("\n"), 2)),
)


@pytest.mark.parametrize(
    ("code", "snippet"), _FORBIDDEN, ids=[f"{code}-{number}" for number, (code, _) in enumerate(_FORBIDDEN)]
)
def test_each_forbidden_construct_alone_is_rejected_with_its_documented_code(code: str, snippet: str) -> None:
    assert code in REJECTION_MESSAGES
    assert _code(_PRELUDE + snippet + "\n") == code


@given(
    blocks=_safe_blocks(),
    slot=st.integers(0, 10),
    forbidden=st.sampled_from(_FORBIDDEN),
    wrap=st.sampled_from(_WRAPPERS),
)
def test_a_forbidden_construct_is_rejected_with_its_own_code_wherever_it_appears(
    blocks: list[str], slot: int, forbidden: tuple[str, str], wrap
) -> None:
    code, snippet = forbidden
    position = slot % (len(blocks) + 1)
    source = _assemble([*blocks[:position], wrap(snippet), *blocks[position:]])

    assert _code(source) == code
