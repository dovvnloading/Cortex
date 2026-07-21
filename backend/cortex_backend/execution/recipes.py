"""Strict Phase 2 contracts for trusted, fixed-function primitives.

This module validates typed plans only.  It does not decode images, evaluate model
source, access paths, launch processes, or publish artifacts.  A later qualified
provider must consume these plans behind the production sandbox and artifact gates.
"""

from __future__ import annotations

from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import re
from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


MAX_IMAGE_STEPS = 8
MAX_IMAGE_DIMENSION = 16_384
MAX_RECIPE_PAYLOAD_BYTES = 64 * 1024
MAX_DECIMAL_ABS = Decimal("1e12")
MAX_RESULT_ABS = Decimal("1e24")
_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class RecipeValidationError(ValueError):
    """Stable, non-sensitive validation failure for a proposed primitive."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid recipe validation code")
        self.code = code
        super().__init__("The requested typed operation is invalid.")


class PrimitiveEvaluationError(ValueError):
    """Stable failure for a bounded deterministic calculator/check operation."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("invalid primitive evaluation code")
        self.code = code
        super().__init__("The typed operation could not be evaluated safely.")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def canonical_json(self) -> str:
        """Return a stable representation suitable for an idempotency digest."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("ascii")).hexdigest()


def _bounded_decimal(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) > MAX_DECIMAL_ABS:
        raise ValueError("decimal is outside the supported bound")
    digits = value.as_tuple().digits
    exponent = value.as_tuple().exponent
    if len(digits) > 18 or exponent < -12 or exponent > 12:
        raise ValueError("decimal precision is outside the supported bound")
    return value


def _coerce_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("decimal must be an integer, string, or Decimal")
    if not isinstance(value, (Decimal, int, str)):
        raise ValueError("decimal type is unsupported")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("decimal is invalid") from None


def _bounded_result(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) > MAX_RESULT_ABS:
        raise PrimitiveEvaluationError("result_out_of_bounds")
    if value.as_tuple().exponent < -18:
        try:
            value = value.quantize(
                Decimal("1e-18"),
                rounding=ROUND_HALF_EVEN,
                context=Context(prec=50, Emax=24, Emin=-18),
            )
        except (ArithmeticError, InvalidOperation):
            raise PrimitiveEvaluationError("result_precision_exceeded") from None
    if len(value.as_tuple().digits) > 50:
        raise PrimitiveEvaluationError("result_precision_exceeded")
    return value


class GrayscaleStep(_StrictModel):
    op: Literal["grayscale"]


class ContrastStep(_StrictModel):
    op: Literal["contrast"]
    factor: Decimal

    _coerce_factor = field_validator("factor", mode="before")(_coerce_decimal)
    _factor = field_validator("factor")(_bounded_decimal)

    @model_validator(mode="after")
    def _within_image_range(self) -> "ContrastStep":
        if not Decimal("0") <= self.factor <= Decimal("4"):
            raise ValueError("contrast factor is outside the supported range")
        return self


class BrightnessStep(_StrictModel):
    op: Literal["brightness"]
    factor: Decimal

    _coerce_factor = field_validator("factor", mode="before")(_coerce_decimal)
    _factor = field_validator("factor")(_bounded_decimal)

    @model_validator(mode="after")
    def _within_image_range(self) -> "BrightnessStep":
        if not Decimal("0") <= self.factor <= Decimal("4"):
            raise ValueError("brightness factor is outside the supported range")
        return self


class CropStep(_StrictModel):
    op: Literal["crop"]
    x: Annotated[int, Field(strict=True, ge=0, le=MAX_IMAGE_DIMENSION - 1)]
    y: Annotated[int, Field(strict=True, ge=0, le=MAX_IMAGE_DIMENSION - 1)]
    width: Annotated[int, Field(strict=True, ge=1, le=MAX_IMAGE_DIMENSION)]
    height: Annotated[int, Field(strict=True, ge=1, le=MAX_IMAGE_DIMENSION)]


class ResizeStep(_StrictModel):
    op: Literal["resize"]
    width: Annotated[int, Field(strict=True, ge=1, le=MAX_IMAGE_DIMENSION)]
    height: Annotated[int, Field(strict=True, ge=1, le=MAX_IMAGE_DIMENSION)]


class RotateStep(_StrictModel):
    op: Literal["rotate"]
    degrees: Literal[90, 180, 270]


ImageStep = Annotated[
    Union[GrayscaleStep, ContrastStep, BrightnessStep, CropStep, ResizeStep, RotateStep],
    Field(discriminator="op"),
]


def _validate_operands(values: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    return tuple(_bounded_decimal(value) for value in values)


def _coerce_operands(values: Any) -> tuple[Decimal, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("operands must be an array")
    return tuple(_coerce_decimal(value) for value in values)


class ImageTransformPlan(_StrictModel):
    schema_version: Literal["artifact.transform.v1"]
    input_artifact_id: Annotated[str, Field(min_length=1, max_length=128)]
    steps: tuple[ImageStep, ...] = Field(min_length=1, max_length=MAX_IMAGE_STEPS)
    output_format: Literal["png", "jpeg", "webp"]
    strip_metadata: Literal[True] = True

    @field_validator("input_artifact_id")
    @classmethod
    def _opaque_artifact_id(cls, value: str) -> str:
        if _SAFE_ARTIFACT_ID.fullmatch(value) is None:
            raise ValueError("artifact identifier must be opaque")
        return value


class CalculatorPlan(_StrictModel):
    schema_version: Literal["calculation.v1"]
    operation: Literal["add", "subtract", "multiply", "divide", "min", "max"]
    operands: tuple[Decimal, ...] = Field(min_length=2, max_length=16)

    _coerce_operands = field_validator("operands", mode="before")(_coerce_operands)
    _operands = field_validator("operands")(_validate_operands)

    @model_validator(mode="after")
    def _validate_divisors(self) -> "CalculatorPlan":
        if self.operation == "divide" and any(operand == 0 for operand in self.operands[1:]):
            raise ValueError("division by zero is not supported")
        return self


class CheckPlan(_StrictModel):
    schema_version: Literal["check.v1"]
    operation: Literal[
        "equals",
        "not_equals",
        "less_than",
        "less_or_equal",
        "greater_than",
        "greater_or_equal",
        "is_close",
    ]
    left: Decimal
    right: Decimal
    tolerance: Decimal | None = None

    _coerce_left = field_validator("left", mode="before")(_coerce_decimal)
    _coerce_right = field_validator("right", mode="before")(_coerce_decimal)
    _coerce_tolerance = field_validator("tolerance", mode="before")(
        lambda value: None if value is None else _coerce_decimal(value)
    )
    _left = field_validator("left")(_bounded_decimal)
    _right = field_validator("right")(_bounded_decimal)
    _tolerance = field_validator("tolerance")(_bounded_decimal)

    @model_validator(mode="after")
    def _validate_tolerance(self) -> "CheckPlan":
        if self.operation == "is_close":
            if self.tolerance is None or self.tolerance <= 0:
                raise ValueError("is_close requires a positive tolerance")
        elif self.tolerance is not None:
            raise ValueError("tolerance is valid only for is_close")
        return self


def _parse_payload(payload: Mapping[str, Any], model: type[_StrictModel], code: str) -> _StrictModel:
    if not isinstance(payload, Mapping):
        raise RecipeValidationError(code)
    try:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        raise RecipeValidationError(code) from None
    if len(encoded.encode("ascii")) > MAX_RECIPE_PAYLOAD_BYTES:
        raise RecipeValidationError("payload_too_large")
    candidate = dict(payload)
    for sequence_field in ("steps", "operands"):
        if isinstance(candidate.get(sequence_field), list):
            candidate[sequence_field] = tuple(candidate[sequence_field])
    try:
        return model.model_validate(candidate)
    except ValidationError:
        raise RecipeValidationError(code) from None


def parse_image_transform(payload: Mapping[str, Any]) -> ImageTransformPlan:
    result = _parse_payload(payload, ImageTransformPlan, "invalid_image_recipe")
    assert isinstance(result, ImageTransformPlan)
    return result


def parse_calculator(payload: Mapping[str, Any]) -> CalculatorPlan:
    result = _parse_payload(payload, CalculatorPlan, "invalid_calculation")
    assert isinstance(result, CalculatorPlan)
    return result


def parse_check(payload: Mapping[str, Any]) -> CheckPlan:
    result = _parse_payload(payload, CheckPlan, "invalid_check")
    assert isinstance(result, CheckPlan)
    return result


def evaluate_calculator(plan: CalculatorPlan) -> Decimal:
    """Evaluate only the finite, bounded operations represented by ``plan``."""
    with localcontext(Context(prec=34, Emax=24, Emin=-18)):
        try:
            if plan.operation == "add":
                result = sum(plan.operands, Decimal("0"))
            elif plan.operation == "subtract":
                result = plan.operands[0]
                for operand in plan.operands[1:]:
                    result -= operand
            elif plan.operation == "multiply":
                result = Decimal("1")
                for operand in plan.operands:
                    result *= operand
            elif plan.operation == "divide":
                result = plan.operands[0]
                for operand in plan.operands[1:]:
                    result /= operand
            elif plan.operation == "min":
                result = min(plan.operands)
            else:
                result = max(plan.operands)
        except (ArithmeticError, InvalidOperation):
            raise PrimitiveEvaluationError("calculation_failed") from None
    return _bounded_result(+result)


def evaluate_check(plan: CheckPlan) -> bool:
    """Evaluate a comparison without parsing expressions or executing code."""
    if plan.operation == "equals":
        return plan.left == plan.right
    if plan.operation == "not_equals":
        return plan.left != plan.right
    if plan.operation == "less_than":
        return plan.left < plan.right
    if plan.operation == "less_or_equal":
        return plan.left <= plan.right
    if plan.operation == "greater_than":
        return plan.left > plan.right
    if plan.operation == "greater_or_equal":
        return plan.left >= plan.right
    assert plan.tolerance is not None
    return abs(plan.left - plan.right) <= plan.tolerance


__all__ = [
    "BrightnessStep",
    "CalculatorPlan",
    "CheckPlan",
    "ContrastStep",
    "CropStep",
    "GrayscaleStep",
    "ImageTransformPlan",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_STEPS",
    "PrimitiveEvaluationError",
    "RecipeValidationError",
    "ResizeStep",
    "RotateStep",
    "evaluate_calculator",
    "evaluate_check",
    "parse_calculator",
    "parse_check",
    "parse_image_transform",
]
