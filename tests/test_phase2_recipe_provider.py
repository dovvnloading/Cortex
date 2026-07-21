"""Qualification tests for the disabled fixed-function image provider."""

from __future__ import annotations

from io import BytesIO
from threading import Event

import pytest
from PIL import Image

from cortex_backend.execution.lifecycle import RuntimeHealth
import cortex_backend.execution.recipe_provider as provider_module
from cortex_backend.execution.recipe_provider import (
    MAX_DECODED_BYTES,
    MAX_DIMENSION,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_PIXELS,
    RecipeImageProvider,
    RecipeProviderError,
    RecipeProviderLimits,
)
from cortex_backend.execution.recipes import parse_image_transform


def _plan(*steps: dict, output_format: str = "png"):
    return parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": "artifact-1",
            "steps": list(steps),
            "output_format": output_format,
        }
    )


def _image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (4, 3),
    mode: str = "RGBA",
    exif: bytes | None = None,
) -> bytes:
    image = Image.new(mode, size, (120, 80, 40, 255) if "A" in mode else 120)
    try:
        with BytesIO() as stream:
            kwargs = {"format": image_format}
            if exif is not None:
                kwargs["exif"] = exif
            image.save(stream, **kwargs)
            return stream.getvalue()
    finally:
        image.close()


def _started_provider(limits: RecipeProviderLimits | None = None) -> RecipeImageProvider:
    provider = RecipeImageProvider(limits)
    health = provider.start(RuntimeHealth.ready("test sandbox attestation"))
    assert health.available
    return provider


def test_provider_is_disabled_until_external_sandbox_health_passes():
    provider = RecipeImageProvider()
    assert provider.health_snapshot.code == "recipe_provider_disabled"
    assert provider.health().code == "sandbox_unverified"
    blocked = provider.start(RuntimeHealth.blocked("sandbox_unavailable", "sandbox is unavailable"))
    assert not blocked.available
    assert not provider.enabled
    with pytest.raises(RecipeProviderError) as error:
        provider.transform(_plan({"op": "grayscale"}), _image_bytes())
    assert error.value.code == "provider_disabled"


def test_provider_transforms_and_reencodes_without_metadata():
    provider = _started_provider()
    source = _image_bytes("JPEG", mode="RGB", exif=b"Exif\x00\x00untrusted metadata")
    plan = _plan(
        {"op": "grayscale"},
        {"op": "contrast", "factor": "1.2"},
        {"op": "resize", "width": 2, "height": 2},
        output_format="png",
    )

    result = provider.transform(plan, source)

    assert result.mime_type == "image/png"
    assert result.format == "PNG"
    assert (result.width, result.height) == (2, 2)
    assert result.sha256
    with Image.open(BytesIO(result.content)) as output:
        assert output.format == "PNG"
        assert output.mode == "L"
        assert "exif" not in output.info
        assert "text" not in output.info
    assert provider.transform(plan, source).sha256 == result.sha256


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"not an image", "unsupported_format"),
        (b"<!doctype html><script>alert(1)</script>", "invalid_input"),
        (b"MZ\x90\x00not an image", "invalid_input"),
    ],
)
def test_provider_rejects_non_allowlisted_or_active_input(content: bytes, expected: str):
    provider = _started_provider()
    with pytest.raises(RecipeProviderError) as error:
        provider.transform(_plan({"op": "grayscale"}), content)
    assert error.value.code == expected


def test_provider_rejects_malformed_and_multipage_input():
    provider = _started_provider()
    with pytest.raises(RecipeProviderError) as malformed:
        provider.transform(_plan({"op": "grayscale"}), b"\xff\xd8\xffmalformed jpeg")
    assert malformed.value.code == "decode_failed"

    first = Image.new("RGB", (2, 2), (1, 2, 3))
    second = Image.new("RGB", (2, 2), (4, 5, 6))
    try:
        with BytesIO() as stream:
            first.save(
                stream,
                format="WEBP",
                save_all=True,
                append_images=[second],
                duration=100,
                loop=0,
                lossless=True,
            )
            animated = stream.getvalue()
    finally:
        first.close()
        second.close()
    with pytest.raises(RecipeProviderError) as frames:
        provider.transform(_plan({"op": "grayscale"}), animated)
    assert frames.value.code == "unsupported_frames"


def test_provider_enforces_pixel_decoded_memory_and_output_limits():
    image = _image_bytes("PNG", size=(8, 8))
    provider = _started_provider(RecipeProviderLimits(max_pixels=16, max_decoded_bytes=64))
    with pytest.raises(RecipeProviderError) as pixels:
        provider.transform(_plan({"op": "grayscale"}), image)
    assert pixels.value.code == "resource_limit"

    tiny_output_limit = _started_provider(RecipeProviderLimits(max_output_bytes=8))
    with pytest.raises(RecipeProviderError) as output:
        tiny_output_limit.transform(_plan({"op": "grayscale"}), _image_bytes())
    assert output.value.code == "output_too_large"


def test_provider_rejects_out_of_bounds_crop_and_honors_cancellation():
    provider = _started_provider()
    with pytest.raises(RecipeProviderError) as crop:
        provider.transform(
            _plan({"op": "crop", "x": 3, "y": 0, "width": 2, "height": 2}),
            _image_bytes(),
        )
    assert crop.value.code == "invalid_plan"

    cancelled = Event()
    cancelled.set()
    with pytest.raises(RecipeProviderError) as cancel_error:
        provider.transform(_plan({"op": "grayscale"}), _image_bytes(), cancel_check=cancelled.is_set)
    assert cancel_error.value.code == "cancelled"

    with pytest.raises(RecipeProviderError) as callback_error:
        provider.transform(_plan({"op": "grayscale"}), _image_bytes(), cancel_check=lambda: 1 / 0)
    assert callback_error.value.code == "cancellation_check_failed"


def test_provider_stop_is_monotonic_and_redacts_runtime_failures():
    provider = _started_provider()
    stopped = provider.stop()
    assert not stopped.available
    assert stopped.code == "recipe_provider_stopped"
    assert not provider.enabled


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_input_bytes", MAX_INPUT_BYTES + 1),
        ("max_output_bytes", MAX_OUTPUT_BYTES + 1),
        ("max_pixels", MAX_PIXELS + 1),
        ("max_dimension", MAX_DIMENSION + 1),
        ("max_decoded_bytes", MAX_DECODED_BYTES + 1),
        ("max_steps", 9),
    ],
)
def test_provider_limits_cannot_raise_the_qualification_ceiling(field: str, value: int):
    with pytest.raises(ValueError):
        RecipeProviderLimits(**{field: value})


def test_provider_health_blocks_missing_dependency_or_codec(monkeypatch):
    provider = RecipeImageProvider()
    monkeypatch.setattr(provider_module, "Image", None)
    missing = provider.start(RuntimeHealth.ready("test sandbox attestation"))
    assert not missing.available
    assert missing.code == "recipe_dependency_missing"
    assert not provider.enabled

    monkeypatch.undo()
    monkeypatch.setattr(provider_module.Image, "registered_extensions", lambda: {".png": "PNG"})
    unavailable = provider.start(RuntimeHealth.ready("test sandbox attestation"))
    assert not unavailable.available
    assert unavailable.code == "recipe_codec_unavailable"
    assert not provider.enabled


def test_provider_rejects_input_before_decoder_when_encoded_limit_is_exceeded():
    provider = _started_provider(RecipeProviderLimits(max_input_bytes=8))
    with pytest.raises(RecipeProviderError) as error:
        provider.transform(_plan({"op": "grayscale"}), _image_bytes())
    assert error.value.code == "input_too_large"
