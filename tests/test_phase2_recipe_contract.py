"""Phase 2 typed primitive contracts stay bounded and execution-free."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cortex_backend.execution.recipes import (
    PrimitiveEvaluationError,
    RecipeValidationError,
    evaluate_calculator,
    evaluate_check,
    parse_calculator,
    parse_check,
    parse_image_transform,
)


def _image_payload(**overrides):
    payload = {
        "schema_version": "artifact.transform.v1",
        "input_artifact_id": "artifact_123",
        "steps": [
            {"op": "grayscale"},
            {"op": "contrast", "factor": "1.2"},
            {"op": "resize", "width": 1024, "height": 768},
        ],
        "output_format": "png",
    }
    payload.update(overrides)
    return payload


def test_image_recipe_is_typed_canonical_and_digest_stable():
    first = parse_image_transform(_image_payload())
    reordered = parse_image_transform(
        {
            "output_format": "png",
            "steps": list(reversed(_image_payload()["steps"])),
            "input_artifact_id": "artifact_123",
            "schema_version": "artifact.transform.v1",
        }
    )

    assert first.canonical_json() != reordered.canonical_json()
    assert first.digest() == parse_image_transform(_image_payload()).digest()
    assert first.steps[1].factor == Decimal("1.2")
    assert first.model_dump(mode="json")["strip_metadata"] is True


@pytest.mark.parametrize(
    "payload",
    [
        _image_payload(input_artifact_id="..\\private.txt"),
        _image_payload(steps=[{"op": "blur", "radius": 4}]),
        _image_payload(steps=[{"op": "contrast", "factor": "4.01"}]),
        _image_payload(steps=[{"op": "rotate", "degrees": 45}]),
        _image_payload(extra_path="C:\\Users\\Admin\\secret.txt"),
    ],
)
def test_image_recipe_rejects_paths_unknown_operations_and_unsafe_bounds(payload):
    with pytest.raises(RecipeValidationError) as error:
        parse_image_transform(payload)

    assert error.value.code == "invalid_image_recipe"
    assert "secret" not in str(error.value).lower()
    assert "private" not in str(error.value).lower()


def test_image_recipe_rejects_too_many_steps_and_oversized_payload():
    with pytest.raises(RecipeValidationError) as steps_error:
        parse_image_transform(_image_payload(steps=[{"op": "grayscale"}] * 9))
    assert steps_error.value.code == "invalid_image_recipe"

    with pytest.raises(RecipeValidationError) as payload_error:
        parse_image_transform(_image_payload(note="x" * (64 * 1024)))
    assert payload_error.value.code == "payload_too_large"

    with pytest.raises(RecipeValidationError) as object_error:
        parse_image_transform(_image_payload(note=object()))
    assert object_error.value.code == "invalid_image_recipe"


def test_calculator_is_decimal_only_bounded_and_deterministic():
    plan = parse_calculator(
        {
            "schema_version": "calculation.v1",
            "operation": "divide",
            "operands": [1, 3],
        }
    )

    assert evaluate_calculator(plan) == Decimal("0.333333333333333333")
    assert parse_calculator(
        {
            "schema_version": "calculation.v1",
            "operation": "add",
            "operands": ["40", "2"],
        }
    ).digest()

    with pytest.raises(RecipeValidationError) as float_error:
        parse_calculator(
            {
                "schema_version": "calculation.v1",
                "operation": "add",
                "operands": [1.5, 2],
            }
        )
    assert float_error.value.code == "invalid_calculation"

    with pytest.raises(RecipeValidationError) as zero_error:
        parse_calculator(
            {
                "schema_version": "calculation.v1",
                "operation": "divide",
                "operands": [1, 0],
            }
        )
    assert zero_error.value.code == "invalid_calculation"


def test_calculator_result_limits_fail_closed():
    plan = parse_calculator(
        {
            "schema_version": "calculation.v1",
            "operation": "multiply",
            "operands": ["1000000000000", "1000000000000", 2],
        }
    )

    with pytest.raises(PrimitiveEvaluationError) as error:
        evaluate_calculator(plan)
    assert error.value.code == "result_out_of_bounds"


@pytest.mark.parametrize(
    ("operation", "left", "right", "expected"),
    [
        ("equals", "2", "2", True),
        ("not_equals", "2", "3", True),
        ("less_than", "2", "3", True),
        ("less_or_equal", "2", "2", True),
        ("greater_than", "3", "2", True),
        ("greater_or_equal", "2", "2", True),
    ],
)
def test_check_operations_are_explicit_and_non_executable(operation, left, right, expected):
    plan = parse_check(
        {
            "schema_version": "check.v1",
            "operation": operation,
            "left": left,
            "right": right,
        }
    )
    assert evaluate_check(plan) is expected


def test_check_tolerance_is_only_available_for_is_close():
    close = parse_check(
        {
            "schema_version": "check.v1",
            "operation": "is_close",
            "left": "1",
            "right": "1.01",
            "tolerance": "0.02",
        }
    )
    assert evaluate_check(close) is True

    with pytest.raises(RecipeValidationError):
        parse_check(
            {
                "schema_version": "check.v1",
                "operation": "equals",
                "left": 1,
                "right": 1,
                "tolerance": 0.1,
            }
        )
