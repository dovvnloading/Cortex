"""Headless Ollama model availability and exact-tag checks."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from threading import Event
from typing import Any, Protocol

from cortex_backend.core.generation import ConnectionResult


class ModelGateway(Protocol):
    """Minimal Ollama client boundary needed by model readiness checks."""

    def list(self) -> Any:
        """Return the installed model listing."""

    def pull(self, model: str, *, stream: bool = False) -> Any:
        """Pull one exact model tag."""

    def show(self, model: str) -> Any:
        """Return Ollama's model details, including capability metadata."""


@dataclass(frozen=True, slots=True)
class InstalledModel:
    """Safe installed-model metadata returned by Ollama."""

    name: str
    size: int | None = None
    modified_at: str | None = None
    capabilities: tuple[str, ...] = ()
    supports_vision: bool | None = None
    parameter_size: str | None = None
    quantization_level: str | None = None
    family: str | None = None
    context_length: int | None = None


@dataclass(frozen=True, slots=True)
class ModelShowDetails:
    """Everything this app reads off one Ollama ``/api/show`` response."""

    capabilities: tuple[str, ...]
    parameter_size: str | None = None
    quantization_level: str | None = None
    family: str | None = None
    context_length: int | None = None


@dataclass(frozen=True, slots=True)
class ModelPullProgress:
    """Normalized model-pull update suitable for an API job event."""

    model: str
    status: str
    completed: int | None = None
    total: int | None = None
    digest: str | None = None

    @property
    def percent(self) -> int | None:
        if self.completed is None or not self.total:
            return None
        return min(100, max(0, round(self.completed / self.total * 100)))


class ModelService:
    """Check required model tags without depending on a UI or transport."""

    def __init__(self, gateway: ModelGateway):
        self._gateway = gateway

    def list_installed(self) -> tuple[str, ...]:
        """Return the exact installed model tags in stable order."""
        return tuple(item.name for item in self.list_installed_details())

    def list_installed_details(self) -> tuple[InstalledModel, ...]:
        """Return normalized installed model metadata without logging content."""
        models, _ = self.inventory()
        return models

    def inventory(self) -> tuple[tuple[InstalledModel, ...], ConnectionResult]:
        """Read one authoritative local inventory and its connection result."""
        try:
            raw_models = tuple(
                sorted(
                    self.extract_model_details(self._gateway.list()),
                    key=lambda item: item.name,
                )
            )
            models = tuple(self._with_show_details(item) for item in raw_models)
        except Exception as exc:
            logging.error("Ollama model metadata listing failed (%s).", type(exc).__name__)
            return (), ConnectionResult.failed(
                "Could not connect to Ollama. Please start Ollama and retry.",
                details=type(exc).__name__,
            )
        return models, ConnectionResult.connected("Connected to Ollama.")

    def probe(self) -> ConnectionResult:
        """Check Ollama reachability without pulling or changing model state."""
        _, connection = self.inventory()
        return connection

    def check(
        self,
        *,
        required_models: Iterable[str],
        optional_models: Iterable[str] = (),
        progress_callback: Callable[[ModelPullProgress], None] | None = None,
        cancellation_event: Event | None = None,
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
                if cancellation_event is not None and cancellation_event.is_set():
                    return ConnectionResult.failed("Model check cancelled.", details="cancelled")
                logging.info(
                    "Required model tag '%s' is not installed; pulling exact tag.",
                    model,
                )
                self.pull_model(
                    model,
                    progress_callback=progress_callback,
                    cancellation_event=cancellation_event,
                    verify=False,
                )

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
            message = "Connected to Ollama and scanned the local model inventory."
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

    def pull_model(
        self,
        model: str,
        *,
        progress_callback: Callable[[ModelPullProgress], None] | None = None,
        cancellation_event: Event | None = None,
        verify: bool = True,
    ) -> bool:
        """Pull one exact tag and normalize streamed Ollama progress."""
        exact_model = model.strip()
        if not exact_model:
            raise ValueError("Model tag must not be empty.")
        try:
            try:
                stream = self._gateway.pull(exact_model, stream=True)
            except TypeError:
                # Keep compatibility with the Stage 1/2 test and legacy gateway
                # shape while real Ollama uses the streaming keyword.
                stream = self._gateway.pull(exact_model)
            updates = self._iter_updates(stream)
            for raw_update in updates:
                if cancellation_event is not None and cancellation_event.is_set():
                    return False
                update = self._normalize_progress(exact_model, raw_update)
                if progress_callback is not None:
                    progress_callback(update)
            if cancellation_event is not None and cancellation_event.is_set():
                return False
            return (
                exact_model in self.extract_model_tags(self._gateway.list())
                if verify
                else True
            )
        except Exception as exc:
            logging.error("Ollama model pull failed (%s).", type(exc).__name__)
            raise

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

    @staticmethod
    def extract_model_details(response: Any) -> tuple[InstalledModel, ...]:
        if isinstance(response, dict):
            entries = response.get("models", [])
        else:
            entries = getattr(response, "models", [])
        details: dict[str, InstalledModel] = {}
        for entry in entries or []:
            if isinstance(entry, Mapping):
                name = entry.get("name") or entry.get("model")
                size = entry.get("size")
                modified_at = entry.get("modified_at")
            else:
                name = getattr(entry, "model", None) or getattr(entry, "name", None)
                size = getattr(entry, "size", None)
                modified_at = getattr(entry, "modified_at", None)
            if name:
                normalized_name = str(name).strip()
                details.setdefault(
                    normalized_name,
                    InstalledModel(
                        name=normalized_name,
                        size=int(size) if isinstance(size, (int, float)) else None,
                        modified_at=str(modified_at) if modified_at else None,
                    ),
                )
        return tuple(details.values())

    def capabilities(self, model: str) -> tuple[str, ...] | None:
        """Read the official Ollama ``/api/show`` capability list.

        Older test/compatibility gateways may not expose ``show``; ``None``
        means capability detection is unavailable, while an empty tuple is a
        successful response that explicitly advertises no capabilities.
        """
        details = self.show_details(model)
        return None if details is None else details.capabilities

    def model_supports_vision(self, model: str) -> bool | None:
        capabilities = self.capabilities(model)
        return None if capabilities is None else "vision" in capabilities

    def show_details(self, model: str) -> ModelShowDetails | None:
        """Read the full ``/api/show`` response this app cares about.

        A single round trip already happens today (previously only to read
        ``capabilities``); this reads the ``details``/``model_info`` fields
        Ollama already returns in that same response instead of a second call.
        """
        show = getattr(self._gateway, "show", None)
        if not callable(show):
            return None
        try:
            response = show(model)
        except Exception as exc:
            logging.warning("Ollama capability probe failed (%s).", type(exc).__name__)
            return None
        get = response.get if isinstance(response, Mapping) else lambda key, default=None: getattr(response, key, default)
        values = get("capabilities", ())
        capabilities = (
            ()
            if not isinstance(values, (list, tuple, set))
            else tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        )
        details = get("details", {}) or {}
        model_info = get("model_info", {}) or {}
        details_get = details.get if isinstance(details, Mapping) else lambda key, default=None: getattr(details, key, default)
        family = details_get("family")
        family = str(family).strip() or None if family else None
        context_length = None
        if family and isinstance(model_info, Mapping):
            raw_context_length = model_info.get(f"{family}.context_length")
            if isinstance(raw_context_length, (int, float)):
                context_length = int(raw_context_length)
        parameter_size = details_get("parameter_size")
        quantization_level = details_get("quantization_level")
        return ModelShowDetails(
            capabilities=capabilities,
            parameter_size=str(parameter_size).strip() or None if parameter_size else None,
            quantization_level=str(quantization_level).strip() or None if quantization_level else None,
            family=family,
            context_length=context_length,
        )

    def _with_show_details(self, item: InstalledModel) -> InstalledModel:
        details = self.show_details(item.name)
        if details is None:
            return item
        return replace(
            item,
            capabilities=details.capabilities,
            supports_vision="vision" in details.capabilities,
            parameter_size=details.parameter_size,
            quantization_level=details.quantization_level,
            family=details.family,
            context_length=details.context_length,
        )

    @staticmethod
    def _iter_updates(stream: Any) -> Iterable[Any]:
        if stream is None:
            return ()
        if isinstance(stream, Mapping):
            return (stream,)
        if isinstance(stream, (str, bytes)):
            return ({"status": stream.decode() if isinstance(stream, bytes) else stream},)
        try:
            return iter(stream)
        except TypeError:
            return (stream,)

    @staticmethod
    def _normalize_progress(model: str, raw_update: Any) -> ModelPullProgress:
        if isinstance(raw_update, Mapping):
            status = raw_update.get("status", "pulling")
            completed = raw_update.get("completed")
            total = raw_update.get("total")
            digest = raw_update.get("digest")
        else:
            status = getattr(raw_update, "status", "pulling")
            completed = getattr(raw_update, "completed", None)
            total = getattr(raw_update, "total", None)
            digest = getattr(raw_update, "digest", None)
        return ModelPullProgress(
            model=model,
            status=str(status),
            completed=int(completed) if isinstance(completed, (int, float)) else None,
            total=int(total) if isinstance(total, (int, float)) else None,
            digest=str(digest) if digest else None,
        )
