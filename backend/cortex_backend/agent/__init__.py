"""Durable metadata for explicitly started coding-agent runs.

Model/tool-call wire protocol. No execution or dispatch lives in this package.
"""

from .protocol import (
    ModelTurn,
    ProtocolError,
    RuntimeCapabilities,
    ToolCall,
    ToolSpec,
    call_model,
    runtime_capabilities,
)

__all__ = [
    "ModelTurn",
    "ProtocolError",
    "RuntimeCapabilities",
    "ToolCall",
    "ToolSpec",
    "call_model",
    "runtime_capabilities",
]
