"""Contract tests for the non-production Phase 0 prerequisite probe."""

from __future__ import annotations

from tools.execution_spikes.phase0_probe import build_report


def test_phase0_probe_has_stable_gate_shape() -> None:
    report = build_report(
        run_job_smoke=False,
        run_ipc_smoke=False,
        run_wasi_smoke=False,
    )

    assert report["probe"] == "cortex-execution-phase0"
    assert report["schema_version"] == 1
    assert report["phase0_status"] in {"pass", "blocked"}
    assert isinstance(report["phase0_ready_for_phase1"], bool)
    assert {check["status"] for check in report["checks"]} <= {
        "pass",
        "blocked",
        "fail",
        "not_run",
    }


def test_phase0_probe_fails_closed_when_a_required_check_is_not_green() -> None:
    report = build_report(
        run_job_smoke=False,
        run_ipc_smoke=False,
        run_wasi_smoke=False,
    )
    required = {
        "environment",
        "appcontainer_api_surface",
        "appcontainer_process_isolation_smoke",
        "job_object_api_surface",
        "named_pipe_api_surface",
        "job_object_kill_on_close_smoke",
        "named_pipe_ipc_smoke",
        "wasmtime_guest_runtime",
        "wasmtime_runtime_controls",
        "guest_language_qualification",
        "containment_cancellation_corpus",
        "security_review",
        "pyinstaller_package_preconditions",
    }
    checks = [check for check in report["checks"] if check["name"] in required]
    if any(check["status"] != "pass" for check in checks):
        assert report["phase0_ready_for_phase1"] is False
        assert report["phase0_status"] == "blocked"


def test_phase0_does_not_close_from_smoke_only() -> None:
    report = build_report(
        run_job_smoke=False,
        run_ipc_smoke=False,
        run_wasi_smoke=False,
    )
    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert statuses["guest_language_qualification"] == "blocked"
    assert statuses["containment_cancellation_corpus"] == "blocked"
    assert report["phase0_ready_for_phase1"] is False
