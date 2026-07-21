"""Qualification-only fixed-function image recipe provider.

This module is a bounded transform core, not an execution route.  It accepts
validated ``ImageTransformPlan`` objects and immutable bytes, never paths or
model source, and returns a new encoded image only after decoding and
re-validating the result.  The provider starts only after an external sandbox
health result is available; no application lifecycle imports this module yet.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import math
import re
from threading import RLock
from typing import Any, Iterator
import warnings

from .artifact_boundary import ArtifactBoundaryError, sniff_artifact_mime
from .lifecycle import RuntimeHealth
from .recipes import ImageTransformPlan

try:  # Keep the application importable when the optional qualification wheel is absent.
    import PIL
    from PIL import Image, ImageEnhance, ImageFile
    from PIL import UnidentifiedImageError
except ImportError:  # pragma: no cover - exercised by packaging/health probes.
    PIL = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageEnhance = None  # type: ignore[assignment]
    ImageFile = None  # type: ignore[assignment]
    UnidentifiedImageError = Exception  # type: ignore[assignment,misc]


MAX_INPUT_BYTES = 100 * 1024 * 1024
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
MAX_PIXELS = 64 * 1024 * 1024
MAX_DIMENSION = 16_384
MAX_DECODED_BYTES = 256 * 1024 * 1024
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORMAT_BY_PLAN = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
_MIME_BY_FORMAT = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
_PIL_LOCK = RLock()


class RecipeProviderError(ValueError):
    """Stable provider failure without paths, payloads, or decoder details."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid recipe provider error code")
        self.code = code
        super().__init__("The image recipe could not be completed safely.")


@dataclass(frozen=True, slots=True)
class RecipeProviderLimits:
    """Per-attempt byte, pixel, dimension, and decoded-memory ceilings."""

    max_input_bytes: int = MAX_INPUT_BYTES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_pixels: int = MAX_PIXELS
    max_dimension: int = MAX_DIMENSION
    max_decoded_bytes: int = MAX_DECODED_BYTES
    max_steps: int = 8

    def __post_init__(self) -> None:
        values = (
            self.max_input_bytes,
            self.max_output_bytes,
            self.max_pixels,
            self.max_dimension,
            self.max_decoded_bytes,
            self.max_steps,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("recipe provider limits must be positive integers")
        if self.max_dimension > MAX_DIMENSION:
            raise ValueError("recipe provider dimension ceiling is too high")
        if self.max_steps > 8:
            raise ValueError("recipe provider step ceiling is too high")
        if (
            self.max_input_bytes > MAX_INPUT_BYTES
            or self.max_output_bytes > MAX_OUTPUT_BYTES
            or self.max_pixels > MAX_PIXELS
            or self.max_decoded_bytes > MAX_DECODED_BYTES
        ):
            raise ValueError("recipe provider resource ceiling is too high")


@dataclass(frozen=True, slots=True)
class RecipeProviderResult:
    """Validated output bytes and safe metadata returned to a future coordinator."""

    content: bytes
    mime_type: str
    width: int
    height: int
    format: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("provider result content must be non-empty bytes")
        if self.mime_type not in set(_MIME_BY_FORMAT.values()):
            raise ValueError("provider result MIME is invalid")
        if self.format not in set(_MIME_BY_FORMAT):
            raise ValueError("provider result format is invalid")
        if _MIME_BY_FORMAT[self.format] != self.mime_type:
            raise ValueError("provider result format/MIME mismatch")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("provider result dimensions are invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("provider result digest is invalid")


CancellationCheck = Callable[[], bool]


def _check_cancel(cancel_check: CancellationCheck | None) -> None:
    if cancel_check is None:
        return
    try:
        cancelled = bool(cancel_check())
    except Exception:
        raise RecipeProviderError("cancellation_check_failed") from None
    if cancelled:
        raise RecipeProviderError("cancelled")


def _pillow_health() -> tuple[bool, str, str]:
    if Image is None or ImageEnhance is None or ImageFile is None or PIL is None:
        return False, "recipe_dependency_missing", "The image recipe dependency is unavailable."
    try:
        Image.init()
        supported = set(Image.registered_extensions().values())
    except Exception:
        return False, "recipe_codec_unavailable", "The required image codecs are unavailable."
    if not {"PNG", "JPEG", "WEBP"}.issubset(supported):
        return False, "recipe_codec_unavailable", "The required image codecs are unavailable."
    return True, "ready", f"Fixed-function image provider is ready (Pillow {PIL.__version__})."


@contextmanager
def _decoder_limits(max_pixels: int) -> Iterator[None]:
    """Serialize Pillow's process-global bomb/truncation controls and restore them."""

    if Image is None or ImageFile is None:
        raise RecipeProviderError("recipe_dependency_missing")
    with _PIL_LOCK:
        previous_pixels = Image.MAX_IMAGE_PIXELS
        previous_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
        Image.MAX_IMAGE_PIXELS = max_pixels
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                yield
            finally:
                Image.MAX_IMAGE_PIXELS = previous_pixels
                ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated


def _format_for_mime(mime_type: str) -> str:
    for image_format, image_mime in _MIME_BY_FORMAT.items():
        if image_mime == mime_type:
            return image_format
    raise RecipeProviderError("unsupported_format")


def _image_dimensions(image: Any, limits: RecipeProviderLimits) -> tuple[int, int]:
    try:
        width, height = int(image.width), int(image.height)
        frames = int(getattr(image, "n_frames", 1))
    except Exception:
        raise RecipeProviderError("decode_failed") from None
    if width <= 0 or height <= 0:
        raise RecipeProviderError("decode_failed")
    if frames != 1:
        raise RecipeProviderError("unsupported_frames")
    pixels = width * height
    if width > limits.max_dimension or height > limits.max_dimension or pixels > limits.max_pixels:
        raise RecipeProviderError("resource_limit")
    try:
        channels = {"1": 1, "L": 1, "P": 1, "LA": 2, "RGB": 3, "RGBA": 4}.get(image.mode, 4)
    except Exception:
        channels = 4
    if pixels * channels > limits.max_decoded_bytes:
        raise RecipeProviderError("resource_limit")
    return width, height


def _decode_verified(
    content: bytes,
    image_format: str,
    limits: RecipeProviderLimits,
    cancel_check: CancellationCheck | None,
) -> Any:
    if not isinstance(content, bytes) or not content:
        raise RecipeProviderError("invalid_input")
    if len(content) > limits.max_input_bytes:
        raise RecipeProviderError("input_too_large")
    try:
        detected = sniff_artifact_mime(content)
    except ArtifactBoundaryError:
        raise RecipeProviderError("invalid_input") from None
    if detected != _MIME_BY_FORMAT[image_format]:
        raise RecipeProviderError("input_format_mismatch")
    _check_cancel(cancel_check)
    try:
        with BytesIO(content) as stream:
            with Image.open(stream, formats=(image_format,)) as candidate:
                _image_dimensions(candidate, limits)
                candidate.verify()
        _check_cancel(cancel_check)
        with BytesIO(content) as stream:
            with Image.open(stream, formats=(image_format,)) as decoded:
                _image_dimensions(decoded, limits)
                decoded.load()
                _image_dimensions(decoded, limits)
                normalized = decoded.convert("RGBA")
                normalized.info.clear()
                _image_dimensions(normalized, limits)
                return normalized
    except RecipeProviderError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise RecipeProviderError("resource_limit") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, MemoryError):
        raise RecipeProviderError("decode_failed") from None


def _replace_image(current: Any, replacement: Any) -> Any:
    current.close()
    return replacement


def _apply_plan(
    image: Any,
    plan: ImageTransformPlan,
    limits: RecipeProviderLimits,
    cancel_check: CancellationCheck | None,
) -> Any:
    if len(plan.steps) > limits.max_steps:
        raise RecipeProviderError("resource_limit")
    current = image
    try:
        for step in plan.steps:
            _check_cancel(cancel_check)
            if step.op == "grayscale":
                current = _replace_image(current, current.convert("L"))
            elif step.op == "contrast":
                factor = float(step.factor)
                if not math.isfinite(factor):
                    raise RecipeProviderError("invalid_plan")
                current = _replace_image(current, ImageEnhance.Contrast(current).enhance(factor))
            elif step.op == "brightness":
                factor = float(step.factor)
                if not math.isfinite(factor):
                    raise RecipeProviderError("invalid_plan")
                current = _replace_image(current, ImageEnhance.Brightness(current).enhance(factor))
            elif step.op == "crop":
                right = step.x + step.width
                bottom = step.y + step.height
                if right > current.width or bottom > current.height:
                    raise RecipeProviderError("invalid_plan")
                current = _replace_image(current, current.crop((step.x, step.y, right, bottom)))
            elif step.op == "resize":
                current = _replace_image(
                    current,
                    current.resize((step.width, step.height), resample=Image.Resampling.LANCZOS),
                )
            elif step.op == "rotate":
                current = _replace_image(
                    current,
                    current.rotate(step.degrees, resample=Image.Resampling.BICUBIC, expand=True),
                )
            else:  # Defensive if a future plan type bypasses the parser.
                raise RecipeProviderError("invalid_plan")
            _image_dimensions(current, limits)
        _check_cancel(cancel_check)
        return current
    except Exception:
        if current is not image:
            current.close()
        raise


def _encode_verified(
    image: Any,
    output_format: str,
    limits: RecipeProviderLimits,
    cancel_check: CancellationCheck | None,
) -> tuple[bytes, int, int]:
    _check_cancel(cancel_check)
    width, height = _image_dimensions(image, limits)
    target = image
    converted = None
    if output_format == "JPEG" and image.mode not in {"L", "RGB"}:
        converted = image.convert("RGB")
        target = converted
    try:
        target.info.clear()
        with BytesIO() as stream:
            kwargs: dict[str, Any] = {}
            if output_format == "PNG":
                kwargs.update(compress_level=9, optimize=False, exif=b"", icc_profile=None)
            elif output_format == "JPEG":
                kwargs.update(
                    quality=95,
                    optimize=False,
                    progressive=False,
                    subsampling=0,
                    exif=b"",
                    icc_profile=None,
                )
            elif output_format == "WEBP":
                kwargs.update(lossless=True, method=6, exif=b"", icc_profile=None)
            target.save(stream, format=output_format, **kwargs)
            encoded = stream.getvalue()
    except (OSError, ValueError, MemoryError):
        raise RecipeProviderError("encode_failed") from None
    finally:
        if converted is not None:
            converted.close()
    if len(encoded) == 0 or len(encoded) > limits.max_output_bytes:
        raise RecipeProviderError("output_too_large")
    _check_cancel(cancel_check)
    try:
        if sniff_artifact_mime(encoded) != _MIME_BY_FORMAT[output_format]:
            raise RecipeProviderError("output_invalid")
        with BytesIO(encoded) as stream:
            with Image.open(stream, formats=(output_format,)) as candidate:
                out_width, out_height = _image_dimensions(candidate, limits)
                candidate.verify()
        with BytesIO(encoded) as stream:
            with Image.open(stream, formats=(output_format,)) as candidate:
                candidate.load()
                out_width, out_height = _image_dimensions(candidate, limits)
                if out_width != width or out_height != height:
                    raise RecipeProviderError("output_invalid")
                if any(key in candidate.info for key in ("exif", "icc_profile", "xmp", "comment", "text")):
                    raise RecipeProviderError("output_metadata_present")
    except RecipeProviderError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise RecipeProviderError("resource_limit") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, MemoryError):
        raise RecipeProviderError("output_invalid") from None
    return encoded, out_width, out_height


class RecipeImageProvider:
    """Fixed-function image provider that remains disabled until sandbox health passes."""

    def __init__(self, limits: RecipeProviderLimits | None = None) -> None:
        self.limits = limits or RecipeProviderLimits()
        self._enabled = False
        self._health = RuntimeHealth.blocked(
            code="recipe_provider_disabled",
            message="The fixed-function image provider is disabled in this build.",
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def health_snapshot(self) -> RuntimeHealth:
        return self._health

    def health(self, sandbox_health: RuntimeHealth | None = None) -> RuntimeHealth:
        if sandbox_health is None:
            return RuntimeHealth.blocked(
                code="sandbox_unverified",
                message="The image provider sandbox has not been verified.",
            )
        if not sandbox_health.available:
            return RuntimeHealth.blocked(
                code="sandbox_unavailable",
                message="The image provider sandbox is unavailable.",
            )
        available, code, message = _pillow_health()
        return RuntimeHealth(available=available, code=code, message=message)

    def start(self, sandbox_health: RuntimeHealth) -> RuntimeHealth:
        """Enable only after the caller supplies a passing external sandbox probe."""

        health = self.health(sandbox_health)
        self._health = health
        self._enabled = health.available
        return health

    def stop(self) -> RuntimeHealth:
        self._enabled = False
        self._health = RuntimeHealth.blocked(
            code="recipe_provider_stopped",
            message="The fixed-function image provider is stopped.",
        )
        return self._health

    def transform(
        self,
        plan: ImageTransformPlan,
        content: bytes,
        *,
        cancel_check: CancellationCheck | None = None,
    ) -> RecipeProviderResult:
        """Transform one immutable image in memory; no paths or host capabilities are accepted."""

        if not self._enabled:
            raise RecipeProviderError("provider_disabled")
        if not isinstance(plan, ImageTransformPlan):
            raise RecipeProviderError("invalid_plan")
        try:
            image_format = _format_for_mime(sniff_artifact_mime(content))
        except ArtifactBoundaryError:
            raise RecipeProviderError("invalid_input") from None
        expected_format = _FORMAT_BY_PLAN[plan.output_format]
        with _decoder_limits(self.limits.max_pixels):
            image = _decode_verified(content, image_format, self.limits, cancel_check)
            try:
                image = _apply_plan(image, plan, self.limits, cancel_check)
                encoded, width, height = _encode_verified(
                    image,
                    expected_format,
                    self.limits,
                    cancel_check,
                )
            finally:
                image.close()
        return RecipeProviderResult(
            content=encoded,
            mime_type=_MIME_BY_FORMAT[expected_format],
            width=width,
            height=height,
            format=expected_format,
            sha256=sha256(encoded).hexdigest(),
        )


__all__ = [
    "CancellationCheck",
    "MAX_DECODED_BYTES",
    "MAX_DIMENSION",
    "MAX_INPUT_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_PIXELS",
    "RecipeImageProvider",
    "RecipeProviderError",
    "RecipeProviderLimits",
    "RecipeProviderResult",
]
