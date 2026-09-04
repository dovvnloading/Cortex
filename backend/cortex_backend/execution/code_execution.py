"""Approval-gated local Python execution.

This module intentionally keeps code execution separate from the automatic
scratch calculator.  The runner accepts only a bounded Python subset and
exposes host operations through the explicit ``cortex`` capability object.
The parent process owns approval, lifecycle, and cancellation; the worker
receives an immutable request and returns a compact JSON-safe result.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import signal
from threading import Event as ThreadEvent, Lock as ThreadLock, Thread
import time
from typing import Any
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    AbstractHTTPHandler,
    HTTPRedirectHandler,
    Request as UrlRequest,
    build_opener,
    ProxyHandler,
)


CODE_EXECUTION_PROFILE = "code.exec.v1"
CODE_EXECUTION_PAYLOAD_SCHEMA = "code.execution.v1"
CODE_EXECUTION_RESULT_SCHEMA = "code.result.v1"
MAX_CODE_SOURCE_BYTES = 64 * 1024
MAX_CODE_OUTPUT_BYTES = 256 * 1024
MAX_CODE_AST_NODES = 4096
MAX_CODE_AST_DEPTH = 32
MAX_CODE_LOOP_ITERATIONS = 10_000
MAX_CODE_TOTAL_ITERATIONS = 100_000
MAX_CODE_TIMEOUT_SECONDS = 10.0
MAX_CODE_MEMORY_BYTES = 256 * 1024 * 1024
MAX_CODE_VALUE_BYTES = 64 * 1024
MAX_CODE_PATH_CHARS = 4096
MAX_CODE_FILE_BYTES = 1 * 1024 * 1024
MAX_CODE_FILE_OPERATIONS = 32
MAX_CODE_PROCESS_OPERATIONS = 4
MAX_CODE_PROCESS_ARGUMENTS = 64
MAX_CODE_PROCESS_ARGUMENT_CHARS = 4096
MAX_CODE_PROCESS_ARGUMENT_BYTES = 16 * 1024
MAX_CODE_NETWORK_REQUESTS = 4
MAX_CODE_NETWORK_URL_CHARS = 2048
MAX_CODE_NETWORK_RESPONSE_BYTES = MAX_CODE_OUTPUT_BYTES
MAX_CODE_TRACE_EVENTS = 2_000_000
MAX_CODE_LIST_ENTRIES = 2048
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_ENCODINGS = frozenset({"utf-8", "utf8", "utf-16", "utf-16-le", "utf-16-be", "latin-1", "ascii"})
_DISALLOWED_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "dir",
        "help",
        "memoryview",
        "getattr",
        "setattr",
        "delattr",
        "object",
        "type",
    }
)


class CodeExecutionError(RuntimeError):
    """Stable internal error for validation and worker failures."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodeCapabilities:
    filesystem: bool = False
    process: bool = False
    network: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> CodeCapabilities:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise CodeExecutionError("capabilities_invalid")
        unknown = set(value) - {"filesystem", "process", "network"}
        if unknown:
            raise CodeExecutionError("capabilities_invalid")
        if any(
            key in value and type(value[key]) is not bool
            for key in ("filesystem", "process", "network")
        ):
            raise CodeExecutionError("capabilities_invalid")
        return cls(
            filesystem=value.get("filesystem", False),
            process=value.get("process", False),
            network=value.get("network", False),
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "filesystem": self.filesystem,
            "process": self.process,
            "network": self.network,
        }

    def restricted_to(self, required: CodeCapabilities) -> CodeCapabilities:
        """Keep only grants the validated source can actually use."""

        if not isinstance(required, CodeCapabilities):
            raise TypeError("required capabilities must be CodeCapabilities")
        return CodeCapabilities(
            filesystem=self.filesystem and required.filesystem,
            process=self.process and required.process,
            network=self.network and required.network,
        )


@dataclass(frozen=True, slots=True)
class CodeExecutionRequest:
    owner: str
    request_id: str
    source: str
    intent_summary: str
    capabilities: CodeCapabilities = CodeCapabilities()
    # Set only for a run the model proposed inside a chat, so the finished
    # result can be shown back to that same conversation. It is bookkeeping for
    # the chat layer: the worker never receives it, recovery never needs it,
    # and it grants no authority.
    thread_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner, str) or _SAFE_OWNER.fullmatch(self.owner) is None:
            raise ValueError("owner is invalid")
        if not isinstance(self.request_id, str) or _SAFE_REQUEST_ID.fullmatch(self.request_id) is None:
            raise ValueError("request_id is invalid")
        if not isinstance(self.source, str) or not self.source.strip():
            raise CodeExecutionError("source_empty")
        if len(self.source.encode("utf-8")) > MAX_CODE_SOURCE_BYTES:
            raise CodeExecutionError("source_too_large")
        if not isinstance(self.intent_summary, str) or not self.intent_summary.strip():
            raise CodeExecutionError("intent_invalid")
        if len(self.intent_summary) > 500:
            raise CodeExecutionError("intent_invalid")
        if any(ord(char) < 32 and char not in "\t" for char in self.intent_summary):
            raise CodeExecutionError("intent_invalid")
        if not isinstance(self.capabilities, CodeCapabilities):
            raise CodeExecutionError("capabilities_invalid")
        if self.thread_id is not None and (
            not isinstance(self.thread_id, str)
            or _SAFE_REQUEST_ID.fullmatch(self.thread_id) is None
        ):
            raise ValueError("thread_id is invalid")
        validate_code_source(self.source)
        if (
            self.capabilities.process
            or capabilities_required_by_source(self.source).process
        ):
            # A normal Windows subprocess inherits the user's ambient file and
            # network authority. A Job Object bounds resources and descendants,
            # but it is not an AppContainer or restricted-token sandbox. Keep
            # this capability fail-closed until the platform can enforce the
            # same authority boundary promised by the broker APIs.
            raise CodeExecutionError("process_capability_unavailable")

    @property
    def source_digest(self) -> str:
        import hashlib

        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    @property
    def approval_scope_digest(self) -> str:
        """Bind consent to source and the exact capability grant."""

        import hashlib

        scope = json.dumps(
            {
                "capabilities": self.capabilities.as_dict(),
                "source_digest": self.source_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(scope).hexdigest()

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": CODE_EXECUTION_PAYLOAD_SCHEMA,
            "language": "python",
            "source": self.source,
            "intent_summary": self.intent_summary.strip(),
            "source_digest": self.source_digest,
            "capabilities": self.capabilities.as_dict(),
        }
        # Omitted entirely when absent, so a run started through the public API
        # keeps exactly the payload it has always had.
        if self.thread_id:
            payload["thread_id"] = self.thread_id
        return payload


@dataclass(frozen=True, slots=True)
class CodeExecutionResult:
    stdout: str
    stderr: str
    value: Any = None
    truncated: bool = False
    duration_ms: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CODE_EXECUTION_RESULT_SCHEMA,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "value": _json_safe(self.value),
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


_ALLOWED_CALLS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
    "int", "len", "list", "map", "max", "min", "print", "range", "round",
    "set", "sorted", "str", "sum", "tuple", "zip",
}
_ALLOWED_CORTEX_CALLS = {
    ("cortex", "fs", "read_text"),
    ("cortex", "fs", "write_text"),
    ("cortex", "fs", "listdir"),
    ("cortex", "process", "run"),
    ("cortex", "net", "get"),
    ("cortex", "network", "get"),
}
_ALLOWED_CORTEX_NAMESPACES = {
    ("cortex", "fs"),
    ("cortex", "process"),
    ("cortex", "net"),
    ("cortex", "network"),
}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub, ast.Not)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot)


class _CodeValidator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes = 0
        self.depth = 0
        self.loop_product = 1

    def visit(self, node: ast.AST) -> Any:
        self.nodes += 1
        if self.nodes > MAX_CODE_AST_NODES:
            raise CodeExecutionError("source_too_complex")
        self.depth += 1
        if self.depth > MAX_CODE_AST_DEPTH:
            raise CodeExecutionError("source_too_complex")
        try:
            return super().visit(node)
        finally:
            self.depth -= 1

    def generic_visit(self, node: ast.AST) -> Any:
        allowed = (
            ast.Module, ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign,
            ast.Name, ast.Load, ast.Store, ast.Constant, ast.Attribute, ast.List, ast.Tuple,
            ast.Dict, ast.Set, ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp,
            ast.BoolOp, ast.Compare, ast.If, ast.For, ast.Break, ast.Continue,
            ast.Call, ast.keyword, ast.comprehension, ast.ListComp, ast.SetComp,
            ast.DictComp, ast.GeneratorExp, ast.IfExp, ast.JoinedStr,
            ast.FormattedValue, ast.Pass, ast.Assert,
            ast.operator, ast.unaryop, ast.boolop, ast.cmpop,
        )
        if not isinstance(node, allowed):
            raise CodeExecutionError("syntax_not_allowed")
        return super().generic_visit(node)

    def visit_Import(self, _node: ast.Import) -> Any:
        raise CodeExecutionError("imports_not_allowed")

    def visit_ImportFrom(self, _node: ast.ImportFrom) -> Any:
        raise CodeExecutionError("imports_not_allowed")

    def visit_While(self, _node: ast.While) -> Any:
        raise CodeExecutionError("unbounded_loop")

    def visit_FunctionDef(self, _node: ast.FunctionDef) -> Any:
        raise CodeExecutionError("function_definitions_not_allowed")

    def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> Any:
        raise CodeExecutionError("function_definitions_not_allowed")

    def visit_ClassDef(self, _node: ast.ClassDef) -> Any:
        raise CodeExecutionError("class_definitions_not_allowed")

    def visit_Lambda(self, _node: ast.Lambda) -> Any:
        raise CodeExecutionError("function_definitions_not_allowed")

    def visit_Try(self, _node: ast.Try) -> Any:
        raise CodeExecutionError("try_not_allowed")

    def visit_With(self, _node: ast.With) -> Any:
        raise CodeExecutionError("with_not_allowed")

    def visit_AsyncWith(self, _node: ast.AsyncWith) -> Any:
        raise CodeExecutionError("with_not_allowed")

    def visit_Raise(self, _node: ast.Raise) -> Any:
        raise CodeExecutionError("raise_not_allowed")

    def visit_Delete(self, _node: ast.Delete) -> Any:
        raise CodeExecutionError("delete_not_allowed")

    def visit_For(self, node: ast.For) -> Any:
        if not isinstance(node.target, (ast.Name, ast.Tuple, ast.List)):
            raise CodeExecutionError("loop_target_not_allowed")
        if not isinstance(node.iter, ast.Call) or not isinstance(node.iter.func, ast.Name) or node.iter.func.id != "range":
            raise CodeExecutionError("bounded_range_required")
        # `is None` rather than a truthiness test: the bound is the range's
        # length, and range(0) is a legal empty loop whose length is 0.
        bound = _constant_range_bound(node.iter)
        if bound is None:
            raise CodeExecutionError("bounded_range_required")
        return self._visit_bounded_loop(node, bound)

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        if node.is_async or not isinstance(node.iter, ast.Call) or not isinstance(node.iter.func, ast.Name) or node.iter.func.id != "range":
            raise CodeExecutionError("bounded_range_required")
        if _constant_range_bound(node.iter) is None:
            raise CodeExecutionError("bounded_range_required")
        return self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        self._validate_comprehension_work(node.generators)
        return self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        self._validate_comprehension_work(node.generators)
        return self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        self._validate_comprehension_work(node.generators)
        return self.generic_visit(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> Any:
        self._validate_comprehension_work(node.generators)
        return self.generic_visit(node)

    def _validate_comprehension_work(self, generators: list[ast.comprehension]) -> None:
        product = 1
        for generator in generators:
            if generator.is_async or not isinstance(generator.iter, ast.Call) or not isinstance(generator.iter.func, ast.Name) or generator.iter.func.id != "range":
                raise CodeExecutionError("bounded_range_required")
            bound = _constant_range_bound(generator.iter)
            if bound is None:
                raise CodeExecutionError("bounded_range_required")
            product *= bound
            if product > MAX_CODE_TOTAL_ITERATIONS:
                raise CodeExecutionError("loop_work_too_large")

    def _visit_bounded_loop(self, node: ast.AST, bound: int) -> Any:
        previous = self.loop_product
        self.loop_product *= bound
        if self.loop_product > MAX_CODE_TOTAL_ITERATIONS:
            raise CodeExecutionError("loop_work_too_large")
        try:
            return self.generic_visit(node)
        finally:
            self.loop_product = previous

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        if not isinstance(node.op, _ALLOWED_BINOPS):
            raise CodeExecutionError("operator_not_allowed")
        if isinstance(node.op, ast.Pow):
            if (
                not isinstance(node.right, ast.Constant)
                or type(node.right.value) is not int
                or not 0 <= node.right.value <= 1_000
            ):
                raise CodeExecutionError("exponent_too_large")
        if (
            isinstance(node.op, ast.Mult)
            and isinstance(node.right, ast.Constant)
            and type(node.right.value) is int
            and abs(node.right.value) > MAX_CODE_TOTAL_ITERATIONS
        ):
            raise CodeExecutionError("sequence_too_large")
        if (
            isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, (str, list, tuple, set, dict))
            and not isinstance(node.right, ast.Constant)
        ):
            raise CodeExecutionError("sequence_bound_required")
        return self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise CodeExecutionError("operator_not_allowed")
        return self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> Any:
        if any(not isinstance(item, _ALLOWED_CMPOPS) for item in node.ops):
            raise CodeExecutionError("comparison_not_allowed")
        return self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        # Do not expose the interpreter's implicit ``__builtins__`` mapping or
        # names that could become dangerous if the allow-list grows later.
        if node.id in _DISALLOWED_NAMES or node.id.startswith("__"):
            raise CodeExecutionError("name_not_allowed")
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> Any:
        value = node.value
        if value is not None and type(value) not in {bool, int, float, str}:
            raise CodeExecutionError("constant_not_allowed")
        if isinstance(value, float) and not math.isfinite(value):
            raise CodeExecutionError("constant_not_allowed")
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if isinstance(node.func, ast.Name):
            if node.func.id not in _ALLOWED_CALLS:
                raise CodeExecutionError("call_not_allowed")
        elif _cortex_attribute_chain(node.func) not in _ALLOWED_CORTEX_CALLS:
            raise CodeExecutionError("call_not_allowed")
        if node.keywords and any(keyword.arg is None for keyword in node.keywords):
            raise CodeExecutionError("call_not_allowed")
        return self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        # Capability objects are opaque. Only the exact broker methods may be
        # read; fields such as ``enabled`` or ``__class__`` must never be
        # exposed or assignable from the restricted program.
        if (
            not isinstance(node.ctx, ast.Load)
            or _cortex_attribute_chain(node) not in _ALLOWED_CORTEX_CALLS | _ALLOWED_CORTEX_NAMESPACES
        ):
            raise CodeExecutionError("attribute_not_allowed")
        return self.generic_visit(node)


def _is_cortex_attribute(node: ast.AST) -> bool:
    return _cortex_attribute_chain(node) in _ALLOWED_CORTEX_CALLS


def _cortex_attribute_chain(node: ast.AST) -> tuple[str, ...] | None:
    """Return a capability API chain, rejecting introspection and dunders.

    Merely rooting an attribute at ``cortex`` is not sufficient: allowing
    ``cortex.fs.__class__`` would expose Python's object graph and defeat the
    restricted execution subset.  The validator therefore only accepts the
    small, explicit capability methods above.
    """

    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        if current.attr.startswith("__"):
            return None
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name) or current.id != "cortex":
        return None
    parts.append("cortex")
    return tuple(reversed(parts))


def _constant_range_bound(node: ast.Call) -> int | None:
    values: list[int] = []
    for argument in node.args:
        if not isinstance(argument, ast.Constant) or type(argument.value) is not int:
            return None
        values.append(argument.value)
    if not 1 <= len(values) <= 3:
        return None
    try:
        result = range(*values)
        length = len(result)
    except (TypeError, ValueError, OverflowError):
        return None
    return length if length <= MAX_CODE_LOOP_ITERATIONS else None


def validate_code_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise CodeExecutionError("source_empty")
    if len(source.encode("utf-8")) > MAX_CODE_SOURCE_BYTES:
        raise CodeExecutionError("source_too_large")
    if "\x00" in source or source.count("\n") > 2_048:
        raise CodeExecutionError("source_too_complex")
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError):
        raise CodeExecutionError("syntax_invalid") from None
    _CodeValidator().visit(tree)
    return source


def capabilities_required_by_source(source: str) -> CodeCapabilities:
    """Return broker namespaces referenced by an already validated program."""

    validate_code_source(source)
    tree = ast.parse(source, mode="exec")
    namespaces: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _cortex_attribute_chain(node)
        if chain and len(chain) >= 2 and chain[0] == "cortex":
            namespaces.add(chain[1])
    return CodeCapabilities(
        filesystem="fs" in namespaces,
        process="process" in namespaces,
        network=bool({"net", "network"} & namespaces),
    )


def _is_reparse_point(path: Path) -> bool:
    """Treat links and Windows junctions as untrusted broker path hops."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _secure_workspace(value: str | os.PathLike[str]) -> Path:
    """Resolve one existing, non-link directory used by a single worker."""

    try:
        candidate = Path(value)
    except (TypeError, ValueError):
        raise CodeExecutionError("workspace_invalid") from None
    if not candidate.is_absolute() or len(str(candidate)) > MAX_CODE_PATH_CHARS:
        raise CodeExecutionError("workspace_invalid")
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir() or _is_reparse_point(resolved):
            raise CodeExecutionError("workspace_invalid")
        cursor = resolved
        while True:
            if _is_reparse_point(cursor):
                raise CodeExecutionError("workspace_invalid")
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent
    except CodeExecutionError:
        raise
    except (OSError, RuntimeError):
        raise CodeExecutionError("workspace_invalid") from None
    return resolved


def _secure_workspace_path(root: Path, value: str, *, directory: bool = False) -> Path:
    """Resolve a broker path without allowing traversal or reparse hops."""

    if not isinstance(value, str) or not value or len(value) > MAX_CODE_PATH_CHARS or "\x00" in value:
        raise ValueError("filesystem path is invalid")
    if value.startswith(("~", "\\\\")):
        raise PermissionError("filesystem path is outside the run workspace")
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    try:
        cursor = candidate
        while True:
            if _is_reparse_point(cursor):
                raise PermissionError("filesystem path uses a link")
            if cursor == root or cursor.parent == cursor:
                break
            cursor = cursor.parent
        resolved = candidate.resolve(strict=False)
    except PermissionError:
        raise
    except (OSError, RuntimeError):
        raise PermissionError("filesystem path is unavailable") from None
    if not resolved.is_relative_to(root):
        raise PermissionError("filesystem path is outside the run workspace")
    if directory and resolved.exists() and not resolved.is_dir():
        raise ValueError("filesystem directory is invalid")
    if resolved.exists() and _is_reparse_point(resolved):
        raise PermissionError("filesystem path uses a link")
    return resolved


@dataclass(slots=True)
class _CapabilityBudget:
    file_operations: int = 0
    file_bytes_read: int = 0
    file_bytes_written: int = 0
    process_operations: int = 0
    network_requests: int = 0

    def take_file(self) -> None:
        self.file_operations += 1
        if self.file_operations > MAX_CODE_FILE_OPERATIONS:
            raise CodeExecutionError("filesystem_limit")

    def take_process(self) -> None:
        self.process_operations += 1
        if self.process_operations > MAX_CODE_PROCESS_OPERATIONS:
            raise CodeExecutionError("process_limit")

    def take_network(self) -> None:
        self.network_requests += 1
        if self.network_requests > MAX_CODE_NETWORK_REQUESTS:
            raise CodeExecutionError("network_limit")


class _CapabilityRuntime:
    def __init__(self, capabilities: CodeCapabilities, workspace: str) -> None:
        self.capabilities = capabilities
        self.workspace = _secure_workspace(workspace)
        budget = _CapabilityBudget()
        self.fs = _FilesystemCapability(capabilities.filesystem, self.workspace, budget)
        self.process = _ProcessCapability(capabilities.process, self.workspace, budget)
        self.net = _NetworkCapability(capabilities.network, budget)
        self.network = self.net


class _FilesystemCapability:
    def __init__(self, enabled: bool, workspace: Path, budget: _CapabilityBudget) -> None:
        self.enabled = enabled
        self.workspace = workspace
        self._budget = budget

    def _check(self) -> None:
        if not self.enabled:
            raise PermissionError("filesystem capability was not approved")
        self._budget.take_file()

    @staticmethod
    def _encoding(value: str) -> str:
        if not isinstance(value, str) or value.casefold() not in _SAFE_ENCODINGS:
            raise ValueError("filesystem encoding is not allowed")
        return value

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        self._check()
        target = _secure_workspace_path(self.workspace, path)
        encoding = self._encoding(encoding)
        try:
            info = target.lstat()
            identity = (
                int(getattr(info, "st_dev", 0)),
                int(getattr(info, "st_ino", 0)),
                int(info.st_size),
                int(getattr(info, "st_mtime_ns", 0)),
                int(getattr(info, "st_ctime_ns", 0)),
                int(getattr(info, "st_nlink", 1)),
            )
            if not target.is_file() or identity[-1] != 1:
                raise ValueError("filesystem target is not a regular file")
            if info.st_size > MAX_CODE_FILE_BYTES:
                raise CodeExecutionError("filesystem_limit")
            with target.open("rb") as stream:
                raw = stream.read(MAX_CODE_FILE_BYTES + 1)
            after = target.lstat()
            after_identity = (
                int(getattr(after, "st_dev", 0)),
                int(getattr(after, "st_ino", 0)),
                int(after.st_size),
                int(getattr(after, "st_mtime_ns", 0)),
                int(getattr(after, "st_ctime_ns", 0)),
                int(getattr(after, "st_nlink", 1)),
            )
            if identity != after_identity or len(raw) > MAX_CODE_FILE_BYTES:
                raise CodeExecutionError("filesystem_changed")
            self._budget.file_bytes_read += len(raw)
            if self._budget.file_bytes_read > MAX_CODE_FILE_BYTES * 4:
                raise CodeExecutionError("filesystem_limit")
            return raw.decode(encoding)
        except (CodeExecutionError, PermissionError, ValueError):
            raise
        except (OSError, UnicodeError):
            raise CodeExecutionError("filesystem_read_failed") from None

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> int:
        self._check()
        target = _secure_workspace_path(self.workspace, path)
        encoding = self._encoding(encoding)
        text = str(content)
        try:
            raw = text.encode(encoding)
        except UnicodeError:
            raise CodeExecutionError("filesystem_write_failed") from None
        if len(raw) > MAX_CODE_FILE_BYTES:
            raise CodeExecutionError("filesystem_limit")
        self._budget.file_bytes_written += len(raw)
        if self._budget.file_bytes_written > MAX_CODE_FILE_BYTES * 4:
            raise CodeExecutionError("filesystem_limit")
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        _secure_workspace_path(self.workspace, str(parent), directory=True)
        if target.exists():
            if _is_reparse_point(target):
                raise PermissionError("filesystem target uses a link")
            try:
                if int(getattr(target.lstat(), "st_nlink", 1)) != 1:
                    raise PermissionError("filesystem target is shared")
            except OSError:
                raise CodeExecutionError("filesystem_write_failed") from None
        try:
            with target.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            info = target.lstat()
            if not target.is_file() or int(getattr(info, "st_nlink", 1)) != 1:
                raise CodeExecutionError("filesystem_write_failed")
        except (CodeExecutionError, PermissionError):
            raise
        except OSError:
            raise CodeExecutionError("filesystem_write_failed") from None
        return len(text)

    def listdir(self, path: str = ".") -> list[str]:
        self._check()
        target = _secure_workspace_path(self.workspace, path, directory=True)
        names: list[str] = []
        try:
            for item in target.iterdir():
                if _is_reparse_point(item):
                    raise PermissionError("filesystem directory contains a link")
                names.append(item.name)
                if len(names) > MAX_CODE_LIST_ENTRIES:
                    raise CodeExecutionError("filesystem_limit")
        except (CodeExecutionError, PermissionError):
            raise
        except OSError:
            raise CodeExecutionError("filesystem_list_failed") from None
        return sorted(names)


class _ProcessCapability:
    def __init__(self, enabled: bool, workspace: Path, budget: _CapabilityBudget) -> None:
        self.enabled = enabled
        self.workspace = workspace
        self._budget = budget

    def run(self, args: list[str] | tuple[str, ...], timeout: float = 5.0) -> dict[str, Any]:
        if not self.enabled:
            raise PermissionError("process capability was not approved")
        if not isinstance(args, (list, tuple)) or not args or len(args) > MAX_CODE_PROCESS_ARGUMENTS:
            raise ValueError("process arguments are invalid")
        if any(
            not isinstance(item, str)
            or not item
            or len(item) > MAX_CODE_PROCESS_ARGUMENT_CHARS
            or "\x00" in item
            or any(ord(char) < 32 and char not in "\t" for char in item)
            for item in args
        ):
            raise ValueError("process arguments are invalid")
        if sum(len(item.encode("utf-8")) for item in args) > MAX_CODE_PROCESS_ARGUMENT_BYTES:
            raise ValueError("process arguments are too large")
        if isinstance(timeout, bool):
            raise ValueError("process timeout is invalid")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise ValueError("process timeout is invalid") from None
        if not math.isfinite(timeout):
            raise ValueError("process timeout is invalid")
        self._budget.take_process()
        timeout = max(0.1, min(timeout, 5.0))
        completed = _run_brokered_process(list(args), workspace=self.workspace, timeout=timeout)
        return {
            "returncode": completed["returncode"],
            "stdout": completed["stdout"],
            "stderr": completed["stderr"],
            "truncated": completed["truncated"],
        }


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time", ctypes.c_longlong),
        ("per_job_user_time", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _WindowsJobIoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operations", ctypes.c_ulonglong),
        ("write_operations", ctypes.c_ulonglong),
        ("other_operations", ctypes.c_ulonglong),
        ("read_transfers", ctypes.c_ulonglong),
        ("write_transfers", ctypes.c_ulonglong),
        ("other_transfers", ctypes.c_ulonglong),
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _WindowsJobBasicLimitInformation),
        ("io_info", _WindowsJobIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsProcessJob:
    """Kill-on-close Job Object for one brokered child process.

    The handle lives in the code worker, so terminating that worker also closes
    the job and tears down any descendants spawned by the approved process.
    """

    _PROCESS_TIME = 0x00000002
    _ACTIVE_PROCESS = 0x00000008
    _PROCESS_MEMORY = 0x00000100
    _KILL_ON_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(
        self,
        process: Any,
        *,
        memory_limit: int = MAX_CODE_MEMORY_BYTES,
        active_process_limit: int = 8,
        cpu_seconds: float = MAX_CODE_TIMEOUT_SECONDS + 1.0,
    ) -> None:
        if os.name != "nt":
            raise CodeExecutionError("process_isolation_unavailable")
        if memory_limit <= 0 or active_process_limit <= 0 or not math.isfinite(cpu_seconds) or cpu_seconds <= 0:
            raise CodeExecutionError("process_isolation_unavailable")
        kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise CodeExecutionError("process_isolation_unavailable")
        self._kernel32 = kernel32
        self._handle = handle
        limits = _WindowsJobExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            self._KILL_ON_CLOSE
            | self._PROCESS_TIME
            | self._ACTIVE_PROCESS
            | self._PROCESS_MEMORY
        )
        limits.basic_limit_information.per_process_user_time = int(cpu_seconds * 10_000_000)
        limits.basic_limit_information.active_process_limit = active_process_limit
        limits.process_memory_limit = memory_limit
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            process_handle = getattr(getattr(process, "_popen", None), "_handle", None)
        if process_handle is None:
            process_handle = getattr(process, "sentinel", None)
        if process_handle is None:
            self.close()
            raise CodeExecutionError("process_isolation_unavailable")
        try:
            if not kernel32.SetInformationJobObject(
                handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ) or not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process_handle)):
                raise CodeExecutionError("process_isolation_unavailable")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        handle, self._handle = getattr(self, "_handle", None), None
        if handle:
            self._kernel32.CloseHandle(handle)


def _run_brokered_process(args: list[str], *, workspace: Path, timeout: float) -> dict[str, Any]:
    """Run one approved process with bounded output and descendant cleanup."""

    kwargs: dict[str, Any] = {
        "cwd": str(workspace),
        "env": {},
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(args, **kwargs)
    job: _WindowsProcessJob | None = None
    readers: list[Thread] = []
    try:
        if os.name == "nt":
            job = _WindowsProcessJob(process)
        output_lock = ThreadLock()
        output_exceeded = ThreadEvent()
        remaining = MAX_CODE_OUTPUT_BYTES
        truncated = False
        buffers = {"stdout": bytearray(), "stderr": bytearray()}

        def drain(name: str, stream: Any) -> None:
            nonlocal remaining, truncated
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    with output_lock:
                        if remaining <= 0:
                            truncated = True
                            continue
                        take = min(len(chunk), remaining)
                        buffers[name].extend(chunk[:take])
                        remaining -= take
                        if take < len(chunk):
                            truncated = True
                            output_exceeded.set()
            finally:
                stream.close()

        readers = [
            Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
            for name in ("stdout", "stderr")
        ]
        for reader in readers:
            reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if output_exceeded.is_set():
                _terminate_brokered_process(process, job)
                raise CodeExecutionError("process_output_limit")
            if time.monotonic() >= deadline:
                _terminate_brokered_process(process, job)
                raise CodeExecutionError("process_timeout")
            time.sleep(0.01)
        process.wait()
        for reader in readers:
            reader.join(timeout=1.0)
        return {
            "returncode": process.returncode,
            "stdout": bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
            "stderr": bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
    except Exception:
        if process.poll() is None:
            _terminate_brokered_process(process, job)
        raise
    finally:
        for reader in readers:
            reader.join(timeout=1.0)
        if job is not None:
            job.close()
        try:
            process.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass


def _terminate_brokered_process(process: subprocess.Popen[bytes], job: _WindowsProcessJob | None) -> None:
    if job is not None:
        job.close()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.kill()
    except OSError:
        pass


def _validate_network_url(url: str) -> tuple[str, str]:
    """Validate a URL and return it with the single vetted IP to dial.

    Returning the resolved address is the point: the caller must connect to
    *this* address rather than let the stack re-resolve the hostname, or a
    time-varying DNS answer can pass the public-address check below and then
    steer the actual connection to loopback/LAN (DNS rebinding). The
    filesystem capability already closes the equivalent race by re-stat'ing
    and comparing an identity tuple after validating.
    """
    if not isinstance(url, str) or len(url) > MAX_CODE_NETWORK_URL_CHARS:
        raise ValueError("network URL is invalid")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("network URL is invalid") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "\x00" in url
        or "%" in host
    ):
        raise ValueError("network URL is invalid")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("network URL is invalid")
    normalized_host = host.rstrip(".").casefold()
    if (
        normalized_host in {"localhost", "localhost.localdomain"}
        or normalized_host.endswith((".localhost", ".local", ".internal"))
    ):
        raise PermissionError("network host is not public")
    try:
        addresses = socket.getaddrinfo(
            normalized_host,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise CodeExecutionError("network_host_unavailable") from None
    if not addresses:
        raise CodeExecutionError("network_host_unavailable")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address[4][0])
        except (ValueError, IndexError):
            raise CodeExecutionError("network_host_unavailable") from None
        if (
            resolved.is_private
            or resolved.is_loopback
            or resolved.is_link_local
            or resolved.is_multicast
            or resolved.is_reserved
            or resolved.is_unspecified
        ):
            raise PermissionError("network host is not public")
    # Every address in this answer passed, so any of them is safe to use.
    # Pin the first so the connection cannot resolve a different one.
    return url, addresses[0][4][0]


def _pinned_connection_classes(pinned_ip: str) -> tuple[type, type]:
    """HTTP/HTTPS connection classes that dial ``pinned_ip`` directly.

    The hostname still travels in the ``Host`` header and in TLS SNI and
    certificate validation, so servers and certificate checks behave exactly
    as they normally would -- only the address the socket connects to is
    forced, which is what closes the rebinding window.
    """
    import http.client

    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = self._create_connection(
                (pinned_ip, self.port), self.timeout, self.source_address
            )
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            self.sock = self._create_connection(
                (pinned_ip, self.port), self.timeout, self.source_address
            )
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            # server_hostname stays the real hostname: certificate validation
            # must not be weakened just because we dialed an address.
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)

    return _PinnedHTTPConnection, _PinnedHTTPSConnection


class _PinnedHTTPHandler(AbstractHTTPHandler):
    """Opens http:// requests through a caller-supplied connection factory."""

    def __init__(self, connection_factory: Callable[..., Any]) -> None:
        super().__init__()
        self._connection_factory = connection_factory

    def http_open(self, req: Any) -> Any:
        return self.do_open(self._connection_factory, req)

    http_request = AbstractHTTPHandler.do_request_


class _PinnedHTTPSHandler(AbstractHTTPHandler):
    """Opens https:// requests through a caller-supplied connection factory."""

    def __init__(self, connection_factory: Callable[..., Any]) -> None:
        super().__init__()
        self._connection_factory = connection_factory

    def https_open(self, req: Any) -> Any:
        return self.do_open(self._connection_factory, req)

    https_request = AbstractHTTPHandler.do_request_


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Re-validates every redirect target and re-pins the opener to it.

    Validating without re-pinning would leave the same hole one hop later:
    the redirect's own connection would re-resolve the new hostname and could
    land on an address this check just rejected.
    """

    def __init__(self, rebind: Callable[[str], None]) -> None:
        super().__init__()
        self._rebind = rebind

    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        _, pinned_ip = _validate_network_url(urljoin(request.full_url, newurl))
        self._rebind(pinned_ip)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


class _NetworkCapability:
    def __init__(self, enabled: bool, budget: _CapabilityBudget) -> None:
        self.enabled = enabled
        self._budget = budget

    def get(self, url: str, timeout: float = 5.0) -> str:
        if not self.enabled:
            raise PermissionError("network capability was not approved")
        if isinstance(timeout, bool):
            raise ValueError("network timeout is invalid")
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise ValueError("network timeout is invalid") from None
        if not math.isfinite(timeout):
            raise ValueError("network timeout is invalid")
        safe_url, pinned_ip = _validate_network_url(url)
        self._budget.take_network()
        timeout = max(0.1, min(timeout, 5.0))

        # A mutable holder so a redirect can re-pin the opener to whatever its
        # own (re-validated) target resolved to. The connection classes are
        # rebuilt per connection rather than once up front -- otherwise every
        # hop would keep dialing the first hop's address.
        current_ip = {"value": pinned_ip}

        def rebind(next_ip: str) -> None:
            current_ip["value"] = next_ip

        def connection_factory(secure: bool) -> Callable[..., Any]:
            def factory(*args: Any, **kwargs: Any) -> Any:
                plain, tls = _pinned_connection_classes(current_ip["value"])
                return (tls if secure else plain)(*args, **kwargs)

            return factory

        opener = build_opener(
            _PinnedHTTPHandler(connection_factory(secure=False)),
            _PinnedHTTPSHandler(connection_factory(secure=True)),
            _SafeRedirectHandler(rebind),
            ProxyHandler({}),
        )
        request = UrlRequest(safe_url, headers={"User-Agent": "Cortex-local-code/1"})
        try:
            with opener.open(request, timeout=timeout) as response:
                _validate_network_url(response.geturl())
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > MAX_CODE_NETWORK_RESPONSE_BYTES:
                            raise CodeExecutionError("network_response_limit")
                    except ValueError:
                        raise CodeExecutionError("network_response_invalid") from None
                content = response.read(MAX_CODE_NETWORK_RESPONSE_BYTES + 1)
                if len(content) > MAX_CODE_NETWORK_RESPONSE_BYTES:
                    raise CodeExecutionError("network_response_limit")
                return content.decode("utf-8", errors="replace")
        except CodeExecutionError:
            raise
        except (HTTPError, URLError, OSError, UnicodeError):
            raise CodeExecutionError("network_request_failed") from None


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                break
            result[str(key)[:100]] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for index, item in enumerate(value):
            if index >= 100:
                break
            result.append(_json_safe(item, depth=depth + 1))
        return result
    return str(value)[:1000]


def _bounded_json_value(value: Any) -> tuple[Any, bool]:
    safe = _json_safe(value)
    try:
        encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return "[unavailable]", True
    if len(encoded.encode("utf-8")) > MAX_CODE_VALUE_BYTES:
        return "[truncated]", True
    return safe, False


def _bounded_text(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CODE_OUTPUT_BYTES:
        return value, False
    return encoded[:MAX_CODE_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


class _BoundedTextWriter(io.TextIOBase):
    """Text sink that prevents untrusted code from growing output in memory."""

    def __init__(self, maximum: int = MAX_CODE_OUTPUT_BYTES) -> None:
        super().__init__()
        self.maximum = maximum
        self._size = 0
        self._chunks: list[str] = []
        self.truncated = False

    def write(self, value: str) -> int:
        text = str(value)
        encoded = text.encode("utf-8", errors="replace")
        remaining = self.maximum - self._size
        if remaining <= 0:
            self.truncated = True
            return len(text)
        if len(encoded) > remaining:
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            self.truncated = True
        self._chunks.append(text)
        self._size += len(text.encode("utf-8", errors="replace"))
        return len(value)

    def getvalue(self) -> str:
        return "".join(self._chunks)


class _ExecutionGuard:
    """Cooperative instruction watchdog for platforms without rlimit."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + MAX_CODE_TIMEOUT_SECONDS
        self.events = 0

    def trace(self, frame: Any, event: str, _arg: Any) -> Callable[..., Any]:
        if frame.f_code.co_filename == "<cortex-code>":
            if event == "call":
                frame.f_trace_opcodes = True
            elif event in {"line", "opcode"}:
                self.events += 1
                if self.events > MAX_CODE_TRACE_EVENTS or time.monotonic() >= self.deadline:
                    raise CodeExecutionError("runtime_limit")
        return self.trace


def run_code_in_worker(source: str, capabilities: Mapping[str, Any] | None = None, workspace: str | None = None) -> CodeExecutionResult:
    """Execute validated source inside a child process boundary."""

    required = capabilities_required_by_source(source)
    grants = CodeCapabilities.from_mapping(capabilities)
    if grants.process or required.process:
        raise CodeExecutionError("process_capability_unavailable")
    started = time.monotonic()
    stdout = _BoundedTextWriter()
    stderr = _BoundedTextWriter()
    runtime = _CapabilityRuntime(grants, workspace or os.getcwd())
    _apply_resource_limits()
    # One namespace, used as both globals and locals. Passing two distinct
    # mappings makes top-level names locals, which a comprehension or generator
    # expression cannot see: its implicit function scope resolves free names
    # through *globals*, so `total = sum([data[i] for i in range(3)])` raises
    # NameError for `data` even though the program is obviously correct. CPython
    # 3.12 hid half of that by inlining list/set/dict comprehensions, which only
    # made the failure version-dependent -- generator expressions still broke,
    # and on 3.11 every comprehension did. Sharing the namespace removes the
    # trap on every supported version. It grants nothing: the builtins mapping
    # and the validated subset are unchanged.
    namespace: dict[str, Any] = {
        "__builtins__": {name: getattr(builtins, name) for name in _ALLOWED_CALLS if hasattr(builtins, name)},
        "cortex": runtime,
    }
    old_stdout, old_stderr = sys.stdout, sys.stderr
    old_trace = sys.gettrace()
    guard = _ExecutionGuard()
    try:
        sys.stdout, sys.stderr = stdout, stderr
        sys.settrace(guard.trace)
        exec(compile(source, "<cortex-code>", "exec"), namespace)
    except CodeExecutionError:
        raise
    except MemoryError:
        raise CodeExecutionError("memory_limit") from None
    except RecursionError:
        raise CodeExecutionError("runtime_limit") from None
    except Exception as exc:
        raise CodeExecutionError("runtime_error", f"{type(exc).__name__}: {exc}") from exc
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        sys.settrace(old_trace)
    out, out_truncated = _bounded_text(stdout.getvalue())
    err, err_truncated = _bounded_text(stderr.getvalue())
    value = namespace.get("_result", namespace.get("result"))
    safe_value, value_truncated = _bounded_json_value(value)
    return CodeExecutionResult(
        stdout=out,
        stderr=err,
        value=safe_value,
        truncated=stdout.truncated or stderr.truncated or out_truncated or err_truncated or value_truncated,
        duration_ms=min(MAX_CODE_TIMEOUT_SECONDS * 1_000, int((time.monotonic() - started) * 1000)),
    )


def _apply_resource_limits() -> None:
    """Apply portable best-effort worker limits before evaluating source."""

    try:
        import resource  # Unix only; unavailable on the Windows desktop build.

        resource.setrlimit(resource.RLIMIT_AS, (MAX_CODE_MEMORY_BYTES, MAX_CODE_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (int(MAX_CODE_TIMEOUT_SECONDS) + 1, int(MAX_CODE_TIMEOUT_SECONDS) + 2))
    except (ImportError, OSError, ValueError):
        # Windows is bounded by the parent wall-clock watchdog and process
        # termination. The platform-specific job object can be added without
        # changing the worker protocol.
        return


def _scrub_worker_environment() -> None:
    """Remove inherited credentials/proxy settings before broker calls."""

    system_root = os.environ.get("SystemRoot") if os.name == "nt" else None
    os.environ.clear()
    if system_root:
        os.environ["SystemRoot"] = system_root


def code_worker_main(connection: Any, source: str, capabilities: Mapping[str, Any], workspace: str) -> None:
    try:
        _scrub_worker_environment()
        # Let the parent distinguish a slow process bootstrap from a program
        # that exceeded its execution budget. This is especially important for
        # frozen desktop launches, where importing the worker can be slower
        # than running a small approved script.
        connection.send({"ok": True, "event": "ready"})
        # Hold here until the parent confirms the resource-limiting Job
        # Object is attached (or that this platform has none to attach).
        # Without this wait, the source below -- untrusted, only bounded by
        # the AST allow-list at this point -- could run during the window
        # between process creation and job assignment, unconstrained by the
        # memory/CPU/process-count limits that window exists to apply before
        # anything else does.
        try:
            go = connection.recv()
        except (EOFError, OSError):
            return
        if not isinstance(go, Mapping) or go.get("go") is not True:
            return
        result = run_code_in_worker(source, capabilities, workspace)
        connection.send({"ok": True, "result": result.as_payload()})
    except CodeExecutionError as exc:
        connection.send({"ok": False, "code": exc.code})
    except Exception as exc:
        connection.send({"ok": False, "code": type(exc).__name__.lower()})
    finally:
        try:
            connection.close()
        except Exception:
            pass


__all__ = [
    "CODE_EXECUTION_PAYLOAD_SCHEMA",
    "CODE_EXECUTION_PROFILE",
    "CODE_EXECUTION_RESULT_SCHEMA",
    "CodeCapabilities",
    "CodeExecutionError",
    "CodeExecutionRequest",
    "CodeExecutionResult",
    "MAX_CODE_OUTPUT_BYTES",
    "MAX_CODE_MEMORY_BYTES",
    "MAX_CODE_SOURCE_BYTES",
    "MAX_CODE_TIMEOUT_SECONDS",
    "code_worker_main",
    "run_code_in_worker",
    "validate_code_source",
]
