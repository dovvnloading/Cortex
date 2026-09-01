"""A deliberately small, deterministic computation language for Cortex.

This is not a Python, shell, or general-purpose code runner.  It accepts one
math expression from a tightly bounded grammar, evaluates it with decimal
arithmetic, and exposes only a short rendered result.  The worker-facing
function in this module has no filesystem, network, subprocess, package, or
environment API.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext
import re
from typing import Any


SCRATCH_COMPUTE_PROFILE = "scratch.auto.v1"
SCRATCH_PAYLOAD_SCHEMA = "scratch.compute.v1"
SCRATCH_RESULT_SCHEMA = "scratch.result.v1"
MAX_EXPRESSION_CHARS = 512
MAX_AST_NODES = 96
MAX_AST_DEPTH = 16
MAX_OPERATIONS = 64
MAX_RESULT_ABS = Decimal("1e18")
MAX_DECIMAL_SCALE = 18
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_AUTO_PREFIX = re.compile(
    r"^\s*(?:calculate|compute|evaluate|solve|what\s+is|what['’]s|how\s+much\s+is)\s+(.+?)\s*[?.!]*\s*$",
    re.IGNORECASE,
)


class ScratchComputeError(ValueError):
    """A stable, non-sensitive safe-compute failure category."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid scratch compute error code")
        self.code = code
        super().__init__("The safe computation could not be completed.")


@dataclass(frozen=True, slots=True)
class ScratchComputeRequest:
    """One owner-scoped request; it carries no path or executable authority."""

    owner: str
    request_id: str
    expression: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or _SAFE_ID.fullmatch(self.owner) is None:
            raise ValueError("owner must be a bounded opaque identifier")
        if not isinstance(self.request_id, str) or _SAFE_ID.fullmatch(self.request_id) is None:
            raise ValueError("request_id must be a bounded opaque identifier")
        validate_scratch_expression(self.expression)


@dataclass(frozen=True, slots=True)
class ScratchComputeResult:
    """The bounded observation that can be shown to a person or a model."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value or len(self.value) > 128:
            raise ValueError("scratch result must be a bounded string")


CancellationCheck = Callable[[], bool]


def _check_cancel(cancel_check: CancellationCheck | None) -> None:
    if cancel_check is None:
        return
    try:
        if bool(cancel_check()):
            raise ScratchComputeError("cancelled")
    except ScratchComputeError:
        raise
    except Exception:
        raise ScratchComputeError("cancellation_check_failed") from None


def _parse(expression: str) -> ast.Expression:
    if (
        not isinstance(expression, str)
        or not expression.strip()
        or len(expression) > MAX_EXPRESSION_CHARS
        or "\x00" in expression
    ):
        raise ScratchComputeError("expression_invalid")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, MemoryError, RecursionError):
        raise ScratchComputeError("expression_invalid") from None
    if not isinstance(tree, ast.Expression):  # defensive for alternate AST producers
        raise ScratchComputeError("expression_invalid")
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES or _ast_depth(tree) > MAX_AST_DEPTH:
        raise ScratchComputeError("expression_too_complex")
    return tree


def _ast_depth(node: ast.AST) -> int:
    children = list(ast.iter_child_nodes(node))
    return 1 + max((_ast_depth(child) for child in children), default=0)


def validate_scratch_expression(expression: str) -> str:
    """Parse and structurally bound an expression without evaluating it."""

    tree = _parse(expression)
    _validate_structure(tree.body)
    return expression.strip()


def _validate_structure(node: ast.AST) -> None:
    """Reject every AST shape outside the small arithmetic language."""

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ScratchComputeError("expression_not_allowed")
        return
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.UAdd, ast.USub)):
            raise ScratchComputeError("expression_not_allowed")
        _validate_structure(node.operand)
        return
    if isinstance(node, ast.BinOp):
        if not isinstance(
            node.op,
            (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow),
        ):
            raise ScratchComputeError("expression_not_allowed")
        _validate_structure(node.left)
        _validate_structure(node.right)
        return
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.keywords
            or node.func.id not in {"abs", "sqrt", "min", "max", "round"}
            or not node.args
            or len(node.args) > 16
        ):
            raise ScratchComputeError("expression_not_allowed")
        _validate_structure(node.func)
        for argument in node.args:
            _validate_structure(argument)
        return
    if isinstance(node, ast.Name) and node.id in {"abs", "sqrt", "min", "max", "round"}:
        return
    raise ScratchComputeError("expression_not_allowed")


class _Evaluator:
    def __init__(self, cancel_check: CancellationCheck | None) -> None:
        self._cancel_check = cancel_check
        self._operations = 0

    def _operation(self) -> None:
        _check_cancel(self._cancel_check)
        self._operations += 1
        if self._operations > MAX_OPERATIONS:
            raise ScratchComputeError("operation_limit")

    def evaluate(self, node: ast.AST) -> Decimal:
        self._operation()
        if isinstance(node, ast.Constant):
            return self._constant(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self.evaluate(node.operand)
            return _bounded(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp):
            return self._binary(node)
        if isinstance(node, ast.Call):
            return self._call(node)
        raise ScratchComputeError("expression_not_allowed")

    @staticmethod
    def _constant(value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScratchComputeError("expression_not_allowed")
        try:
            converted = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ScratchComputeError("number_invalid") from None
        return _bounded(converted)

    def _binary(self, node: ast.BinOp) -> Decimal:
        left = self.evaluate(node.left)
        right = self.evaluate(node.right)
        try:
            with localcontext(_decimal_context()):
                if isinstance(node.op, ast.Add):
                    value = left + right
                elif isinstance(node.op, ast.Sub):
                    value = left - right
                elif isinstance(node.op, ast.Mult):
                    value = left * right
                elif isinstance(node.op, ast.Div):
                    value = left / right
                elif isinstance(node.op, ast.FloorDiv):
                    value = left // right
                elif isinstance(node.op, ast.Mod):
                    value = left % right
                elif isinstance(node.op, ast.Pow):
                    if right != right.to_integral_value() or not -12 <= int(right) <= 12:
                        raise ScratchComputeError("exponent_out_of_bounds")
                    value = left ** int(right)
                else:
                    raise ScratchComputeError("expression_not_allowed")
        except ScratchComputeError:
            raise
        except (DivisionByZero, InvalidOperation, Overflow, ValueError, ZeroDivisionError):
            raise ScratchComputeError("arithmetic_failed") from None
        return _bounded(+value)

    def _call(self, node: ast.Call) -> Decimal:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ScratchComputeError("expression_not_allowed")
        name = node.func.id
        values = tuple(self.evaluate(argument) for argument in node.args)
        try:
            with localcontext(_decimal_context()):
                if name == "abs" and len(values) == 1:
                    value = abs(values[0])
                elif name == "sqrt" and len(values) == 1 and values[0] >= 0:
                    value = values[0].sqrt()
                elif name == "min" and 2 <= len(values) <= 16:
                    value = min(values)
                elif name == "max" and 2 <= len(values) <= 16:
                    value = max(values)
                elif name == "round" and len(values) in {1, 2}:
                    places = 0
                    if len(values) == 2:
                        if values[1] != values[1].to_integral_value():
                            raise ScratchComputeError("rounding_invalid")
                        places = int(values[1])
                    if not 0 <= places <= 12:
                        raise ScratchComputeError("rounding_invalid")
                    value = values[0].quantize(
                        Decimal("1").scaleb(-places), rounding=ROUND_HALF_EVEN
                    )
                else:
                    raise ScratchComputeError("expression_not_allowed")
        except ScratchComputeError:
            raise
        except (DivisionByZero, InvalidOperation, Overflow, ValueError):
            raise ScratchComputeError("arithmetic_failed") from None
        return _bounded(+value)


def _decimal_context() -> Context:
    return Context(prec=34, Emax=24, Emin=-MAX_DECIMAL_SCALE)


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) > MAX_RESULT_ABS:
        raise ScratchComputeError("result_out_of_bounds")
    try:
        if value.as_tuple().exponent < -MAX_DECIMAL_SCALE:
            value = value.quantize(
                Decimal("1").scaleb(-MAX_DECIMAL_SCALE),
                rounding=ROUND_HALF_EVEN,
                context=_decimal_context(),
            )
    except (InvalidOperation, ValueError):
        raise ScratchComputeError("result_precision_exceeded") from None
    if len(value.as_tuple().digits) > 34:
        raise ScratchComputeError("result_precision_exceeded")
    return value


def _render(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if len(rendered) > 128:
        raise ScratchComputeError("result_precision_exceeded")
    return rendered


def evaluate_scratch_expression(
    expression: str,
    *,
    cancel_check: CancellationCheck | None = None,
) -> ScratchComputeResult:
    """Evaluate one expression with no host capabilities or dynamic execution."""

    tree = _parse(expression)
    evaluator = _Evaluator(cancel_check)
    value = evaluator.evaluate(tree.body)
    _check_cancel(cancel_check)
    return ScratchComputeResult(value=_render(value))


def extract_automatic_expression(prompt: str) -> str | None:
    """Return an unambiguous user-requested computation, if one is present.

    Automatic use deliberately requires an explicit computation verb.  Natural
    language is not guessed into a program, and invalid expressions simply
    fall back to ordinary chat.
    """

    if not isinstance(prompt, str) or len(prompt) > 100_000:
        return None
    match = _AUTO_PREFIX.match(prompt)
    if match is None:
        return None
    candidate = match.group(1).strip()
    try:
        validate_scratch_expression(candidate)
    except ScratchComputeError:
        return None
    return candidate


def scratch_worker_main(
    connection: Any,
    cancel_event: Any,
    expression: str,
) -> None:
    """Process entrypoint. It returns a compact, redacted message only."""

    try:
        # Keep process bootstrap separate from the small expression's wall-clock
        # budget.  On a busy Windows desktop, importing the worker can take
        # longer than evaluating a bounded expression.
        connection.send({"ok": True, "event": "ready"})
        result = evaluate_scratch_expression(
            expression,
            cancel_check=lambda: bool(cancel_event.is_set()),
        )
        connection.send({"ok": True, "value": result.value})
    except ScratchComputeError as exc:
        connection.send({"ok": False, "code": exc.code})
    except Exception:
        connection.send({"ok": False, "code": "worker_failed"})
    finally:
        try:
            connection.close()
        except Exception:
            pass


def scratch_result_payload(value: str) -> Mapping[str, str]:
    """Return the fixed durable result schema for a completed computation."""

    return {"schema_version": SCRATCH_RESULT_SCHEMA, "value": value}


__all__ = [
    "MAX_EXPRESSION_CHARS",
    "SCRATCH_COMPUTE_PROFILE",
    "SCRATCH_PAYLOAD_SCHEMA",
    "SCRATCH_RESULT_SCHEMA",
    "ScratchComputeError",
    "ScratchComputeRequest",
    "ScratchComputeResult",
    "evaluate_scratch_expression",
    "extract_automatic_expression",
    "scratch_result_payload",
    "scratch_worker_main",
    "validate_scratch_expression",
]
