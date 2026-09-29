"""The brokered child process is contained before it runs.

The ``process`` capability is refused today, so this is the path that has to be
right before it is ever enabled. On Windows the child is created suspended,
placed in its job object while it has executed nothing, and only then resumed:
between process creation and job assignment it could otherwise start a
descendant that escapes the job's kill-on-close and limits. The Windows-only
tests use real processes and read the job back from inside the child.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from cortex_backend.execution import code_execution
from cortex_backend.execution.code_execution import (
    MAX_CODE_MEMORY_BYTES,
    MAX_CODE_TIMEOUT_SECONDS,
    CodeExecutionError,
)
import job_probe

windows_only = pytest.mark.skipif(os.name != "nt", reason="uses Windows job objects")

_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
# Win32 JOB_OBJECT_LIMIT_* and JOB_OBJECT_UILIMIT_* values, written out here so
# the job is checked against the operating system's numbers and not the code's.
_LIMIT_PROCESS_TIME = 0x00000002
_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_PROCESS_MEMORY = 0x00000100
_LIMIT_JOB_MEMORY = 0x00000200
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_UILIMIT_ALL = 0x000000FF


@windows_only
def test_a_suspended_process_does_not_run_until_it_is_resumed(tmp_path: Path):
    marker = tmp_path / "ran"
    child = subprocess.Popen(
        [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"],
        env=code_execution._minimal_worker_environment(),
        creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # A process that has never run cannot finish, however long it is left.
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.75)
        assert not marker.exists()

        code_execution._resume_suspended_process(child)

        assert child.wait(timeout=60) == 0
        assert marker.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)


@windows_only
@pytest.mark.parametrize(
    "process", [SimpleNamespace(), SimpleNamespace(_handle=0)], ids=["no handle", "bad handle"]
)
def test_a_process_that_cannot_be_resumed_is_reported_as_isolation_unavailable(process: object):
    with pytest.raises(CodeExecutionError) as raised:
        code_execution._resume_suspended_process(process)

    assert raised.value.code == "process_isolation_unavailable"


class _Spy:
    """Records the order brokered-process start-up steps happen in."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[Any] = []
        self.children: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen
        real_job = code_execution._WindowsProcessJob
        real_resume = code_execution._resume_suspended_process
        spy = self

        def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            spy.events.append(("popen", kwargs.get("creationflags", 0)))
            child = real_popen(*args, **kwargs)
            spy.children.append(child)
            return child

        class Job(real_job):
            def __init__(self, process: Any, **kwargs: Any) -> None:
                super().__init__(process, **kwargs)
                spy.events.append("job")

        def resume(process: Any) -> None:
            spy.events.append("resume")
            real_resume(process)

        monkeypatch.setattr(code_execution.subprocess, "Popen", popen)
        monkeypatch.setattr(code_execution, "_WindowsProcessJob", Job)
        monkeypatch.setattr(code_execution, "_resume_suspended_process", resume)


@windows_only
def test_a_brokered_process_is_created_suspended_and_joins_its_job_before_it_is_resumed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    spy = _Spy(monkeypatch)

    result = code_execution._run_brokered_process(
        [sys.executable, "-c", "print('ran')"], workspace=tmp_path, timeout=60
    )

    assert [event[0] if isinstance(event, tuple) else event for event in spy.events] == [
        "popen",
        "job",
        "resume",
    ]
    assert spy.events[0][1] & _CREATE_SUSPENDED
    # It also starts: the minimal environment is enough for a real interpreter.
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "ran"


@windows_only
def test_a_brokered_process_that_cannot_join_its_job_is_ended_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    spy = _Spy(monkeypatch)
    marker = tmp_path / "ran"

    def refuse(process: Any, **kwargs: Any) -> None:
        raise CodeExecutionError("process_isolation_unavailable")

    monkeypatch.setattr(code_execution, "_WindowsProcessJob", refuse)

    with pytest.raises(CodeExecutionError) as raised:
        code_execution._run_brokered_process(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], workspace=tmp_path, timeout=60
        )

    assert raised.value.code == "process_isolation_unavailable"
    (child,) = spy.children
    # It was ended rather than left suspended (wait would time out), and it never ran.
    child.wait(timeout=30)
    assert not marker.exists(), "the child ran outside its job"


@windows_only
def test_a_brokered_process_that_cannot_be_resumed_is_ended_and_never_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    spy = _Spy(monkeypatch)
    marker = tmp_path / "ran"

    def cannot_resume(process: Any) -> None:
        raise CodeExecutionError("process_isolation_unavailable")

    monkeypatch.setattr(code_execution, "_resume_suspended_process", cannot_resume)

    with pytest.raises(CodeExecutionError) as raised:
        code_execution._run_brokered_process(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], workspace=tmp_path, timeout=60
        )

    assert raised.value.code == "process_isolation_unavailable"
    (child,) = spy.children
    # Closing the job ends it with exit code 0, so the exit status says nothing here:
    # what matters is that it was ended rather than left suspended, and never ran.
    child.wait(timeout=30)
    assert not marker.exists()


@windows_only
def test_a_brokered_process_reads_back_the_job_it_was_placed_in():
    """Run through the real broker: the child's first act is to ask which job it is in."""

    result = code_execution._run_brokered_process(
        [sys.executable, str(Path(job_probe.__file__))], workspace=Path.cwd(), timeout=60
    )

    assert result["returncode"] == 0
    report = json.loads(result["stdout"])
    assert report == {
        "in_job": True,
        "queried": True,
        "limit_flags": (
            _LIMIT_KILL_ON_JOB_CLOSE
            | _LIMIT_PROCESS_TIME
            | _LIMIT_ACTIVE_PROCESS
            | _LIMIT_PROCESS_MEMORY
            | _LIMIT_JOB_MEMORY
        ),
        "active_process_limit": 8,
        "per_process_user_time": int((MAX_CODE_TIMEOUT_SECONDS + 1.0) * 10_000_000),
        "process_memory_limit": MAX_CODE_MEMORY_BYTES,
        "job_memory_limit": MAX_CODE_MEMORY_BYTES,
        "ui_restrictions": _UILIMIT_ALL,
    }


def test_a_brokered_process_gets_the_system_root_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("CORTEX_SYNTHETIC_API_KEY", "not-a-real-credential")

    result = code_execution._run_brokered_process(
        [sys.executable, "-c", "import os; print(sorted(os.environ))"], workspace=tmp_path, timeout=60
    )

    assert result["returncode"] == 0
    names = ast.literal_eval(result["stdout"])  # the child printed a list of variable names
    assert [name.upper() for name in names] == (["SYSTEMROOT"] if os.name == "nt" else [])
