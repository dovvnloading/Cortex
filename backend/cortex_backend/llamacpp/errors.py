"""Error taxonomy for the local llama.cpp runtime.

Mirrors the duck-typed shape ``services/llm.py``'s
``_generation_failure_message`` already reads off ``ollama``'s exceptions
(``status_code``, ``error``), plus a ``backend`` marker so that function can
tell a llama.cpp failure from an Ollama failure without importing this
module.  Real ``ollama`` exceptions never carry a ``backend`` attribute, so
existing Ollama error copy is unaffected.
"""

from __future__ import annotations


class LlamaCppError(RuntimeError):
    """Raised for any llama.cpp runtime failure (binary fetch, process, HTTP)."""

    backend = "llamacpp"

    def __init__(self, error: str, *, status_code: int | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.status_code = status_code


class BinaryVerificationError(LlamaCppError):
    """Raised when a downloaded/cached llama-server binary fails verification."""


class ServerLaunchError(LlamaCppError):
    """Raised when the llama-server process exits before becoming healthy.

    This is the specific signature :class:`~cortex_backend.llamacpp.server_manager.LlamaServerManager`
    treats as "this GPU backend can't run here" (as opposed to a slow-but-alive load).
    """


class ServerStartTimeoutError(LlamaCppError):
    """Raised when the llama-server process never becomes healthy in time.

    Deliberately distinct from :class:`ServerLaunchError`: a process that is
    still alive but slow to finish loading a large model must not be treated
    as a GPU-backend failure and must not trigger a CPU fallback.
    """
