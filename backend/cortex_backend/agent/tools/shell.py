"""Policy-gated shell commands in the agent's disposable Windows work copy.

The shell never accepts a caller environment or cwd. The launcher owns the
allowlisted environment, AppContainer access and process-tree job. Approvals
are one-shot ToolContext grants; neither the model nor the tool arguments can
set them. Output is ephemeral: only bounded excerpts reach tool observations.
"""

from __future__ import annotations

from collections.abc import Callable
from _thread import LockType
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Final, cast

from cortex_backend.core.win_paths import system32, system_directory

from ..policy import Mode, classify, decide
from ..protocol import ToolCall, ToolSpec
from ..sandbox import AutoLauncher, JobOnlyLauncher, Launcher, ProcessHandle, UnsandboxedFallbackRequired
from ..sandbox.base import canonical_workcopy
from ..tool_registry import NeedsApproval, ToolContext, ToolInterrupted, ToolRegistry, ToolResult
from ..workcopy import WorkCopyError, _check_root

_HEAD_BYTES: Final = 4096
_TAIL_BYTES: Final = 4096
_PENDING_BYTES: Final = 8192
_STREAM_BYTES: Final = 1024 * 1024
_MAX_BACKGROUNDS: Final = 2
_MAX_TIMEOUT: Final = 120.0

_COMMAND = {"type": "string", "minLength": 1, "maxLength": 32768}
_NETWORK = {"type": "boolean", "default": False}
_ID = {"type": "string", "minLength": 1, "maxLength": 32}


def _schema(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object", "properties": {**required, **(optional or {})},
        "required": list(required), "additionalProperties": False,
    }


SHELL_TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        "run_command", "Run a bounded command in the disposable work copy. Network requires separate consent.",
        _schema({"command": _COMMAND, "timeout_s": {"type": "number", "minimum": 0.1, "maximum": _MAX_TIMEOUT}},
                {"network": _NETWORK, "shell": {"type": "string", "enum": ["powershell", "cmd"]}}),
    ),
    ToolSpec(
        "start_background", "Start one of at most two dev-server processes owned by this run.",
        _schema({"command": _COMMAND}, {"network": _NETWORK, "shell": {"type": "string", "enum": ["powershell", "cmd"]}}),
    ),
    ToolSpec("read_background", "Read buffered output and status of this run's background process.",
             _schema({"id": _ID})),
    ToolSpec("stop_background", "Kill a background process and its descendants.", _schema({"id": _ID})),
)


def _shell_argv(command: str, shell: str) -> list[str]:
    """Use pinned Windows system binaries, not PATH or a modified ComSpec."""
    if shell == "powershell":
        return [system32("powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", command]
    if shell != "cmd":
        raise ValueError("unsupported shell")
    directory = system_directory()
    if sys.platform == "win32":
        if directory is None:
            raise OSError("Windows system directory is unavailable")
        expected = (directory / "cmd.exe").resolve(strict=True)
        candidate = Path(os.environ.get("ComSpec", str(expected))).resolve(strict=True)
        if candidate != expected:
            raise PermissionError("ComSpec is not the system cmd.exe")
        executable = str(candidate)
    else:
        executable = os.environ.get("ComSpec", "cmd.exe")
    return [executable, "/d", "/s", "/c", command]


@dataclass(slots=True)
class _Excerpt:
    head: bytes = b""
    tail: bytes = b""
    total: int = 0

    def add(self, value: bytes) -> None:
        needed = max(0, _HEAD_BYTES - len(self.head))
        self.head += value[:needed]
        self.tail = (self.tail + value[needed:])[-_TAIL_BYTES:]
        self.total += len(value)

    def result(self) -> dict[str, object]:
        truncated = self.total > len(self.head) + len(self.tail)
        gap = b"\n... [output omitted] ...\n" if truncated else b""
        return {
            "output": (self.head + gap + self.tail).decode("utf-8", errors="replace"),
            "output_bytes": self.total, "truncated": truncated,
        }


class _Pump:
    """Read asynchronously so cancellation works even without shell output."""

    def __init__(self, process: ProcessHandle, on_chunk: Callable[[bytes], None]) -> None:
        self.process = process
        self.on_chunk = on_chunk
        self.excerpt = _Excerpt()
        self.done = Event()
        self.exit_code: int | None = None
        self.failed = False
        self.thread = Thread(target=self._drain, name="cortex-agent-shell-output", daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            for raw in self.process.stdout_chunks():
                if not isinstance(raw, bytes):
                    raise TypeError("sandbox output must be bytes")
                self.excerpt.add(raw)
                try:
                    self.on_chunk(raw)
                except Exception:
                    pass  # output notification cannot weaken containment or block cleanup
            self.exit_code = self.process.wait()
        except Exception:
            self.failed = True
        finally:
            self.done.set()


@dataclass(slots=True)
class _Background:
    process: ProcessHandle
    pump: _Pump | None = None
    pending: bytes = b""
    dropped: bool = False
    stopped: bool = False
    lock: LockType = field(default_factory=Lock)

    def append(self, data: bytes) -> None:
        with self.lock:
            if len(self.pending) + len(data) > _PENDING_BYTES:
                self.dropped = True
            self.pending = (self.pending + data)[-_PENDING_BYTES:]

    def read(self) -> tuple[str, bool]:
        with self.lock:
            text = self.pending.decode("utf-8", errors="replace")
            dropped = self.dropped
            self.pending = b""
            self.dropped = False
        return text, dropped


class ShellTools:
    """Own all commands and background trees of exactly one agent run."""

    def __init__(
        self, workcopy: str | os.PathLike[str], run_id: str, *, mode: str,
        on_output: Callable[[str], None] | None = None,
        launcher_factory: Callable[[Path, str], Launcher] = AutoLauncher,
        jobonly_factory: Callable[[Path], Launcher] = JobOnlyLauncher,
    ) -> None:
        if mode not in {"plan_only", "ask", "auto_sandboxed"}:
            raise ValueError("invalid agent mode")
        if not run_id or len(run_id) > 40 or not all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in run_id):
            raise ValueError("invalid run identifier")
        self.workcopy = canonical_workcopy(Path(workcopy))
        info = self.workcopy.lstat()
        self._identity = (info.st_dev, info.st_ino)
        self.run_id = run_id
        self.mode = cast(Mode, mode)
        self._output = on_output
        self._launcher_factory = launcher_factory
        self._jobonly_factory = jobonly_factory
        self._lock = Lock()
        self._closed = False
        self._jobonly = False
        self._serial = 0
        self._streamed = 0
        self._stream_truncated = False
        self._foreground: dict[int, ProcessHandle] = {}
        self._background: dict[str, _Background] = {}

    def _emit(self, data: bytes) -> None:
        if self._output is None or not data:
            return
        with self._lock:
            allowed = max(0, _STREAM_BYTES - self._streamed)
            part = data[:allowed]
            self._streamed += len(part)
            truncated = len(data) > allowed and not self._stream_truncated
            if truncated:
                self._stream_truncated = True
        # SSE replay is bounded even for a runaway dev server; output is still
        # fully drained and the tool observation retains its head and tail.
        for offset in range(0, len(part), 8192):
            try:
                self._output(part[offset:offset + 8192].decode("utf-8", errors="replace"))
            except Exception:
                return
        if truncated:
            try:
                self._output("\n[Further shell output omitted from event stream]\n")
            except Exception:
                pass

    def _error(self, code: str) -> ToolResult:
        return ToolResult(json.dumps({"error": code}), summary="Shell tool refused", success=False)

    def _launcher(self) -> Launcher:
        with self._lock:
            if self._closed:
                raise RuntimeError("run has ended")
            self._serial += 1
            number = self._serial
            fallback = self._jobonly
        if fallback:
            return self._jobonly_factory(self.workcopy)
        return self._launcher_factory(self.workcopy, f"{self.run_id}-{number:06x}")

    def _permission(
        self, command: str, shell: str, network: bool, context: ToolContext, launcher: Launcher,
        tool: str, *, check_risk: str | None = None,
    ) -> NeedsApproval | ToolResult | None:
        level = launcher.level
        if level not in {"appcontainer", "unsandboxed"}:
            return self._error("unknown_sandbox_level")
        verdict = classify(command, shell, level)
        decision = decide(verdict, self.mode)
        if check_risk not in {None, "safe", "ask", "network", "dangerous"}:
            return self._error("invalid_check_risk")
        if decision == "refuse" or check_risk == "dangerous":
            return self._error("command_denied")
        if level == "unsandboxed" and not network:
            # No Job Object can enforce network=False. Park each implicit
            # check visibly, then still refuse on approval; never silently
            # downgrade an offline check to network-enabled execution.
            if check_risk is not None and not context.approved:
                return NeedsApproval({
                    "tool": tool, "command": command, "shell": shell, "level": level,
                    "network": False, "reason": "unsandboxed_offline_unavailable",
                    "summary": "Job-only cannot isolate the network; this check cannot execute offline.",
                })
            return self._error("unsandboxed_cannot_disable_network")
        if decision == "ask" or network or check_risk in {"ask", "network"}:
            if not context.approved:
                return NeedsApproval({
                    "tool": tool, "command": command, "shell": shell, "level": level,
                    "network": network,
                    "summary": "Authorize network access for this specific command, as well as execution." if network
                    else "Approve this command in the displayed sandbox level.",
                    "reason": "network_consent_required" if network else "command_approval_required",
                })
        return None

    def _prepare(
        self, call: ToolCall, context: ToolContext, *, check_risk: str | None = None,
    ) -> tuple[list[str], str, bool, Launcher] | NeedsApproval | ToolResult:
        if context.stopped():
            raise ToolInterrupted("shell interrupted")
        command = call.arguments.get("command")
        shell = call.arguments.get("shell", "powershell")
        network = call.arguments.get("network", False)
        allowed = {"command", "network", "shell"}
        if call.name == "run_command":
            allowed.add("timeout_s")
        if (set(call.arguments) - allowed or not isinstance(command, str) or not command.strip()
                or len(command) > 32768 or "\x00" in command or not isinstance(shell, str)
                or shell not in {"powershell", "cmd"} or not isinstance(network, bool)):
            return self._error("invalid_arguments")
        try:
            _check_root(self.workcopy, self._identity)
            launcher = self._launcher()
            permission = self._permission(
                command, shell, network, context, launcher, call.name, check_risk=check_risk,
            )
            if permission is not None:
                return permission
            argv = _shell_argv(command, shell)
        except (OSError, ValueError, PermissionError, WorkCopyError, RuntimeError):
            return self._error("sandbox_unavailable")
        return argv, command, network, launcher

    def _start(
        self, argv: list[str], command: str, network: bool, launcher: Launcher,
        context: ToolContext, tool: str,
    ) -> ProcessHandle | NeedsApproval | ToolResult:
        if context.stopped():
            raise ToolInterrupted("shell interrupted")
        try:
            # The launcher must construct its *own* environment; env is never
            # supplied, and cwd is never selected from model/tool arguments.
            process = launcher.start(argv, self.workcopy, network=network)
        except UnsandboxedFallbackRequired:
            with self._lock:
                self._jobonly = True
            # A prior approval covered the AppContainer level, not a downgrade.
            # The loop cannot park the same approved step twice. Require a new
            # command call and a new, visibly unsandboxed approval instead.
            if context.approved or not network:
                return self._error("unsandboxed_requires_new_approval")
            return NeedsApproval({
                "tool": tool, "command": command, "level": "unsandboxed", "network": True,
                "summary": "AppContainer unavailable. Approve network-capable, unsandboxed Job Object execution.",
                "reason": "unsandboxed_consent_required",
            })
        except (OSError, ValueError, PermissionError, RuntimeError):
            return self._error("launch_failed")
        if process.level != launcher.level or (process.level != "appcontainer" and not network):
            process.kill_tree()
            return self._error("sandbox_level_changed")
        with self._lock:
            closed = self._closed
        if closed:
            process.kill_tree()
            return self._error("run_ended")
        return process

    def _run(
        self, call: ToolCall, context: ToolContext, *, check_risk: str | None = None,
    ) -> ToolResult | NeedsApproval:
        timeout = call.arguments.get("timeout_s")
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not 0.1 <= timeout <= _MAX_TIMEOUT):
            return self._error("invalid_timeout")
        prepared = self._prepare(call, context, check_risk=check_risk)
        if not isinstance(prepared, tuple):
            return prepared
        argv, command, network, launcher = prepared
        started = self._start(argv, command, network, launcher, context, call.name)
        if not hasattr(started, "stdout_chunks"):
            return started
        process = cast(ProcessHandle, started)
        with self._lock:
            if self._closed:
                closed = True
            else:
                closed = False
                self._foreground[id(process)] = process
        if closed:
            process.kill_tree()
            return self._error("run_ended")
        pump = _Pump(process, self._emit)
        end = monotonic() + timeout
        timed_out = False
        try:
            while not pump.done.wait(0.05):
                if context.stopped():
                    process.kill_tree()
                    raise ToolInterrupted("shell cancelled or run deadline reached")
                if monotonic() >= end:
                    timed_out = True
                    process.kill_tree()
                    break
            pump.done.wait(1)
            if context.stopped():
                raise ToolInterrupted("shell cancelled or run deadline reached")
            # A job killed on timeout can report 0 despite never completing.
            # Cancellation raises ToolInterrupted and never returns a success observation.
            exit_code = None if timed_out and pump.exit_code == 0 else pump.exit_code
            data = {"exit_code": exit_code, "timed_out": timed_out, **pump.excerpt.result()}
            if pump.failed and not timed_out:
                return self._error("process_failed")
            return ToolResult(
                json.dumps(data, ensure_ascii=False),
                summary="Command timed out" if timed_out else "Command exited",
                success=not timed_out and exit_code == 0,
            )
        finally:
            with self._lock:
                self._foreground.pop(id(process), None)
            if not pump.done.is_set():
                process.kill_tree()

    def _background_output(self, identifier: str, data: bytes) -> None:
        with self._lock:
            background = self._background.get(identifier)
        if background is not None:
            background.append(data)
        self._emit(data)

    def _start_background(self, call: ToolCall, context: ToolContext) -> ToolResult | NeedsApproval:
        with self._lock:
            count = sum(
                1 for background in self._background.values()
                if background.pump is None or not background.pump.done.is_set() and not background.stopped
            )
            if count >= _MAX_BACKGROUNDS:
                return self._error("background_limit")
        prepared = self._prepare(call, context)
        if not isinstance(prepared, tuple):
            return prepared
        argv, command, network, launcher = prepared
        started = self._start(argv, command, network, launcher, context, call.name)
        if not hasattr(started, "stdout_chunks"):
            return started
        process = cast(ProcessHandle, started)
        with self._lock:
            if self._closed:
                closed = True
                identifier = ""
                background = None
            else:
                closed = False
                self._serial += 1
                identifier = f"bg-{self._serial:06x}"
                background = _Background(process)
                self._background[identifier] = background
        if closed or background is None:
            process.kill_tree()
            return self._error("run_ended")
        background.pump = _Pump(process, lambda data: self._background_output(identifier, data))
        return ToolResult(
            json.dumps({"id": identifier, "pid": process.pid, "level": process.level}),
            summary="Background command started",
        )

    def _find_background(self, call: ToolCall) -> _Background | None:
        identifier = call.arguments.get("id")
        if not isinstance(identifier, str) or set(call.arguments) != {"id"}:
            return None
        with self._lock:
            return self._background.get(identifier)

    def _read_background(self, call: ToolCall) -> ToolResult:
        background = self._find_background(call)
        if background is None:
            return self._error("unknown_background")
        text, truncated = background.read()
        pump = background.pump
        return ToolResult(json.dumps({
            "output": text, "truncated": truncated,
            "running": pump is None or not pump.done.is_set() and not background.stopped,
            "exit_code": pump.exit_code if pump is not None else None,
        }, ensure_ascii=False), summary="Background output read")

    def _stop_background(self, call: ToolCall) -> ToolResult:
        background = self._find_background(call)
        if background is None:
            return self._error("unknown_background")
        background.stopped = True
        try:
            background.process.kill_tree()
        except (OSError, RuntimeError, TimeoutError):
            return self._error("background_stop_failed")
        return ToolResult(json.dumps({"stopped": True}), summary="Background process stopped")

    def handle(
        self, call: ToolCall, context: ToolContext, *, check_risk: str | None = None,
    ) -> ToolResult | NeedsApproval:
        if context.stopped():
            raise ToolInterrupted("shell interrupted")
        with self._lock:
            if self._closed:
                return self._error("run_ended")
        if call.name == "run_command":
            return self._run(call, context, check_risk=check_risk)
        if check_risk is not None:
            return self._error("invalid_arguments")
        if call.name == "start_background":
            return self._start_background(call, context)
        if call.name == "read_background":
            return self._read_background(call)
        if call.name == "stop_background":
            return self._stop_background(call)
        return self._error("unknown_tool")

    def close(self) -> None:
        """Idempotently kill all live process trees on any terminal run path."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            processes = [*self._foreground.values(), *(bg.process for bg in self._background.values())]
        for process in processes:
            try:
                process.kill_tree()
            except (OSError, RuntimeError, TimeoutError):
                pass  # Job/launcher also owns teardown even if the caller leaves.


def register_shell_tools(
    registry: ToolRegistry, workcopy: str | os.PathLike[str], run_id: str, *, mode: str,
    on_output: Callable[[str], None] | None = None,
) -> ShellTools:
    """Register all four shell operations on an opted-in run, returning cleanup owner."""
    shell = ShellTools(workcopy, run_id, mode=mode, on_output=on_output)
    for spec in SHELL_TOOLS:
        registry.register(spec, shell.handle)
    return shell
