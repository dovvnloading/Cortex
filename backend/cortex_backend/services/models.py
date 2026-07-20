"""Headless Ollama model availability and exact-tag checks."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Protocol

from cortex_backend.core.generation import ConnectionResult


class ModelGateway(Protocol):
    """Minimal Ollama client boundary needed by model readiness checks."""

    def list(self) -> Any:
        """Return the installed model listing."""

    def pull(self, model: str) -> Any:
        """Pull one exact model tag."""


class ModelService:
    """Check required model tags without depending on a UI or transport."""

    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    def list_installed(self) -> tuple[str, ...]:
        """Return the exact installed model tags in stable order."""
        try:
            return tuple(sorted(self.extract_model_tags(self._gateway.list())))
        except Exception as exc:
            logging.error("Ollama model listing failed (%s).", type(exc).__name__)
            return ()

    def check(
        self,
        *,
        required_models: Iterable[str],
        optional_models: Iterable[str] = (),
    ) -> ConnectionResult:
        """Verify required tags and report optional tags without pulling them."""
        required = tuple(dict.fromkeys(model for model in required_models if model))
        optional = tuple(dict.fromkeys(model for model in optional_models if model))
        try:
            local_models = self.extract_model_tags(self._gateway.list())
            missing_models = tuple(
                model for model in required if model not in local_models
            )
            for model in missing_models:
                logging.info(
                    "Required model tag '%s' is not installed; pulling exact tag.",
                    model,
                )
                self._gateway.pull(model)

            if missing_models:
                local_models = self.extract_model_tags(self._gateway.list())
            still_missing = tuple(
                model for model in required if model not in local_models
            )
            if still_missing:
                return ConnectionResult.failed(
                    "Required Ollama models are unavailable. Please install them and retry.",
                    details="missing_required_models",
                    missing_models=still_missing,
                )

            optional_missing = tuple(
                model for model in optional if model not in local_models
            )
            message = "Connected to Ollama and verified the required model tags."
            if optional_missing:
                message += " Optional models unavailable: " + ", ".join(
                    optional_missing
                )
            logging.info(message)
            return ConnectionResult.connected(
                message,
                missing_models=missing_models,
                optional_missing_models=optional_missing,
            )
        except Exception as exc:
            logging.error("Ollama model check failed (%s).", type(exc).__name__)
            return ConnectionResult.failed(
                "Could not connect to Ollama. Please start Ollama and retry.",
                details=type(exc).__name__,
            )

    @staticmethod
    def extract_model_tags(response: Any) -> set[str]:
        """Extract exact tags from current and legacy Ollama response shapes."""
        if isinstance(response, dict):
            entries = response.get("models", [])
        else:
            entries = getattr(response, "models", [])

        tags: set[str] = set()
        for entry in entries or []:
            if isinstance(entry, dict):
                tag = entry.get("name") or entry.get("model")
            else:
                tag = getattr(entry, "model", None) or getattr(entry, "name", None)
            if tag:
                tags.add(str(tag).strip())
        return tags
