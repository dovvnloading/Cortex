"""Deterministic logical resource/watchdog accounting tests."""

from __future__ import annotations

import math

import pytest

from cortex_backend.execution.resource_accounting import (
    ResourceAccountingError,
    ResourceBudget,
    ResourceGovernor,
    ResourceSample,
)


def _clock(values: list[float]):
    iterator = iter(values)
    return lambda: next(iterator)


def _budget(**overrides):
    values = {
        "profile": "test.v1",
        "wall_time_ms": 100,
        "cpu_time_ms": 100,
        "memory_bytes": 1024,
        "max_messages": 8,
        "max_bytes_read": 8,
        "max_bytes_written": 8,
        "idle_timeout_ms": 100,
    }
    values.update(overrides)
    return ResourceBudget(**values)


def test_adr_profiles_are_bounded_and_immutable():
    scratch = ResourceBudget.scratch_auto_v1()
    artifact = ResourceBudget.artifact_transform_v1()

    assert scratch.wall_time_ms == 10_000
    assert scratch.cpu_time_ms == 5_000
    assert scratch.memory_bytes == 256 * 1024 * 1024
    assert artifact.wall_time_ms == 60_000
    assert artifact.cpu_time_ms == 30_000
    assert artifact.memory_bytes == 512 * 1024 * 1024
    assert artifact.with_watchdog(wall_time_ms=60_000) == artifact
    with pytest.raises(ValueError):
        artifact.with_watchdog(wall_time_ms=600_001)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([0.0, 0.100001], "deadline_exceeded"),
        ([0.0, 0.010001], "watchdog_stalled"),
        ([1.0, 0.5], "watchdog_clock_invalid"),
    ],
)
def test_watchdog_categories_are_deterministic(values, expected):
    idle = 100 if expected == "deadline_exceeded" else 10
    governor = ResourceGovernor(_budget(idle_timeout_ms=idle), clock=_clock(values))
    with pytest.raises(ResourceAccountingError) as error:
        governor.observe(ResourceSample(peak_memory_bytes=1))
    assert error.value.code == expected


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (ResourceSample(cpu_time_ms=101, peak_memory_bytes=1), "cpu_exhausted"),
        (ResourceSample(peak_memory_bytes=1025), "memory_exhausted"),
        (ResourceSample(bytes_read=9, peak_memory_bytes=1), "input_limit"),
        (ResourceSample(bytes_written=9, peak_memory_bytes=1), "output_limit"),
        (ResourceSample(console_bytes=1_048_577, peak_memory_bytes=1), "console_limit"),
        (
            ResourceSample(observation_bytes=65_537, peak_memory_bytes=1),
            "observation_limit",
        ),
        (ResourceSample(messages=9, peak_memory_bytes=1), "message_budget_exhausted"),
    ],
)
def test_governor_enforces_stable_limit_precedence(sample, expected):
    governor = ResourceGovernor(_budget(), clock=lambda: 0.0)
    with pytest.raises(ResourceAccountingError) as error:
        governor.observe(sample)
    assert error.value.code == expected


def test_governor_rejects_cumulative_regression_and_missing_terminal_memory():
    governor = ResourceGovernor(_budget(), clock=lambda: 0.0)
    governor.observe(ResourceSample(peak_memory_bytes=4, messages=1))
    with pytest.raises(ResourceAccountingError) as regression:
        governor.observe(ResourceSample(peak_memory_bytes=None, messages=1))
    assert regression.value.code == "accounting_invalid"

    incomplete = ResourceGovernor(_budget(), clock=lambda: 0.0)
    incomplete.observe(ResourceSample())
    with pytest.raises(ResourceAccountingError) as missing:
        incomplete.finish()
    assert missing.value.code == "accounting_unavailable"


def test_governor_returns_redacted_cumulative_usage():
    governor = ResourceGovernor(_budget(), clock=lambda: 0.0)
    usage = governor.observe(
        ResourceSample(
            cpu_time_ms=4,
            peak_memory_bytes=128,
            bytes_read=3,
            bytes_written=2,
            messages=1,
        )
    )
    assert usage.accounting_complete
    assert usage.cpu_time_ms == 4
    assert usage.peak_memory_bytes == 128
    assert governor.finish() == usage
    assert not hasattr(usage, "path")
    assert math.isfinite(usage.wall_time_ms)


def test_the_store_and_the_governor_agree_on_what_a_profile_name_is(tmp_path):
    """Both layers validate profile names, so they must use one pattern.

    They used to hold separate copies that had drifted to different length
    limits (99 in the store, 63 here). A name between the two was storable but
    could not be given a budget, so a job could be admitted and then fail when
    its limits were built.
    """

    from cortex_backend.execution.models import PROFILE_NAME_PATTERN
    from cortex_backend.execution.repository import ExecutionRepository

    repository = ExecutionRepository(tmp_path / "execution.sqlite3", tmp_path / "artifacts")
    owner = repository.installation_principal_id
    too_long = "a" * 65

    assert PROFILE_NAME_PATTERN.fullmatch(too_long) is None

    with pytest.raises(ValueError):
        repository.create_job(
            job_id="job-profile-length",
            owner=owner,
            request_id="req-profile-length",
            profile=too_long,
            payload={},
        )
    with pytest.raises(ValueError):
        _budget(profile=too_long)
