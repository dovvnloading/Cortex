"""The future coding-agent command boundary; no API route calls this yet.

``level`` describes an enforced AppContainer or the deliberately less isolated
Job-Object-only fallback. A job controls lifetime/resources, not filesystem or
network access. No caller-supplied environment or secrets are forwarded to either launcher.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
import os
import shutil
import subprocess
import sys
import threading
from typing import Literal, Protocol

from cortex_backend.core.win_jobs import JobLimits, KillOnCloseJob
from cortex_backend.core.win_paths import system_directory

SandboxLevel = Literal["appcontainer", "unsandboxed"]
_ALLOWED_TOOLS = ("python", "node", "npm", "git", "dotnet", "cargo", "go")
_DEFAULT_LIMITS = JobLimits(
    process_memory_bytes=1024 * 1024 * 1024,
    job_memory_bytes=1024 * 1024 * 1024,
    active_processes=16,
    cpu_seconds=120.0,
    restrict_ui=True,
)


class Process(Protocol):
    pid: int
    stdout: object

    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


class ProcessHandle(Protocol):
    level: SandboxLevel
    pid: int

    def stdout_chunks(self) -> Iterator[bytes]: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill_tree(self) -> None: ...


class Launcher(Protocol):
    @property
    def level(self) -> SandboxLevel: ...

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: JobLimits | None = None,
    ) -> ProcessHandle: ...


def canonical_workcopy(workcopy: str | Path) -> Path:
    path = Path(workcopy).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("work copy must be an existing directory")
    # Never give the child modify rights on a volume root or the directory
    # holding our crash-recovery journal (under the current user's home).
    if path == Path(path.anchor) or within(Path.home().resolve(), path):
        raise ValueError("work copy is not an isolated directory")
    return path


def within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def working_directory(cwd: str | Path, workcopy: Path) -> Path:
    path = Path(cwd).resolve(strict=True)
    if not path.is_dir() or not within(path, workcopy):
        raise ValueError("working directory escapes the work copy")
    return path


def toolchain() -> tuple[dict[str, Path], tuple[Path, ...]]:
    """Resolve *only* approved tool names from the host PATH, then pin paths.

    PATH entries themselves are never forwarded: only real directories of
    executables that actually resolved for the requested tool names are kept.
    System32 is an OS-provided AppContainer-readable directory for cmd.exe.
    """
    lookup: dict[str, Path] = {}
    folders: list[Path] = []
    host_path = os.environ.get("PATH", "")
    for tool in _ALLOWED_TOOLS:
        selected = shutil.which(tool, path=host_path)
        if selected is None:
            continue
        try:
            resolved = Path(selected).resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_file():
            continue
        lookup[tool] = resolved
        if resolved.parent not in folders:
            folders.append(resolved.parent)
    system = system_directory()
    if system is not None:
        cmd = system / "cmd.exe"
        if cmd.is_file():
            lookup["cmd"] = cmd.resolve(strict=True)
            if system not in folders:
                folders.append(system)
    return lookup, tuple(folders)


def command_argv(argv: Sequence[str], workcopy: Path, tools: Mapping[str, Path]) -> list[str]:
    if not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
        raise ValueError("argv must contain nonempty arguments without NUL bytes")
    first = argv[0]
    selected = tools.get(first.casefold().removesuffix(".exe").removesuffix(".cmd"))
    if selected is None:
        candidate = Path(first)
        if not candidate.is_absolute():
            raise PermissionError("executable is not an allowed PATH tool or absolute work-copy path")
        selected = candidate.resolve(strict=True)
        if not selected.is_file() or not (
            within(selected, workcopy) or any(within(selected, folder) for folder in (p.parent for p in tools.values()))
        ):
            raise PermissionError("executable is outside the work copy and permitted toolchain")
    if selected.suffix.casefold() in {".cmd", ".bat"}:
        cmd = tools.get("cmd")
        if cmd is None:
            raise PermissionError("command scripts require the system cmd.exe")
        # cmd.exe is unavoidable for npm.cmd; never interpolate through a shell
        # when starting an .exe, and do not accept shell control characters.
        if any(any(ch in arg for ch in "&|<>^\r\n%!\"") for arg in argv):
            raise ValueError("shell metacharacters are not accepted in command-script arguments")
        return [str(cmd), "/d", "/s", "/c", subprocess.list2cmdline([str(selected), *argv[1:]])]
    if selected.suffix.casefold() != ".exe" and sys.platform == "win32":
        raise PermissionError("only Windows executables and command scripts may be launched")
    return [str(selected), *argv[1:]]


def child_environment(
    workcopy: Path, folders: Sequence[Path], *, appcontainer: bool = False,
) -> dict[str, str]:
    """Build a minimal environment without forwarding arbitrary parent keys.

    AppContainer creation requires the real user's LOCALAPPDATA for Windows
    profile lookup. The job-only fallback instead keeps LOCALAPPDATA inside
    the work copy: it has no AppContainer profile or filesystem isolation.
    """
    system = system_directory()
    # A real Windows launch validates all required entries at the Win32 boundary.
    # Test/non-Windows callers with no system directory keep the minimal allowlist.
    temp = workcopy / ".cortex-agent-tmp"
    temp.mkdir(exist_ok=True)
    if not temp.resolve(strict=True).is_relative_to(workcopy):
        raise ValueError("TEMP redirects outside the work copy")
    result = {"PATH": os.pathsep.join(str(folder) for folder in folders), "TEMP": str(temp), "PYTHONUTF8": "1"}
    if system is not None:
        result["SystemRoot"] = str(system.parent)
        result["SystemDrive"] = system.drive
        result["TMP"] = str(temp)
        if appcontainer:
            # This is needed by CreateProcessW to locate the per-run profile.
            # No permissions on the user's real LOCALAPPDATA are changed.
            local_appdata = os.environ.get("LOCALAPPDATA")
            if (not local_appdata or not Path(local_appdata).is_absolute()
                    or not Path(local_appdata).is_dir()):
                raise OSError("Windows LOCALAPPDATA directory is unavailable")
            result["LOCALAPPDATA"] = str(Path(local_appdata).resolve(strict=True))
        else:
            local_appdata_dir = workcopy / ".cortex-agent-localappdata"
            local_appdata_dir.mkdir(exist_ok=True)
            if not local_appdata_dir.resolve(strict=True).is_relative_to(workcopy):
                raise ValueError("LOCALAPPDATA redirects outside the work copy")
            result["LOCALAPPDATA"] = str(local_appdata_dir)
    return result

class RunningProcess:
    """Drains output without backpressure and always reaps the job and ACLs.

    A watcher owns cleanup even when the caller never calls ``wait``. Bounded
    output keeps a noisy agent from filling the host's memory indefinitely.
    """

    def __init__(
        self, process: Process, job: KillOnCloseJob, level: SandboxLevel,
        cleanup: Callable[[], None],
    ) -> None:
        self.level = level
        self.pid = process.pid
        self._process = process
        self._job = job
        self._cleanup = cleanup
        self._finished = threading.Event()
        self._output_done = False
        self._condition = threading.Condition()
        self._chunks: deque[bytes] = deque(maxlen=256)
        self._result: int | None = None
        self._error: Exception | None = None
        self._tree_lock = threading.Lock()
        self._reader = threading.Thread(target=self._drain, name="cortex-agent-stdout", daemon=True)
        self._watcher = threading.Thread(target=self._watch, name="cortex-agent-wait", daemon=True)
        self._reader.start()
        self._watcher.start()

    def _drain(self) -> None:
        try:
            stream = self._process.stdout
            assert hasattr(stream, "read")
            while chunk := stream.read(8192):
                with self._condition:
                    self._chunks.append(chunk)
                    self._condition.notify_all()
        except (OSError, ValueError):
            pass  # the job may have been killed while draining
        finally:
            with self._condition:
                self._output_done = True
                self._condition.notify_all()

    def _watch(self) -> None:
        try:
            self._result = self._process.wait()
        except Exception as exc:
            self._error = exc
        finally:
            # Closing the job first also kills descendants of an exited parent.
            with self._tree_lock:
                self._job.close()
            try:
                self._reader.join(timeout=2)
                close = getattr(self._process, "close", None)
                if callable(close):
                    close()
            except Exception as exc:
                self._error = exc
            try:
                self._cleanup()
            except Exception as exc:
                self._error = exc  # journal is kept for restart recovery
            finally:
                self._finished.set()

    def stdout_chunks(self) -> Iterator[bytes]:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._chunks) or self._output_done)
                if self._chunks:
                    chunk = self._chunks.popleft()
                else:
                    return
            yield chunk

    def wait(self, timeout: float | None = None) -> int:
        if not self._finished.wait(timeout):
            self.kill_tree()
            raise TimeoutError("agent process exceeded its timeout and its tree was killed")
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    def kill_tree(self) -> None:
        with self._tree_lock:
            self._job.close()
            try:
                self._process.kill()
            except OSError:
                pass
        if not self._finished.wait(30):
            raise TimeoutError("agent process teardown did not finish")
        if self._error is not None:
            raise self._error

    def close(self) -> None:
        self.kill_tree()

    def __enter__(self) -> RunningProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.kill_tree()


def job_limits(limits: JobLimits | None) -> JobLimits:
    return limits if limits is not None else _DEFAULT_LIMITS
