"""Kill-on-close containment for Cortex's own child processes.

A child that outlives Cortex -- Task Manager, a crash -- keeps its CPU, ports
and sandbox resources, and nothing on the machine reaps it. The fake-based
tests pin the exact Win32 call sequence and the fail-closed paths; the
Windows-only tests at the end use real processes and a real job object.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from cortex_backend.core import win_jobs
from cortex_backend.core.win_jobs import (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    PROCESS_SET_QUOTA,
    PROCESS_TERMINATE,
    JobObjectError,
    JobObjectExtendedLimitInformation,
    KillOnCloseJob,
)
from cortex_backend.execution import local_process
from cortex_backend.execution.local_recipe_attempt import LocalRecipeWorkerAttempt
from cortex_backend.execution.local_scratch_attempt import LocalScratchAttempt
from cortex_backend.execution.recipe_coordinator import RecipeExecutionError
from cortex_backend.execution.scratch_compute import ScratchComputeError
from cortex_backend.launcher.desktop import process_is_alive
from support import wait_until

windows_only = pytest.mark.skipif(os.name != "nt", reason="uses Windows job objects")


class _FakeWin32:
    """Records the kernel32 call sequence without touching real Windows APIs."""

    def __init__(
        self,
        *,
        create: int = 1,
        configure: bool = True,
        open_process: int = 1,
        assign: bool = True,
        close: bool = True,
    ) -> None:
        self.create, self.configure = create, configure
        self.open_process, self.assign, self.close = open_process, assign, close
        self.calls: list[tuple] = []

    def CreateJobObjectW(self, security_attributes, name):
        self.calls.append(("CreateJobObjectW",))
        return self.create

    def SetInformationJobObject(self, job, info_class, info, info_size):
        limits = ctypes.cast(info, ctypes.POINTER(JobObjectExtendedLimitInformation)).contents
        self.calls.append(("SetInformationJobObject", job, limits.basic_limit_information.limit_flags))
        return 1 if self.configure else 0

    def OpenProcess(self, access, inherit_handle, pid):
        self.calls.append(("OpenProcess", access, inherit_handle, pid))
        return self.open_process

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("AssignProcessToJobObject", job, process))
        return 1 if self.assign else 0

    def CloseHandle(self, handle):
        self.calls.append(("CloseHandle", handle))
        return 1 if self.close else 0


def test_job_is_created_kill_on_close_and_assigns_by_pid_then_releases_the_process_handle():
    win32 = _FakeWin32(create=11, open_process=22)

    job = KillOnCloseJob(win32_factory=lambda: win32)
    job.assign(4242)

    assert win32.calls == [
        ("CreateJobObjectW",),
        ("SetInformationJobObject", 11, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
        ("OpenProcess", PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, 4242),
        ("AssignProcessToJobObject", 11, 22),
        ("CloseHandle", 22),
    ]
    assert job.closed is False


def test_closing_the_job_closes_its_handle_once_and_refuses_later_assignment():
    win32 = _FakeWin32(create=11)
    job = KillOnCloseJob(win32_factory=lambda: win32)

    job.close()
    job.close()

    assert win32.calls.count(("CloseHandle", 11)) == 1
    assert job.closed is True
    with pytest.raises(JobObjectError, match="closed"):
        job.assign(1)


def test_a_job_that_cannot_be_created_fails_closed():
    with pytest.raises(JobObjectError, match="create"):
        KillOnCloseJob(win32_factory=lambda: _FakeWin32(create=0))


def test_a_job_that_cannot_be_configured_is_closed_rather_than_leaked():
    win32 = _FakeWin32(create=11, configure=False)

    with pytest.raises(JobObjectError, match="configure"):
        KillOnCloseJob(win32_factory=lambda: win32)

    assert ("CloseHandle", 11) in win32.calls
    assert not any(call[0] == "AssignProcessToJobObject" for call in win32.calls)


@pytest.mark.parametrize(
    ("failure", "message"),
    [({"open_process": 0}, "open the process"), ({"assign": False}, "assign the process")],
)
def test_a_process_that_cannot_be_contained_is_reported_and_its_handle_released(
    failure: dict, message: str
):
    win32 = _FakeWin32(**{"create": 11, "open_process": 22, **failure})
    job = KillOnCloseJob(win32_factory=lambda: win32)

    with pytest.raises(JobObjectError, match=message):
        job.assign(7)

    if "open_process" not in failure:
        assert ("CloseHandle", 22) in win32.calls, "the process handle was leaked"
    assert job.closed is False


def test_a_missing_win32_layer_is_a_containment_error_not_a_crash():
    def unavailable():
        raise AttributeError("module 'ctypes' has no attribute 'WinDLL'")

    with pytest.raises(JobObjectError, match="initialize"):
        KillOnCloseJob(win32_factory=unavailable)


def _recording_job(*, fail_assign: bool = False):
    """A stand-in for ``KillOnCloseJob`` that records what the workers do with it."""

    class RecordingJob:
        created = 0
        assigned: list[int] = []
        closed = 0

        def __init__(self) -> None:
            type(self).created += 1

        def assign(self, pid: int) -> None:
            type(self).assigned.append(pid)
            if fail_assign:
                raise JobObjectError("could not assign the process to containment")

        def close(self) -> None:
            type(self).closed += 1

    return RecordingJob


@windows_only
def test_scratch_worker_is_contained_and_its_job_closed_when_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
):
    job = _recording_job()
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)

    result = LocalScratchAttempt().evaluate("1 + 1", Event())

    assert result.value is not None
    assert job.created == 1
    assert len(job.assigned) == 1 and job.assigned[0] > 0
    assert job.closed == 1


@windows_only
def test_scratch_worker_that_cannot_be_contained_fails_closed_and_is_not_left_running(
    monkeypatch: pytest.MonkeyPatch,
):
    job = _recording_job(fail_assign=True)
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)

    with pytest.raises(ScratchComputeError) as raised:
        LocalScratchAttempt().evaluate("1 + 1", Event())

    assert raised.value.code == "process_isolation_unavailable"
    assert job.closed == 1, "the job the worker was refused by was not closed"
    (pid,) = job.assigned
    wait_until(lambda: not process_is_alive(pid), describe="the uncontained worker to be stopped")


@windows_only
def test_recipe_worker_is_contained_and_its_job_closed_when_it_finishes(
    monkeypatch: pytest.MonkeyPatch,
):
    job = _recording_job()
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)
    plan = SimpleNamespace(model_dump=lambda mode: {"not": "a plan"})

    # The plan is deliberately invalid: the worker starts, rejects it and
    # answers, which is all containment needs.
    with pytest.raises(RecipeExecutionError):
        LocalRecipeWorkerAttempt(None).transform("request", "job-1", plan, b"x", Event())

    assert job.created == 1
    assert len(job.assigned) == 1 and job.assigned[0] > 0
    assert job.closed == 1


@windows_only
def test_recipe_worker_that_cannot_be_contained_fails_closed_and_is_not_left_running(
    monkeypatch: pytest.MonkeyPatch,
):
    job = _recording_job(fail_assign=True)
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)
    plan = SimpleNamespace(model_dump=lambda mode: {"not": "a plan"})

    with pytest.raises(RecipeExecutionError) as raised:
        LocalRecipeWorkerAttempt(None).transform("request", "job-1", plan, b"x", Event())

    assert raised.value.code == "process_isolation_unavailable"
    assert job.closed == 1
    (pid,) = job.assigned
    wait_until(lambda: not process_is_alive(pid), describe="the uncontained worker to be stopped")


def test_containing_a_worker_is_a_no_op_off_windows(monkeypatch: pytest.MonkeyPatch):
    job = _recording_job()
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)
    monkeypatch.setattr(local_process.os, "name", "posix")

    assert local_process._contain_worker(SimpleNamespace(pid=1)) is None
    assert job.created == 0


@windows_only
def test_closing_a_real_job_ends_the_process_assigned_to_it():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = KillOnCloseJob()
        job.assign(child.pid)
        assert child.poll() is None, "assignment alone must not end the process"

        job.close()

        child.wait(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@windows_only
def test_a_real_job_reports_a_process_that_no_longer_exists():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=20)
    job = KillOnCloseJob()
    try:
        # The Popen still holds the exited process's handle, so the pid opens;
        # a process that has exited cannot be assigned to a job.
        with pytest.raises(JobObjectError):
            job.assign(child.pid)
    finally:
        job.close()


def test_the_shared_definitions_are_the_ones_the_llama_launcher_uses():
    from cortex_backend.llamacpp import server_manager

    assert server_manager._JobObjectExtendedLimitInformation is win_jobs.JobObjectExtendedLimitInformation
    assert server_manager._real_job_win32 is win_jobs.real_job_win32
