"""Bounded supervision for the in-process Uvicorn server and Vite child."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


def wait_for_http(
    url: str,
    *,
    timeout: float = 30.0,
    is_alive: Callable[[], bool] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_alive is not None and not is_alive():
            return False
        try:
            request = Request(url, headers={"Host": "127.0.0.1"})
            with urlopen(request, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    return True
        except (OSError, URLError):
            time.sleep(0.1)
    return False


@dataclass
class ServerSupervisor:
    """Run Uvicorn in an owned thread and expose bounded shutdown."""

    server: Any
    thread: threading.Thread | None = field(default=None, init=False)
    error: BaseException | None = field(default=None, init=False)
    started: threading.Event = field(default_factory=threading.Event, init=False)
    exited_unexpectedly: threading.Event = field(
        default_factory=threading.Event, init=False
    )

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError("server supervisor already started")

        def run() -> None:
            self.started.set()
            try:
                self.server.run()
            except BaseException as exc:  # surfaced to the launcher loop
                self.error = exc
            finally:
                if not self.server.should_exit:
                    self.exited_unexpectedly.set()

        self.thread = threading.Thread(target=run, name="cortex-uvicorn", daemon=True)
        self.thread.start()
        if not self.started.wait(timeout=5.0):
            raise RuntimeError("Cortex backend thread did not start within 5 seconds.")

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def accepting_startup(self) -> bool:
        """Remain probeable until the server exits or is asked to stop."""
        return not self.exited_unexpectedly.is_set() and not self.server.should_exit

    def stop(self, *, timeout: float = 15.0) -> None:
        self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        if self.thread is not None and self.thread.is_alive():
            raise TimeoutError("Cortex backend did not stop within the shutdown grace period.")
        if self.error is not None:
            raise RuntimeError("Cortex backend stopped with an error.") from self.error


class ChildProcessSupervisor:
    """Own a development child and terminate its complete Windows tree."""

    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str] | None = None):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.process: subprocess.Popen[str] | None = None
        self._log_thread: threading.Thread | None = None

    def start(self) -> None:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError("Could not start the supervised frontend process.") from exc

        def stream_logs() -> None:
            if self.process is None or self.process.stdout is None:
                return
            for line in self.process.stdout:
                print(f"[vite] {line.rstrip()}", flush=True)

        self._log_thread = threading.Thread(target=stream_logs, name="cortex-vite-logs", daemon=True)
        self._log_thread.start()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.process.poll() if self.process is not None else None

    def stop(self, *, timeout: float = 10.0) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        pid = self.process.pid
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:  # pragma: no cover - Windows is the supported launcher target
            self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("The supervised frontend process did not stop.") from exc
