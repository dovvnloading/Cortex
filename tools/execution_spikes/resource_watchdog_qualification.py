"""Deterministic resource/watchdog accounting qualification corpus.

This helper is a release-gate probe, not an execution fallback.  The pure cases
exercise the immutable Phase 2 budgets, monotonic watchdog, cumulative sample
validation, failure precedence, and missing-accounting behavior without running
model or user input.  On Windows it also invokes the existing fixed native
Job Object policy and kill-on-close process-tree corpus, but records only their
stable status/evidence categories.  It never authorizes a provider launch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "execution_spikes"))

from cortex_backend.execution.resource_accounting import (
    ResourceAccountingError,
    ResourceBudget,
    ResourceGovernor,
    ResourceSample,
)


PASS = "pass"
BLOCKED = "blocked"
FAIL = "fail"
CORPUS = "resource-watchdog.v1"
CASE_NAMES = (
    "budget_matrix",
    "wall_deadline",
    "idle_watchdog",
    "clock_regression",
    "sample_regression",
    "cpu_limit",
    "memory_limit",
    "input_limit",
    "output_limit",
    "console_limit",
    "observation_limit",
    "message_limit",
    "missing_memory_accounting",
    "native_job_accounting",
    "native_tree_reaping",
)


def _result(name: str, status: str, evidence: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "evidence": evidence}
    if details:
        payload["details"] = details
    return payload


def _expect_code(action: Callable[[], Any], expected: str) -> bool:
    try:
        action()
    except ResourceAccountingError as error:
        return error.code == expected
    except Exception:
        return False
    return False


def _constant_clock() -> Callable[[], float]:
    return lambda: 0.0


def _budget_matrix() -> dict[str, Any]:
    scratch = ResourceBudget.scratch_auto_v1()
    artifact = ResourceBudget.artifact_transform_v1()
    expected = {
        "scratch.auto.v1": {
            "wall_time_ms": 10_000,
            "cpu_time_ms": 5_000,
            "memory_bytes": 256 * 1024 * 1024,
            "max_messages": 256,
            "max_bytes_read": 0,
            "max_bytes_written": 0,
            "max_console_bytes": 1 * 1024 * 1024,
            "max_observation_bytes": 64 * 1024,
            "idle_timeout_ms": 10_000,
        },
        "artifact.transform.v1": {
            "wall_time_ms": 60_000,
            "cpu_time_ms": 30_000,
            "memory_bytes": 512 * 1024 * 1024,
            "max_messages": 16_384,
            "max_bytes_read": 100 * 1024 * 1024,
            "max_bytes_written": 128 * 1024 * 1024,
            "max_console_bytes": 1 * 1024 * 1024,
            "max_observation_bytes": 64 * 1024,
            "idle_timeout_ms": 60_000,
        },
    }
    actual = {
        budget.profile: {
            key: getattr(budget, key)
            for key in expected[budget.profile]
        }
        for budget in (scratch, artifact)
    }
    checks = {
        "profiles_match_adr": actual == expected,
        "hard_caps_immutable": artifact.with_watchdog(
            wall_time_ms=60_000,
            idle_timeout_ms=60_000,
            max_messages=16_384,
        )
        == artifact,
    }
    return _result(
        "resource_budget_matrix",
        PASS if all(checks.values()) else FAIL,
        "The two ADR execution profiles resolve to immutable bounded values.",
        checks=checks,
    )


def _watchdog_case(name: str, clock_values: list[float], expected: str, *, idle: int = 10) -> dict[str, Any]:
    values = iter(clock_values)

    def clock() -> float:
        return next(values)

    budget = ResourceBudget(
        profile="qualification.v1",
        wall_time_ms=100,
        cpu_time_ms=100,
        memory_bytes=1024,
        max_messages=8,
        max_bytes_read=8,
        max_bytes_written=8,
        idle_timeout_ms=idle,
    )
    governor = ResourceGovernor(budget, clock=clock)
    passed = _expect_code(
        lambda: governor.observe(
            ResourceSample(peak_memory_bytes=1),
            progress=False,
        ),
        expected,
    )
    return _result(
        name,
        PASS if passed else FAIL,
        "A fixed monotonic clock produced the expected fail-closed watchdog category.",
        expected_code=expected,
    )


def _sample_regression() -> dict[str, Any]:
    governor = ResourceGovernor(
        ResourceBudget(
            profile="qualification.v1",
            wall_time_ms=100,
            cpu_time_ms=100,
            memory_bytes=1024,
            max_messages=8,
            max_bytes_read=8,
            max_bytes_written=8,
        ),
        clock=_constant_clock(),
    )
    governor.observe(ResourceSample(peak_memory_bytes=4, messages=1))
    passed = _expect_code(
        lambda: governor.observe(ResourceSample(peak_memory_bytes=None, messages=1)),
        "accounting_invalid",
    )
    return _result(
        "sample_regression",
        PASS if passed else FAIL,
        "A missing or regressing cumulative memory sample is rejected after a value was observed.",
        expected_code="accounting_invalid",
    )


def _limit_case(name: str, budget: ResourceBudget, sample: ResourceSample, expected: str) -> dict[str, Any]:
    governor = ResourceGovernor(budget, clock=_constant_clock())
    passed = _expect_code(lambda: governor.observe(sample), expected)
    return _result(
        name,
        PASS if passed else FAIL,
        "Resource failure precedence is stable and reports only the safe category.",
        expected_code=expected,
    )


def _native_job_accounting() -> dict[str, Any]:
    try:
        from native_launcher_qualification import _probe_resource_policy

        result = _probe_resource_policy()
    except Exception as exc:
        return _result(
            "native_job_accounting",
            FAIL,
            "The fixed native Job Object accounting probe failed closed.",
            error_type=type(exc).__name__,
        )
    status = result.get("status")
    if status == PASS:
        return _result(
            "native_job_accounting",
            PASS,
            "A fixed suspended AppContainer child returned configured limits and actual Job Object accounting.",
        )
    if status == BLOCKED:
        return _result(
            "native_job_accounting",
            BLOCKED,
            "Windows Job Object accounting qualification is unavailable on this host.",
        )
    return _result(
        "native_job_accounting",
        FAIL,
        "The fixed native Job Object accounting probe did not pass every check.",
    )


def _native_tree_reaping() -> dict[str, Any]:
    try:
        from cancellation_corpus import run

        result = run()
    except Exception as exc:
        return _result(
            "native_tree_reaping",
            FAIL,
            "The fixed kill-on-close watchdog corpus failed closed.",
            error_type=type(exc).__name__,
        )
    status = result.get("status")
    if status == PASS:
        return _result(
            "native_tree_reaping",
            PASS,
            "A fixed AppContainer descendant tree was reaped by the kill-on-close watchdog path.",
        )
    if status == BLOCKED:
        return _result(
            "native_tree_reaping",
            BLOCKED,
            "The Windows kill-on-close watchdog corpus is unavailable on this host.",
        )
    return _result(
        "native_tree_reaping",
        FAIL,
        "The fixed kill-on-close watchdog corpus did not prove full-tree reaping.",
    )


def run_qualification() -> dict[str, Any]:
    """Return stable redacted evidence for the resource/watchdog corpus."""

    small_budget = ResourceBudget(
        profile="qualification.v1",
        wall_time_ms=100,
        cpu_time_ms=1,
        memory_bytes=8,
        max_messages=1,
        max_bytes_read=1,
        max_bytes_written=1,
        max_console_bytes=1,
        max_observation_bytes=1,
        idle_timeout_ms=100,
    )
    checks = [
        _budget_matrix(),
        _watchdog_case("wall_deadline", [0.0, 0.100001], "deadline_exceeded", idle=100),
        _watchdog_case("idle_watchdog", [0.0, 0.010001], "watchdog_stalled"),
        _watchdog_case("clock_regression", [1.0, 0.5], "watchdog_clock_invalid"),
        _sample_regression(),
        _limit_case(
            "cpu_limit",
            small_budget,
            ResourceSample(cpu_time_ms=2, peak_memory_bytes=1),
            "cpu_exhausted",
        ),
        _limit_case(
            "memory_limit",
            small_budget,
            ResourceSample(peak_memory_bytes=9),
            "memory_exhausted",
        ),
        _limit_case(
            "input_limit",
            small_budget,
            ResourceSample(peak_memory_bytes=1, bytes_read=2),
            "input_limit",
        ),
        _limit_case(
            "output_limit",
            small_budget,
            ResourceSample(peak_memory_bytes=1, bytes_written=2),
            "output_limit",
        ),
        _limit_case(
            "console_limit",
            small_budget,
            ResourceSample(peak_memory_bytes=1, console_bytes=2),
            "console_limit",
        ),
        _limit_case(
            "observation_limit",
            small_budget,
            ResourceSample(peak_memory_bytes=1, observation_bytes=2),
            "observation_limit",
        ),
        _limit_case(
            "message_limit",
            small_budget,
            ResourceSample(peak_memory_bytes=1, messages=2),
            "message_budget_exhausted",
        ),
    ]
    missing = ResourceGovernor(
        ResourceBudget(
            profile="qualification.v1",
            wall_time_ms=100,
            cpu_time_ms=100,
            memory_bytes=1024,
            max_messages=8,
            max_bytes_read=8,
            max_bytes_written=8,
        ),
        clock=_constant_clock(),
    )
    missing.observe(ResourceSample())
    checks.append(
        _result(
            "missing_memory_accounting",
            PASS
            if _expect_code(missing.finish, "accounting_unavailable")
            else FAIL,
            "A terminal result without peak-memory accounting remains fail-closed.",
            expected_code="accounting_unavailable",
        )
    )
    checks.extend((_native_job_accounting(), _native_tree_reaping()))

    statuses = [check["status"] for check in checks]
    if any(status == FAIL for status in statuses):
        status = FAIL
    elif any(status == BLOCKED for status in statuses):
        status = BLOCKED
    else:
        status = PASS
    digest = hashlib.sha256(
        (CORPUS + "\n" + "\n".join(CASE_NAMES)).encode("ascii")
    ).hexdigest()[:16]
    return {
        "name": "cortex-resource-watchdog-qualification",
        "probe": "cortex-resource-watchdog-qualification",
        "schema_version": 1,
        "corpus": CORPUS,
        "corpus_digest": digest,
        "checks": checks,
        "provider_launch_authorized": False,
        "status": status,
        "qualification_status": status,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit compact JSON only.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 unless every resource/watchdog case is green.",
    )
    args = parser.parse_args()
    report = run_qualification()
    if args.json:
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report["qualification_status"] != PASS:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
