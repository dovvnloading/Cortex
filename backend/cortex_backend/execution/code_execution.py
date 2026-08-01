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
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import io
import math
import os
from pathlib import Path
import subprocess
import sys
import signal
from threading import Lock as ThreadLock, Thread
import time
from typing import Any, Mapping
from urllib.request import Request as UrlRequest, urlopen


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
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CodeCapabilities":
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


@dataclass(frozen=True, slots=True)
class CodeExecutionRequest:
    owner: str
    request_id: str
    source: str
    intent_summary: str
    capabilities: CodeCapabilities = CodeCapabilities()

    def __post_init__(self) -> None:
        if not self.owner or not self.request_id:
            raise ValueError("owner and request_id are required")
        if not isinstance(self.source, str) or not self.source.strip():
            raise CodeExecutionError("source_empty")
        if len(self.source.encode("utf-8")) > MAX_CODE_SOURCE_BYTES:
            raise CodeExecutionError("source_too_large")
        if not isinstance(self.intent_summary, str) or not self.intent_summary.strip():
            raise CodeExecutionError("intent_invalid")
        if len(self.intent_summary) > 500:
            raise CodeExecutionError("intent_invalid")
        validate_code_source(self.source)

    @property
    def source_digest(self) -> str:
        import hashlib

        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": CODE_EXECUTION_PAYLOAD_SCHEMA,
            "language": "python",
            "source": self.source,
            "intent_summary": self.intent_summary.strip(),
            "source_digest": self.source_digest,
            "capabilities": self.capabilities.as_dict(),
        }


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
        if not _constant_range_bound(node.iter):
            raise CodeExecutionError("bounded_range_required")
        return self._visit_bounded_loop(node, _constant_range_bound(node.iter) or 0)

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        if node.is_async or not isinstance(node.iter, ast.Call) or not isinstance(node.iter.func, ast.Name) or node.iter.func.id != "range":
            raise CodeExecutionError("bounded_range_required")
        if not _constant_range_bound(node.iter):
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
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant) and isinstance(node.right.value, int) and abs(node.right.value) > 10_000:
            raise CodeExecutionError("exponent_too_large")
        if (
            isinstance(node.op, ast.Mult)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, int)
            and abs(node.right.value) > MAX_CODE_TOTAL_ITERATIONS
        ):
            raise CodeExecutionError("sequence_too_large")
        return self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        if not isinstance(node.op, _ALLOWED_UNARYOPS):
            raise CodeExecutionError("operator_not_allowed")
        return self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> Any:
        if any(not isinstance(item, _ALLOWED_CMPOPS) for item in node.ops):
            raise CodeExecutionError("comparison_not_allowed")
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
        if not isinstance(argument, ast.Constant) or not isinstance(argument.value, int):
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
    try:
        tree = ast.parse(source, mode="exec")
    except (SyntaxError, ValueError):
        raise CodeExecutionError("syntax_invalid") from None
    _CodeValidator().visit(tree)
    return source


class _CapabilityRuntime:
    def __init__(self, capabilities: CodeCapabilities, workspace: str) -> None:
        self.capabilities = capabilities
        self.workspace = Path(workspace)
        self.fs = _FilesystemCapability(capabilities.filesystem, self.workspace)
        self.process = _ProcessCapability(capabilities.process, self.workspace)
        self.net = _NetworkCapability(capabilities.network)
        self.network = self.net


class _FilesystemCapability:
    def __init__(self, enabled: bool, workspace: Path) -> None:
        self.enabled = enabled
        self.workspace = workspace

    def _check(self) -> None:
        if not self.enabled:
            raise PermissionError("filesystem capability was not approved")

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        self._check()
        return Path(path).expanduser().read_text(encoding=encoding)

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> int:
        self._check()
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding=encoding)
        return len(str(content))

    def listdir(self, path: str = ".") -> list[str]:
        self._check()
        return sorted(item.name for item in Path(path).expanduser().iterdir())


class _ProcessCapability:
    def __init__(self, enabled: bool, workspace: Path) -> None:
        self.enabled = enabled
        self.workspace = workspace

    def run(self, args: list[str] | tuple[str, ...], timeout: float = 5.0) -> dict[str, Any]:
        if not self.enabled:
            raise PermissionError("process capability was not approved")
        if not isinstance(args, (list, tuple)) or not args or any(not isinstance(item, str) for item in args):
            raise ValueError("process arguments must be a non-empty string list")
        timeout = max(0.1, min(float(timeout), 5.0))
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

    _KILL_ON_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
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
        limits.basic_limit_information.limit_flags = self._KILL_ON_CLOSE
        try:
            if not kernel32.SetInformationJobObject(
                handle,
                self._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ) or not kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
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
    try:
        if os.name == "nt":
            job = _WindowsProcessJob(process)
        output_lock = ThreadLock()
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
            finally:
                stream.close()

        readers = [
            Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
            for name in ("stdout", "stderr")
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_brokered_process(process, job)
            raise CodeExecutionError("process_timeout") from exc
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


class _NetworkCapability:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def get(self, url: str, timeout: float = 5.0) -> str:
        if not self.enabled:
            raise PermissionError("network capability was not approved")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("network access requires an http or https URL")
        timeout = max(0.1, min(float(timeout), 5.0))
        request = UrlRequest(url, headers={"User-Agent": "Cortex-local-code/1"})
        with urlopen(request, timeout=timeout) as response:
            return response.read(MAX_CODE_OUTPUT_BYTES + 1).decode("utf-8", errors="replace")[:MAX_CODE_OUTPUT_BYTES]


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key)[:100]: _json_safe(item, depth=depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:100]]
    return str(value)[:1000]


def _bounded_text(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CODE_OUTPUT_BYTES:
        return value, False
    return encoded[:MAX_CODE_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


def run_code_in_worker(source: str, capabilities: Mapping[str, Any] | None = None, workspace: str | None = None) -> CodeExecutionResult:
    """Execute validated source inside a child process boundary."""

    validate_code_source(source)
    grants = CodeCapabilities.from_mapping(capabilities)
    started = time.monotonic()
    stdout = io.StringIO()
    stderr = io.StringIO()
    runtime = _CapabilityRuntime(grants, workspace or os.getcwd())
    _apply_resource_limits()
    globals_dict: dict[str, Any] = {
        "__builtins__": {name: getattr(builtins, name) for name in _ALLOWED_CALLS if hasattr(builtins, name)},
        "cortex": runtime,
    }
    locals_dict: dict[str, Any] = {}
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = stdout, stderr
        exec(compile(source, "<cortex-code>", "exec"), globals_dict, locals_dict)
    except CodeExecutionError:
        raise
    except Exception as exc:
        raise CodeExecutionError("runtime_error", f"{type(exc).__name__}: {exc}") from exc
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
    out, out_truncated = _bounded_text(stdout.getvalue())
    err, err_truncated = _bounded_text(stderr.getvalue())
    value = locals_dict.get("_result", locals_dict.get("result"))
    return CodeExecutionResult(
        stdout=out,
        stderr=err,
        value=_json_safe(value),
        truncated=out_truncated or err_truncated,
        duration_ms=int((time.monotonic() - started) * 1000),
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


def code_worker_main(connection: Any, source: str, capabilities: Mapping[str, Any], workspace: str) -> None:
    try:
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
