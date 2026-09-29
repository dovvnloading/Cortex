"""Kill-on-close containment for Cortex's own child processes.

A child that outlives Cortex -- Task Manager, a crash -- keeps its CPU, ports
and sandbox resources, and nothing on the machine reaps it. The fake-based
tests pin the exact Win32 call sequence and the fail-closed paths; the
Windows-only tests at the end use real processes and a real job object.
"""

from __future__ import annotations

import ctypes
from functools import partial
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

from PIL import Image
import pytest

from cortex_backend.core import win_jobs
from cortex_backend.core.win_jobs import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_LIMIT_PROCESS_TIME,
    JOB_OBJECT_UILIMIT_ALL,
    JOBOBJECT_BASIC_UI_RESTRICTIONS_CLASS,
    PROCESS_SET_QUOTA,
    PROCESS_TERMINATE,
    JobBasicUiRestrictions,
    JobLimits,
    JobObjectError,
    JobObjectExtendedLimitInformation,
    KillOnCloseJob,
)
from cortex_backend.execution import (
    local_code_attempt,
    local_process,
    local_recipe_attempt,
    local_scratch_attempt,
)
from cortex_backend.execution.code_execution import (
    MAX_CODE_MEMORY_BYTES,
    CodeCapabilities,
    CodeExecutionError,
)
from cortex_backend.execution.local_code_attempt import LocalCodeAttempt, code_worker_job_limits
from cortex_backend.execution.local_recipe_attempt import (
    RECIPE_WORKER_JOB_LIMITS,
    RECIPE_WORKER_MEMORY_BYTES,
    LocalRecipeWorkerAttempt,
)
from cortex_backend.execution.local_scratch_attempt import (
    SCRATCH_WORKER_JOB_LIMITS,
    LocalScratchAttempt,
)
from cortex_backend.execution.recipe_coordinator import RecipeExecutionError
from cortex_backend.execution.recipe_provider import MAX_DECODED_BYTES, MAX_DIMENSION, MAX_PIXELS
from cortex_backend.execution.recipes import parse_image_transform
from cortex_backend.execution.scratch_compute import SCRATCH_WORKER_MEMORY_BYTES, ScratchComputeError
from cortex_backend.launcher.desktop import process_is_alive
from support import wait_until
import worker_probes

windows_only = pytest.mark.skipif(os.name != "nt", reason="uses Windows job objects")


class _FakeWin32:
    """Records the kernel32 call sequence without touching real Windows APIs."""

    def __init__(
        self,
        *,
        create: int = 1,
        configure: bool = True,
        configure_ui: bool = True,
        open_process: int = 1,
        assign: bool = True,
        close: bool = True,
    ) -> None:
        self.create, self.configure, self.configure_ui = create, configure, configure_ui
        self.open_process, self.assign, self.close = open_process, assign, close
        self.calls: list[tuple] = []
        # What the last extended-limit block asked for, copied out because the
        # block is a temporary on the caller's side.
        self.block: dict[str, int] = {}

    def CreateJobObjectW(self, security_attributes, name):
        self.calls.append(("CreateJobObjectW",))
        return self.create

    def SetInformationJobObject(self, job, info_class, info, info_size):
        if info_class == JOBOBJECT_BASIC_UI_RESTRICTIONS_CLASS:
            restrictions = ctypes.cast(info, ctypes.POINTER(JobBasicUiRestrictions)).contents
            assert info_size == ctypes.sizeof(JobBasicUiRestrictions)
            self.calls.append(("SetUiRestrictions", job, restrictions.ui_restrictions_class))
            return 1 if self.configure_ui else 0
        limits = ctypes.cast(info, ctypes.POINTER(JobObjectExtendedLimitInformation)).contents
        self.calls.append(("SetInformationJobObject", job, limits.basic_limit_information.limit_flags))
        self.block = {
            "process_memory": limits.process_memory_limit,
            "job_memory": limits.job_memory_limit,
            "active_processes": limits.basic_limit_information.active_process_limit,
            "cpu_100ns": limits.basic_limit_information.per_process_user_time,
        }
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


def test_a_job_without_limits_asks_for_kill_on_close_and_nothing_else():
    win32 = _FakeWin32(create=11)

    KillOnCloseJob(JobLimits(), win32_factory=lambda: win32)

    assert win32.calls == [
        ("CreateJobObjectW",),
        ("SetInformationJobObject", 11, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
    ]
    assert win32.block == {"process_memory": 0, "job_memory": 0, "active_processes": 0, "cpu_100ns": 0}


def test_limits_reach_the_extended_limit_block_with_their_flags():
    win32 = _FakeWin32(create=11)
    limits = JobLimits(process_memory_bytes=300, job_memory_bytes=400, active_processes=2, cpu_seconds=2.5)

    KillOnCloseJob(limits, win32_factory=lambda: win32)

    expected_flags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_TIME
    )
    # No user-interface call unless it was asked for.
    assert win32.calls == [
        ("CreateJobObjectW",),
        ("SetInformationJobObject", 11, expected_flags),
    ]
    # CPU time is in 100-nanosecond units.
    assert win32.block == {
        "process_memory": 300,
        "job_memory": 400,
        "active_processes": 2,
        "cpu_100ns": 25_000_000,
    }


def test_ui_restrictions_are_a_second_call_that_restricts_everything():
    win32 = _FakeWin32(create=11)

    KillOnCloseJob(JobLimits(restrict_ui=True), win32_factory=lambda: win32)

    assert win32.calls == [
        ("CreateJobObjectW",),
        ("SetInformationJobObject", 11, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
        ("SetUiRestrictions", 11, JOB_OBJECT_UILIMIT_ALL),
    ]


def test_a_job_whose_ui_restrictions_cannot_be_applied_is_closed_rather_than_leaked():
    win32 = _FakeWin32(create=11, configure_ui=False)

    with pytest.raises(JobObjectError, match="configure"):
        KillOnCloseJob(JobLimits(restrict_ui=True), win32_factory=lambda: win32)

    assert ("CloseHandle", 11) in win32.calls
    assert not any(call[0] == "AssignProcessToJobObject" for call in win32.calls)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"process_memory_bytes": 0},
        {"job_memory_bytes": -1},
        {"active_processes": 0},
        {"active_processes": True},
        {"process_memory_bytes": 1.5},
        {"cpu_seconds": 0},
        {"cpu_seconds": True},
        {"cpu_seconds": float("nan")},
        {"cpu_seconds": float("inf")},
        {"cpu_seconds": "5"},
    ],
)
def test_impossible_limits_are_rejected_before_a_job_is_built(kwargs: dict):
    with pytest.raises(ValueError):
        JobLimits(**kwargs)


def _recording_job(*, fail_assign: bool = False):
    """A stand-in for ``KillOnCloseJob`` that records what the workers do with it."""

    class RecordingJob:
        created = 0
        limits: list[JobLimits | None] = []
        assigned: list[int] = []
        closed = 0

        def __init__(self, limits: JobLimits | None = None) -> None:
            type(self).created += 1
            type(self).limits.append(limits)

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
    assert job.limits == [SCRATCH_WORKER_JOB_LIMITS]


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
    assert job.limits == [RECIPE_WORKER_JOB_LIMITS]


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


@windows_only
def test_code_worker_is_contained_under_its_declared_limits_and_its_job_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    job = _recording_job()
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)

    result = LocalCodeAttempt(timeout_seconds=5.0).evaluate(
        "_result = 6 * 7", CodeCapabilities(), str(tmp_path), Event()
    )

    assert result.value == 42
    assert job.created == 1
    assert len(job.assigned) == 1 and job.assigned[0] > 0
    assert job.closed == 1
    assert job.limits == [code_worker_job_limits(5.0)]


@windows_only
def test_code_worker_that_cannot_be_contained_fails_closed_and_is_not_left_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    job = _recording_job(fail_assign=True)
    monkeypatch.setattr(local_process, "KillOnCloseJob", job)

    with pytest.raises(CodeExecutionError) as raised:
        LocalCodeAttempt().evaluate("_result = 1", CodeCapabilities(), str(tmp_path), Event())

    assert raised.value.code == "process_isolation_unavailable"
    assert job.closed == 1
    (pid,) = job.assigned
    wait_until(lambda: not process_is_alive(pid), describe="the uncontained worker to be stopped")


def test_the_code_worker_keeps_the_limits_it_had_before_the_jobs_were_shared():
    limits = code_worker_job_limits(3.0)

    assert limits.process_memory_bytes == MAX_CODE_MEMORY_BYTES
    assert limits.active_processes == 4
    assert limits.cpu_seconds == 4.0


# Measured on the real worker under a real job, in MiB (the comment on
# RECIPE_WORKER_MEMORY_BYTES says how): the largest peak of any kind, a lossless
# WebP encode of full-entropy 64-megapixel noise that the provider then refuses
# as over its output limit, and the largest peak of a request that succeeds.
_MEASURED_WORST_PEAK_MIB = 2942
_MEASURED_WORST_SUCCESS_MIB = 2308


def test_the_image_worker_memory_ceiling_covers_the_provider_at_its_own_limits():
    """The ceiling comes from measurement, and keeps a margin over the worst of it.

    The first ceiling was three decoded images plus the encoded input and
    output, a figure that contrast alone justified (about 800 MiB at 64
    megapixels). Resize and lossless WebP output need far more, so an accepted
    64-megapixel request failed under the job. Real-job tests run those cases;
    this one holds the constant to the numbers it was sized from.
    """

    mib = 1024 * 1024
    rgba = 4
    image = MAX_PIXELS * rgba
    # A LANCZOS resize of RGBA holds its source, a premultiplied copy, a
    # dst_w x src_h buffer of up to MAX_DIMENSION squared, and the result.
    resize_working_set = 2 * image + MAX_DIMENSION * MAX_DIMENSION * rgba + image

    assert image == MAX_DECODED_BYTES
    assert RECIPE_WORKER_MEMORY_BYTES > 1.25 * resize_working_set
    assert RECIPE_WORKER_MEMORY_BYTES >= 1.25 * _MEASURED_WORST_PEAK_MIB * mib
    assert RECIPE_WORKER_MEMORY_BYTES >= 1.5 * _MEASURED_WORST_SUCCESS_MIB * mib
    assert RECIPE_WORKER_MEMORY_BYTES > MAX_CODE_MEMORY_BYTES * 4
    # Both the process and the job are held to it, and the worker gets one process.
    assert RECIPE_WORKER_JOB_LIMITS.process_memory_bytes == RECIPE_WORKER_MEMORY_BYTES
    assert RECIPE_WORKER_JOB_LIMITS.job_memory_bytes == RECIPE_WORKER_MEMORY_BYTES
    assert RECIPE_WORKER_JOB_LIMITS.active_processes == 1
    assert RECIPE_WORKER_JOB_LIMITS.restrict_ui is True


def test_the_scratch_worker_is_held_to_one_process_and_a_bounded_amount_of_memory():
    assert SCRATCH_WORKER_JOB_LIMITS.process_memory_bytes == SCRATCH_WORKER_MEMORY_BYTES
    assert SCRATCH_WORKER_JOB_LIMITS.job_memory_bytes == SCRATCH_WORKER_MEMORY_BYTES
    assert SCRATCH_WORKER_JOB_LIMITS.active_processes == 1
    assert SCRATCH_WORKER_JOB_LIMITS.restrict_ui is True


def _plan(marker: Path):
    """A stand-in plan that hands the worker probe the path to write."""

    return SimpleNamespace(model_dump=lambda mode: {"marker": str(marker)})


@windows_only
def test_scratch_input_is_not_touched_until_the_worker_is_contained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        local_scratch_attempt, "scratch_worker_main", worker_probes.scratch_marks_the_moment_it_is_released
    )
    monkeypatch.setattr(local_process, "KillOnCloseJob", _recording_job(fail_assign=True))
    refused = tmp_path / "refused"

    with pytest.raises(ScratchComputeError) as raised:
        LocalScratchAttempt().evaluate(str(refused), Event())

    assert raised.value.code == "process_isolation_unavailable"
    # The worker was waiting for its go-ahead when it was stopped, so it never ran.
    assert not refused.exists()

    monkeypatch.setattr(local_process, "KillOnCloseJob", _recording_job())
    allowed = tmp_path / "allowed"
    LocalScratchAttempt().evaluate(str(allowed), Event())
    assert allowed.read_text(encoding="utf-8") == "released"


@windows_only
def test_recipe_input_is_not_touched_until_the_worker_is_contained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        local_recipe_attempt, "_recipe_worker_main", worker_probes.recipe_marks_the_moment_it_is_released
    )
    monkeypatch.setattr(local_process, "KillOnCloseJob", _recording_job(fail_assign=True))
    refused = tmp_path / "refused"

    with pytest.raises(RecipeExecutionError) as raised:
        LocalRecipeWorkerAttempt(None).transform("request", "job-1", _plan(refused), b"x", Event())

    assert raised.value.code == "process_isolation_unavailable"
    assert not refused.exists()

    monkeypatch.setattr(local_process, "KillOnCloseJob", _recording_job())
    allowed = tmp_path / "allowed"
    with pytest.raises(RecipeExecutionError) as answered:
        LocalRecipeWorkerAttempt(None).transform("request", "job-1", _plan(allowed), b"x", Event())
    # The probe writes its marker and then closes the pipe without a result.
    assert answered.value.code == "worker_failed"
    assert allowed.read_text(encoding="utf-8") == "released"


def test_a_scratch_result_from_a_worker_that_was_never_released_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        local_scratch_attempt, "scratch_worker_main", worker_probes.scratch_answers_without_being_released
    )

    with pytest.raises(ScratchComputeError) as raised:
        LocalScratchAttempt().evaluate("1 + 1", Event())

    assert raised.value.code == "worker_output_invalid"


def test_a_recipe_result_from_a_worker_that_was_never_released_is_refused(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        local_recipe_attempt, "_recipe_worker_main", worker_probes.recipe_answers_without_being_released
    )

    with pytest.raises(RecipeExecutionError) as raised:
        LocalRecipeWorkerAttempt(None).transform("request", "job-1", _plan(Path("unused")), b"x", Event())

    assert raised.value.code == "worker_output_invalid"


def test_a_code_result_from_a_worker_that_was_never_released_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        local_code_attempt, "code_worker_main", worker_probes.code_answers_without_being_released
    )

    with pytest.raises(CodeExecutionError) as raised:
        LocalCodeAttempt().evaluate("_result = 1", CodeCapabilities(), str(tmp_path), Event())

    assert raised.value.code == "worker_output_invalid"


def test_a_scratch_worker_that_never_starts_is_a_startup_timeout(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(local_scratch_attempt, "scratch_worker_main", worker_probes.scratch_never_answers)

    with pytest.raises(ScratchComputeError) as raised:
        LocalScratchAttempt(startup_timeout_seconds=0.5).evaluate("1 + 1", Event())

    assert raised.value.code == "worker_startup_timeout"


def test_a_recipe_worker_that_never_starts_is_a_startup_timeout_not_a_worker_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(local_recipe_attempt, "_recipe_worker_main", worker_probes.recipe_never_answers)

    with pytest.raises(RecipeExecutionError) as raised:
        LocalRecipeWorkerAttempt(None, startup_timeout_seconds=0.5).transform(
            "request", "job-1", _plan(Path("unused")), b"x", Event()
        )

    assert raised.value.code == "worker_startup_timeout"


def _png(size: tuple[int, int] = (8, 6)) -> bytes:
    with Image.new("RGBA", size, (120, 80, 40, 255)) as image, BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def _grayscale_plan():
    return parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": "artifact-1",
            "steps": [{"op": "grayscale"}],
            "output_format": "png",
        }
    )


def test_a_slow_cold_start_does_not_spend_the_recipe_transform_budget(monkeypatch: pytest.MonkeyPatch):
    """The transform clock starts when the worker is released, not when it is spawned.

    The worker sleeps 1.5 s before it says ready, longer than the whole 1.0 s
    transform budget, and still returns its result: the wait is startup time.
    """

    monkeypatch.setattr(
        local_recipe_attempt,
        "_recipe_worker_main",
        partial(worker_probes.recipe_starts_late, delay=1.5),
    )

    output = LocalRecipeWorkerAttempt(None, timeout_seconds=1.0, startup_timeout_seconds=60.0).transform(
        "request", "job-1", _grayscale_plan(), _png(), Event()
    )

    assert output.format == "PNG"
    assert (output.width, output.height) == (8, 6)


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
