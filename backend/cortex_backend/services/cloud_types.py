"""Shared, non-secret configuration metadata and user-facing cloud errors."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from .attachments import image_type


class CloudProviderError(RuntimeError):
    """Safe to display. Never pass upstream error bodies, tokens or request URLs."""

    is_user_guidance = True
    guidance_code = "cloud_provider_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.error = message


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    key_env: str
    base_url: str = ""
    url_env: str = ""
    models_env: str = ""
    protocol: str = "openai"
    catalog: bool = True
    setup: str = ""

    def __post_init__(self) -> None:
        if not self.models_env:
            name = self.id.replace("-", "_").upper()
            object.__setattr__(self, "models_env", f"CORTEX_{name}_MODELS")
        if not self.setup:
            required = [name for name in (self.key_env, self.url_env) if name]
            if not self.catalog:
                required.append(self.models_env)
            object.__setattr__(self, "setup", f"Set {' and '.join(required)} before starting Cortex.")


def image_mime_type(encoded: str) -> str:
    """Identify an already validated Cortex image from its base64 prefix."""
    try:
        detected = image_type(base64.b64decode(encoded[:16], validate=True))
    except (ValueError, TypeError):
        detected = None
    if not detected:
        raise CloudProviderError("The selected provider cannot read this image format.")
    return detected[0]
