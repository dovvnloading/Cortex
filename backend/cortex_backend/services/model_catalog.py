"""Merges the Ollama-facing ``ModelService`` with a local GGUF folder scan
behind one ``ModelCatalog`` surface, so ``api/routes.py`` needs no changes to
support either -- or both -- backends.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import Event
from typing import Protocol

from cortex_backend.core.generation import ConnectionResult

from .chat_client import GGUF_PREFIX
from .models import InstalledModel, ModelPullProgress, ModelService


class GGUFModelSource(Protocol):
    """The one method this catalog needs from a GGUF folder scan (see
    ``cortex_backend.llamacpp.model_directory.GGUFModelDirectory``). Kept as
    a Protocol so ``services/`` doesn't need a hard import dependency on the
    ``llamacpp`` feature package, matching the existing ``ModelGateway``
    boundary style in ``services/models.py``.
    """

    def list_installed_details(self) -> tuple[InstalledModel, ...]:
        ...


class CombinedModelCatalog:
    """Satisfies :class:`~cortex_backend.services.models.ModelCatalog` by
    merging Ollama (unchanged) with a GGUF directory scan.

    Every Ollama-facing method is pure delegation -- this class adds nothing
    to the Ollama path, so existing Ollama behavior is provably unaffected.
    """

    def __init__(self, ollama: ModelService, gguf: GGUFModelSource) -> None:
        self._ollama = ollama
        self._gguf = gguf

    def inventory(self) -> tuple[tuple[InstalledModel, ...], ConnectionResult]:
        ollama_models, connection = self._ollama.inventory()
        try:
            gguf_models = self._gguf.list_installed_details()
        except Exception:
            gguf_models = ()
        return ollama_models + gguf_models, connection

    def list_installed(self) -> tuple[str, ...]:
        return tuple(model.name for model in self.inventory()[0])

    def pull_model(
        self,
        model: str,
        *,
        progress_callback: Callable[[ModelPullProgress], None] | None = None,
        cancellation_event: Event | None = None,
        verify: bool = True,
    ) -> bool:
        # GGUF models have no registry "pull by tag" concept -- downloading
        # one is a separate flow (llamacpp/download.py + its own route/job
        # kind), reached from a different UI affordance than "pull a model".
        return self._ollama.pull_model(
            model,
            progress_callback=progress_callback,
            cancellation_event=cancellation_event,
            verify=verify,
        )

    def check(
        self,
        *,
        required_models: Iterable[str],
        optional_models: Iterable[str] = (),
        progress_callback: Callable[[ModelPullProgress], None] | None = None,
        cancellation_event: Event | None = None,
    ) -> ConnectionResult:
        # Required/optional models are always Ollama tags today (the
        # translation default); GGUF availability is reported through the
        # merged inventory instead.
        return self._ollama.check(
            required_models=required_models,
            optional_models=optional_models,
            progress_callback=progress_callback,
            cancellation_event=cancellation_event,
        )

    def model_supports_vision(self, model: str) -> bool | None:
        if model.startswith(GGUF_PREFIX):
            # MVP: no mmproj/vision support for GGUF models -- unknown, not
            # false, so callers can still distinguish "no info" from a
            # confirmed non-vision Ollama model if that distinction matters
            # later; today both disable image attachments the same way.
            return None
        return self._ollama.model_supports_vision(model)
