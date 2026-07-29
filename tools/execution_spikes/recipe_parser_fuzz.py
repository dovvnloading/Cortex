"""Deterministic, bounded fuzz qualification for Phase 2 typed recipe parsers.

This probe never executes a recipe or consumes user/model input.  It generates a
small JSON-like corpus in memory, mutates valid image/calculator/check payloads,
and requires every rejection to remain a stable, redacted ``RecipeValidationError``.
The fixed seed and iteration budget make failures reproducible in CI and during
release review.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import random
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from cortex_backend.execution.recipes import (  # noqa: E402
    CalculatorPlan,
    CheckPlan,
    ImageTransformPlan,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_STEPS,
    MAX_RECIPE_PAYLOAD_BYTES,
    RecipeValidationError,
    parse_calculator,
    parse_check,
    parse_image_transform,
)


DEFAULT_ITERATIONS = 2_000
MAX_ITERATIONS = 10_000
DEFAULT_SEED = 20_260_728
_GENERATOR_DEPTH = 3
_REDACTION_MARKER = "CORTEX_FUZZ_SECRET"
_SAFE_ERROR_TEXT = "The requested typed operation is invalid."
_ALLOWED_ERROR_CODES = frozenset(
    {"invalid_image_recipe", "invalid_calculation", "invalid_check", "payload_too_large"}
)
_TEXT_ALPHABET = "abcXYZ0123 _-./\\\n\r\t\x00\u2603"


@dataclass(frozen=True, slots=True)
class _ParserCase:
    name: str
    parse: Callable[[Any], Any]
    expected_type: type[Any]


_PARSERS = (
    _ParserCase("image", parse_image_transform, ImageTransformPlan),
    _ParserCase("calculator", parse_calculator, CalculatorPlan),
    _ParserCase("check", parse_check, CheckPlan),
)


def _random_text(rng: random.Random, *, maximum: int = 32) -> str:
    length = rng.randrange(maximum + 1)
    return "".join(rng.choice(_TEXT_ALPHABET) for _ in range(length))


def _random_json_value(rng: random.Random, *, depth: int = 0) -> Any:
    choices = [None, True, False, -1, 0, 1, 1.5, "", _random_text(rng)]
    if depth < _GENERATOR_DEPTH:
        choices.extend(
            [
                [_random_json_value(rng, depth=depth + 1) for _ in range(rng.randrange(3))],
                {
                    _random_text(rng, maximum=8): _random_json_value(rng, depth=depth + 1)
                    for _ in range(rng.randrange(3))
                },
            ]
        )
    return deepcopy(rng.choice(choices))


def _valid_image(rng: random.Random) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    operations = ("grayscale", "contrast", "brightness", "crop", "resize", "rotate")
    for _ in range(rng.randint(1, MAX_IMAGE_STEPS)):
        operation = rng.choice(operations)
        if operation == "grayscale":
            steps.append({"op": operation})
        elif operation in {"contrast", "brightness"}:
            steps.append({"op": operation, "factor": rng.choice(["0", "0.5", "1.2", "4"])})
        elif operation == "crop":
            steps.append({"op": operation, "x": 0, "y": 0, "width": 16, "height": 12})
        elif operation == "resize":
            steps.append({"op": operation, "width": rng.choice([1, 16, 1024, MAX_IMAGE_DIMENSION]), "height": 12})
        else:
            steps.append({"op": operation, "degrees": rng.choice([90, 180, 270])})
    return {
        "schema_version": "artifact.transform.v1",
        "input_artifact_id": "artifact_fuzz",
        "steps": steps,
        "output_format": rng.choice(["png", "jpeg", "webp"]),
    }


def _valid_calculator(rng: random.Random) -> dict[str, Any]:
    operation = rng.choice(["add", "subtract", "multiply", "divide", "min", "max"])
    operands = [str(rng.randint(-1_000_000, 1_000_000)) for _ in range(rng.randint(2, 16))]
    if operation == "divide":
        operands[1] = rng.choice(["-3", "1", "2", "7"])
    return {"schema_version": "calculation.v1", "operation": operation, "operands": operands}


def _valid_check(rng: random.Random) -> dict[str, Any]:
    operation = rng.choice(
        ["equals", "not_equals", "less_than", "less_or_equal", "greater_than", "greater_or_equal", "is_close"]
    )
    payload: dict[str, Any] = {
        "schema_version": "check.v1",
        "operation": operation,
        "left": str(rng.randint(-1_000_000, 1_000_000)),
        "right": str(rng.randint(-1_000_000, 1_000_000)),
    }
    if operation == "is_close":
        payload["tolerance"] = rng.choice(["0.0001", "0.01", "1"])
    return payload


def _base_payload(case: _ParserCase, rng: random.Random) -> dict[str, Any]:
    if case.name == "image":
        return _valid_image(rng)
    if case.name == "calculator":
        return _valid_calculator(rng)
    return _valid_check(rng)


def _mutate(case: _ParserCase, payload: dict[str, Any], rng: random.Random, index: int) -> Any:
    """Return one bounded mutation; every 17th case is intentionally valid."""

    if index % 17 == 0:
        return payload
    if index % 31 == 0:
        return {"oversized": "x" * (MAX_RECIPE_PAYLOAD_BYTES + 1)}
    if index % 29 == 0:
        return [payload, _random_json_value(rng)]

    candidate = deepcopy(payload)
    mutation = rng.randrange(8)
    if mutation == 0:
        candidate["unknown_field"] = _REDACTION_MARKER
    elif mutation == 1:
        candidate["schema_version"] = _random_text(rng, maximum=16)
    elif mutation == 2:
        candidate.pop(rng.choice(tuple(candidate)), None)
    elif mutation == 3:
        candidate[rng.choice(tuple(candidate))] = _random_json_value(rng)
    elif mutation == 4:
        candidate["operation"] = _random_text(rng, maximum=12)
    elif mutation == 5:
        field = "steps" if case.name == "image" else "operands" if case.name == "calculator" else "left"
        candidate[field] = _random_json_value(rng)
    elif mutation == 6:
        candidate["input_artifact_id" if case.name == "image" else "operation"] = "..\\private\\secret.txt"
    else:
        candidate["nested"] = {"values": [_random_json_value(rng) for _ in range(3)]}
    return candidate


def _payload_fingerprint(payload: Any) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            default=lambda value: "<non-json>",
        )
    except (TypeError, ValueError, OverflowError):
        encoded = type(payload).__name__
    return sha256(encoded.encode("utf-8", "replace")).hexdigest()[:16]


def _run_case(case: _ParserCase, payload: Any) -> str | None:
    try:
        result = case.parse(payload)
    except RecipeValidationError as error:
        if error.code not in _ALLOWED_ERROR_CODES:
            return f"unexpected_error_code:{case.name}:{error.code}"
        if str(error) != _SAFE_ERROR_TEXT or _REDACTION_MARKER in str(error):
            return f"error_redaction_failed:{case.name}"
        return None
    except Exception as error:  # pragma: no cover - qualification failure path.
        return f"unexpected_exception:{case.name}:{type(error).__name__}"
    if not isinstance(result, case.expected_type):
        return f"unexpected_result_type:{case.name}"
    try:
        canonical = result.canonical_json()
        digest = result.digest()
        if not canonical.isascii() or len(digest) != 64:
            return f"canonical_identity_invalid:{case.name}"
    except Exception as error:  # pragma: no cover - qualification failure path.
        return f"canonicalization_failed:{case.name}:{type(error).__name__}"
    return None


def run_fuzz(*, iterations: int = DEFAULT_ITERATIONS, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    if type(iterations) is not int or not 1 <= iterations <= MAX_ITERATIONS:
        raise ValueError("iterations must be between 1 and 10000")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    rng = random.Random(seed)
    accepted = 0
    rejected = 0
    failures: list[dict[str, str]] = []
    for index in range(iterations):
        case = _PARSERS[index % len(_PARSERS)]
        payload = _mutate(case, _base_payload(case, rng), rng, index)
        failure = _run_case(case, payload)
        if failure is None:
            try:
                case.parse(payload)
            except RecipeValidationError:
                rejected += 1
            else:
                accepted += 1
        elif len(failures) < 8:
            failures.append(
                {
                    "case": case.name,
                    "index": str(index),
                    "failure": failure,
                    "payload_fingerprint": _payload_fingerprint(payload),
                }
            )
    return {
        "status": "passed" if not failures else "blocked",
        "seed": seed,
        "iterations": iterations,
        "accepted": accepted,
        "rejected": rejected,
        "unexpected": len(failures),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_fuzz(iterations=args.iterations, seed=args.seed)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, sort_keys=True, separators=(",", ":") if args.json else None))
    return 2 if args.strict and result["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
