"""What the local workers are actually held to, checked against Windows itself.

``test_worker_containment.py`` pins the call sequence and the fail-closed paths
with fakes. These tests ask the operating system: a real child reports the job
object it landed in, a real allocation is refused, a real second process is
refused, and a spawned worker is shown not to inherit the parent's handles. The
Windows-only ones skip elsewhere; the environment, resource-limit and
entry-point-order checks run everywhere.
"""

from __future__ import annotations

from io import BytesIO
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from typing import Any

from PIL import Image
import pytest

from cortex_backend.core.win_jobs import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    JOB_OBJECT_LIMIT_PROCESS_TIME,
    JOB_OBJECT_UILIMIT_ALL,
    JobLimits,
    KillOnCloseJob,
)
from cortex_backend.execution import (
    code_execution,
    local_process,
    local_recipe_attempt,
)
from cortex_backend.execution.code_execution import (
    MAX_CODE_MEMORY_BYTES,
    MAX_CODE_TIMEOUT_SECONDS,
    CodeCapabilities,
)
from cortex_backend.execution.local_code_attempt import LocalCodeAttempt, code_worker_job_limits
from cortex_backend.execution.local_recipe_attempt import (
    RECIPE_WORKER_JOB_LIMITS,
    RECIPE_WORKER_MEMORY_BYTES,
    LocalRecipeWorkerAttempt,
)
from cortex_backend.execution.local_scratch_attempt import SCRATCH_WORKER_JOB_LIMITS, LocalScratchAttempt
from cortex_backend.execution.recipe_coordinator import RecipeExecutionError
from cortex_backend.execution.recipes import parse_image_transform
from support import wait_until
import job_probe
import worker_probes

windows_only = pytest.mark.skipif(os.name != "nt", reason="uses Windows job objects")

_MIB = 1024 * 1024


# -- The job a child really lands in -------------------------------------------


def _expected_report(limits: JobLimits) -> dict[str, int]:
    """What ``job_probe`` should read back for a job built from ``limits``."""

    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if limits.process_memory_bytes is not None:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
    if limits.job_memory_bytes is not None:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
    if limits.active_processes is not None:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    if limits.cpu_seconds is not None:
        flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
    return {
        "limit_flags": flags,
        "active_process_limit": limits.active_processes or 0,
        "per_process_user_time": int(limits.cpu_seconds * 10_000_000) if limits.cpu_seconds else 0,
        "process_memory_limit": limits.process_memory_bytes or 0,
        "job_memory_limit": limits.job_memory_bytes or 0,
        "ui_restrictions": JOB_OBJECT_UILIMIT_ALL if limits.restrict_ui else 0,
    }


def _run_in_job(limits: JobLimits, *arguments: str) -> str:
    """Run ``python <arguments>`` in a job built from ``limits`` and return its stdout.

    The child waits for one line on stdin before it does anything, so the job is
    attached first; that is the whole point of holding a worker at a checkpoint.
    """

    child = subprocess.Popen(
        [sys.executable, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    job = KillOnCloseJob(limits)
    try:
        job.assign(child.pid)
        output, _ = child.communicate("go\n", timeout=120)
        return output.strip()
    finally:
        job.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)


@windows_only
def test_a_real_job_carries_exactly_the_limits_it_was_given():
    limits = JobLimits(
        process_memory_bytes=300 * _MIB,
        job_memory_bytes=310 * _MIB,
        active_processes=3,
        cpu_seconds=7.5,
        restrict_ui=True,
    )

    report = json.loads(_run_in_job(limits, str(Path(job_probe.__file__))))

    assert report == {"in_job": True, "queried": True, **_expected_report(limits)}


@windows_only
def test_a_real_job_without_limits_is_only_kill_on_close():
    report = json.loads(_run_in_job(JobLimits(), str(Path(job_probe.__file__))))

    assert report == {"in_job": True, "queried": True, **_expected_report(JobLimits())}
    assert report["limit_flags"] == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


_ALLOCATE_200_MIB = (
    "import sys\n"
    "sys.stdin.readline()\n"
    "try:\n"
    "    block = bytearray(200 * 1024 * 1024)\n"
    "    print('allocated')\n"
    "except MemoryError:\n"
    "    print('refused')\n"
)


@windows_only
def test_a_real_job_refuses_memory_beyond_its_process_limit():
    assert _run_in_job(JobLimits(process_memory_bytes=100 * _MIB), "-c", _ALLOCATE_200_MIB) == "refused"
    # The same program with no limit succeeds, so it was the limit that refused it.
    assert _run_in_job(JobLimits(), "-c", _ALLOCATE_200_MIB) == "allocated"


@windows_only
def test_a_real_job_refuses_memory_beyond_its_job_wide_limit():
    assert _run_in_job(JobLimits(job_memory_bytes=100 * _MIB), "-c", _ALLOCATE_200_MIB) == "refused"


_START_A_SECOND_PROCESS = (
    "import subprocess, sys\n"
    "sys.stdin.readline()\n"
    "try:\n"
    "    outcome = subprocess.run([sys.executable, '-c', 'pass'], timeout=60).returncode\n"
    "    print('started', outcome)\n"
    "except OSError:\n"
    "    print('refused')\n"
)


@windows_only
def test_a_real_job_of_one_process_refuses_a_second():
    assert _run_in_job(JobLimits(active_processes=1), "-c", _START_A_SECOND_PROCESS) == "refused"
    # With room for a second process the same program starts one and it succeeds.
    assert _run_in_job(JobLimits(active_processes=2), "-c", _START_A_SECOND_PROCESS) == "started 0"


def _worker_limits() -> list[Any]:
    return [
        pytest.param(SCRATCH_WORKER_JOB_LIMITS, id="scratch"),
        pytest.param(RECIPE_WORKER_JOB_LIMITS, id="recipe"),
        pytest.param(code_worker_job_limits(3.0), id="code"),
    ]


@windows_only
@pytest.mark.parametrize("limits", _worker_limits())
def test_each_worker_is_put_in_a_job_with_the_limits_its_attempt_declares(limits: JobLimits):
    """The shared helper, real spawn and real job: the child reads back what it was given."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe()
    process = context.Process(target=job_probe.report_job, args=(sender,), daemon=True)
    process.start()
    sender.close()
    job = None
    try:
        assert receiver.poll(60), "the probe worker never said it was ready"
        assert receiver.recv() == {"ok": True, "event": "ready"}
        job = local_process._contain_worker(process, limits)
        assert local_process._release_worker(receiver) is True
        assert receiver.poll(60), "the probe worker never reported its job"
        message = receiver.recv()
    finally:
        if job is not None:
            job.close()
        local_process._stop_process(process)
        receiver.close()

    assert message == {"ok": True, "job": {"in_job": True, "queried": True, **_expected_report(limits)}}


# -- Real workers, under their real limits -------------------------------------


def _spying_job(record: list[JobLimits | None]) -> type[KillOnCloseJob]:
    """The real job class, recording the limits each instance was built with."""

    class SpyingJob(KillOnCloseJob):
        def __init__(self, limits: JobLimits | None = None) -> None:
            super().__init__(limits)
            record.append(limits)

    return SpyingJob


def test_the_scratch_worker_still_computes_under_its_real_limits(monkeypatch: pytest.MonkeyPatch):
    built: list[JobLimits | None] = []
    monkeypatch.setattr(local_process, "KillOnCloseJob", _spying_job(built))

    result = LocalScratchAttempt().evaluate("6 * 7", Event())

    assert result.value == "42"
    assert built == ([SCRATCH_WORKER_JOB_LIMITS] if os.name == "nt" else [])


def test_the_code_worker_still_runs_under_its_real_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    built: list[JobLimits | None] = []
    monkeypatch.setattr(local_process, "KillOnCloseJob", _spying_job(built))

    result = LocalCodeAttempt(timeout_seconds=10.0).evaluate(
        "_result = 6 * 7", CodeCapabilities(), str(tmp_path), Event()
    )

    assert result.value == 42
    assert built == ([code_worker_job_limits(10.0)] if os.name == "nt" else [])


def _png(size: tuple[int, int]) -> bytes:
    with Image.new("RGBA", size, (120, 80, 40, 255)) as image, BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def _contrast_plan(steps: int):
    return parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": "artifact-1",
            "steps": [{"op": "contrast", "factor": "1.2"}] * steps,
            "output_format": "png",
        }
    )


def test_the_image_worker_handles_a_large_image_within_its_real_ceiling(monkeypatch: pytest.MonkeyPatch):
    """A 16-megapixel RGBA image is 64 MiB decoded; three contrast steps hold three of them.

    Measured on a real worker this peaks near 200 MiB of committed memory,
    which the old 256 MiB figure would have left almost nothing to spare on and
    a 64-megapixel image (about 800 MiB) would have failed outright.
    """

    built: list[JobLimits | None] = []
    monkeypatch.setattr(local_process, "KillOnCloseJob", _spying_job(built))

    output = LocalRecipeWorkerAttempt(None).transform(
        "request", "job-1", _contrast_plan(3), _png((4096, 4096)), Event()
    )

    assert (output.format, output.width, output.height) == ("PNG", 4096, 4096)
    assert built == ([RECIPE_WORKER_JOB_LIMITS] if os.name == "nt" else [])


@windows_only
def test_a_decoder_that_needs_more_than_the_ceiling_is_stopped_by_the_operating_system(
    monkeypatch: pytest.MonkeyPatch,
):
    """The ceiling is enforced by Windows, not estimated: shrink it and the same image fails."""

    tight = JobLimits(
        process_memory_bytes=128 * _MIB,
        job_memory_bytes=128 * _MIB,
        active_processes=1,
        restrict_ui=True,
    )
    monkeypatch.setattr(local_recipe_attempt, "RECIPE_WORKER_JOB_LIMITS", tight)

    # Three 64 MiB images cannot fit in 128 MiB, whatever the interpreter already holds.
    with pytest.raises(RecipeExecutionError) as raised:
        LocalRecipeWorkerAttempt(None).transform(
            "request", "job-1", _contrast_plan(3), _png((4096, 4096)), Event()
        )

    assert raised.value.code in {"worker_failed", "worker_provider_failed"}


# -- The environment a worker is left with -------------------------------------


def _minimal_names() -> list[str]:
    return ["SYSTEMROOT"] if os.name == "nt" else []


def test_a_worker_child_starts_with_the_secret_and_leaves_the_scrub_without_it(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CORTEX_SYNTHETIC_API_KEY", "not-a-real-credential")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=worker_probes.report_scrubbed_environment,
        args=(sender, "CORTEX_SYNTHETIC_API_KEY"),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        assert receiver.poll(60), "the probe worker never reported"
        report = receiver.recv()
    finally:
        local_process._stop_process(process)
        receiver.close()

    # The credential really was inherited (so the scrub had something to remove),
    # and nothing but the Windows system root is left.
    assert report["inherited"] is True
    assert [name.upper() for name in report["remaining"]] == _minimal_names()


_SYNTHETIC_ENVIRONMENT = {
    "SystemRoot": "C:\\Windows",
    "OPENAI_API_KEY": "synthetic-not-a-key",
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "PATH": "C:\\somewhere",
}


@pytest.mark.parametrize(
    "scrub",
    [local_process.scrub_worker_environment, code_execution._scrub_worker_environment],
    ids=["shared helper", "code worker copy"],
)
def test_both_scrubs_keep_only_the_windows_system_root(monkeypatch: pytest.MonkeyPatch, scrub):
    environment = dict(_SYNTHETIC_ENVIRONMENT)
    monkeypatch.setattr(os, "environ", environment)

    scrub()

    assert environment == ({"SystemRoot": "C:\\Windows"} if os.name == "nt" else {})


def test_the_code_workers_copy_of_the_environment_helper_matches_the_shared_one(
    monkeypatch: pytest.MonkeyPatch,
):
    """``code_execution`` cannot import ``local_process`` (it must stay lean); this holds the copies together."""

    monkeypatch.setattr(os, "environ", dict(_SYNTHETIC_ENVIRONMENT))

    assert code_execution._minimal_worker_environment() == local_process.minimal_worker_environment()
    assert local_process.minimal_worker_environment() == (
        {"SystemRoot": "C:\\Windows"} if os.name == "nt" else {}
    )


class _FakeResourceModule:
    RLIMIT_AS = 9
    RLIMIT_CPU = 0

    def __init__(self, *, fail: type[Exception] | None = None) -> None:
        self.calls: list[tuple[int, tuple[int, int]]] = []
        self._fail = fail

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        if self._fail is not None:
            raise self._fail("refused")
        self.calls.append((which, limits))


def test_the_shared_limits_helper_sets_what_the_code_workers_copy_sets(monkeypatch: pytest.MonkeyPatch):
    cpu = int(MAX_CODE_TIMEOUT_SECONDS) + 1
    shared, copy = _FakeResourceModule(), _FakeResourceModule()

    monkeypatch.setitem(sys.modules, "resource", shared)
    local_process.apply_resource_limits(memory_bytes=MAX_CODE_MEMORY_BYTES, cpu_seconds=cpu)
    monkeypatch.setitem(sys.modules, "resource", copy)
    code_execution._apply_resource_limits()

    assert shared.calls == copy.calls == [
        (9, (MAX_CODE_MEMORY_BYTES, MAX_CODE_MEMORY_BYTES)),
        (0, (cpu, cpu + 1)),
    ]


def test_the_shared_limits_helper_leaves_the_cpu_limit_off_unless_asked(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeResourceModule()
    monkeypatch.setitem(sys.modules, "resource", fake)

    local_process.apply_resource_limits(memory_bytes=5 * _MIB)

    assert fake.calls == [(9, (5 * _MIB, 5 * _MIB))]


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_a_platform_that_refuses_the_limits_does_not_stop_the_worker(
    monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
):
    monkeypatch.setitem(sys.modules, "resource", _FakeResourceModule(fail=failure))

    local_process.apply_resource_limits(memory_bytes=5 * _MIB, cpu_seconds=3)
    code_execution._apply_resource_limits()


def test_a_platform_without_the_resource_module_is_left_to_its_job_object(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "resource", None)  # makes ``import resource`` raise ImportError

    local_process.apply_resource_limits(memory_bytes=5 * _MIB, cpu_seconds=3)
    code_execution._apply_resource_limits()


# -- The order a worker entry point does things in -----------------------------


class _RecordingConnection:
    """A worker pipe that logs, in order, everything the worker does to it."""

    def __init__(self, events: list[Any], go: object) -> None:
        self._events, self._go = events, go
        self.closed = False

    def send(self, message: dict[str, Any]) -> None:
        self._events.append(("send", message.get("event") or ("result" if message.get("ok") else "error")))

    def recv(self) -> object:
        self._events.append("recv")
        return self._go

    def close(self) -> None:
        self.closed = True


def _grayscale_plan_payload() -> dict[str, Any]:
    plan = parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": "artifact-1",
            "steps": [{"op": "grayscale"}],
            "output_format": "png",
        }
    )
    return plan.model_dump(mode="json")


def _run_recipe_worker_in_process(monkeypatch: pytest.MonkeyPatch, go: object) -> list[Any]:
    """Drive the recipe worker's entry point here, recording instead of applying its process-wide steps."""

    events: list[Any] = []
    monkeypatch.setattr(local_recipe_attempt, "scrub_worker_environment", lambda: events.append("scrub"))
    monkeypatch.setattr(
        local_recipe_attempt,
        "apply_resource_limits",
        lambda **kwargs: events.append(("limits", kwargs["memory_bytes"])),
    )
    # Pinning Pillow's plugin table is one-way and process-wide; it belongs to the worker child.
    monkeypatch.setattr(local_recipe_attempt, "pin_plugin_registry", lambda: events.append("pin"))
    real_parse = local_recipe_attempt.parse_image_transform

    def recording_parse(payload: Any) -> Any:
        events.append("parse")
        return real_parse(payload)

    monkeypatch.setattr(local_recipe_attempt, "parse_image_transform", recording_parse)
    connection = _RecordingConnection(events, go)

    local_recipe_attempt._recipe_worker_main(
        connection, SimpleNamespace(is_set=lambda: False), _grayscale_plan_payload(), _png((4, 3))
    )

    assert connection.closed is True
    return events


def test_the_recipe_worker_bootstraps_then_waits_before_it_reads_its_input(monkeypatch: pytest.MonkeyPatch):
    events = _run_recipe_worker_in_process(monkeypatch, {"go": True})

    assert events == [
        "scrub",
        ("limits", RECIPE_WORKER_MEMORY_BYTES),
        "pin",
        ("send", "ready"),
        "recv",
        "parse",
        ("send", "result"),
    ]


@pytest.mark.parametrize("denied", [{"go": False}, {}, None, "go"])
def test_the_recipe_worker_does_nothing_with_its_input_unless_it_is_released(
    monkeypatch: pytest.MonkeyPatch, denied: object
):
    events = _run_recipe_worker_in_process(monkeypatch, denied)

    assert events == ["scrub", ("limits", RECIPE_WORKER_MEMORY_BYTES), "pin", ("send", "ready"), "recv"]


# -- No inherited handles ------------------------------------------------------


def _every_writer_has_closed(read_fd: int) -> bool:
    import _winapi  # Windows only; the caller is a windows_only test.
    import msvcrt

    try:
        _winapi.PeekNamedPipe(msvcrt.get_osfhandle(read_fd), 0)
    except BrokenPipeError:
        return True
    return False


@windows_only
def test_a_worker_child_does_not_inherit_the_parents_handles():
    """A pipe write end the parent marked inheritable must not stay open in a worker.

    The child is started the way every attempt starts its worker (``spawn``,
    which passes ``bInheritHandles=False`` to ``CreateProcess``). If it had
    inherited the write end, the pipe would not report EOF after the parent
    closed its own copy, for as long as the child is alive.
    """

    attempt = LocalScratchAttempt()
    assert attempt._context.get_start_method() == "spawn"
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    parent, child_end = attempt._context.Pipe()
    process = attempt._context.Process(
        target=worker_probes.hold_until_told_to_finish, args=(child_end,), daemon=True
    )
    process.start()
    child_end.close()
    try:
        assert parent.poll(60), "the probe worker never came up"
        assert parent.recv() == "up"
        os.close(write_fd)
        write_fd = -1
        wait_until(
            lambda: _every_writer_has_closed(read_fd),
            describe="the pipe to report that every writer has closed",
        )
    finally:
        if write_fd != -1:
            os.close(write_fd)
        os.close(read_fd)
        try:
            parent.send("done")
        except OSError:
            pass
        parent.close()
        local_process._stop_process(process)
