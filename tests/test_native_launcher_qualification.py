"""Regression tests for the non-production suspended-launch qualification."""

from __future__ import annotations

import pytest

import tools.execution_spikes.appcontainer_smoke as native
import tools.execution_spikes.native_launcher_qualification as qualification


def test_non_windows_resource_probe_fails_closed(monkeypatch):
    monkeypatch.setattr(qualification.sys, "platform", "linux")

    result = qualification._probe_resource_policy()

    assert result["status"] == "blocked"


def test_launcher_report_never_authorizes_when_worker_or_broker_is_blocked(monkeypatch):
    monkeypatch.setattr(
        qualification,
        "_probe_resource_policy",
        lambda: {"name": "native_launcher_resource_policy", "status": "pass"},
    )
    monkeypatch.setattr(
        qualification,
        "_probe_worker_package",
        lambda: {"name": "native_launcher_worker_package", "status": "blocked"},
    )
    monkeypatch.setattr(
        qualification,
        "_probe_broker_binding",
        lambda: {"name": "native_launcher_broker_binding", "status": "blocked"},
    )

    report = qualification.build_report()

    assert report["qualification_status"] == "blocked"
    assert report["provider_launch_authorized"] is False


def test_resource_policy_requires_no_breakaway_flag():
    breakaway = (
        qualification._JOB_OBJECT_LIMIT_BREAKAWAY_OK
        | qualification._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
    )
    required = (
        qualification._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | qualification._JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | qualification._JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | qualification._JOB_OBJECT_LIMIT_JOB_MEMORY
        | qualification._JOB_OBJECT_LIMIT_PROCESS_TIME
        | qualification._JOB_OBJECT_LIMIT_JOB_TIME
    )

    assert required & breakaway == 0


def test_child_policy_limits_reject_unbounded_values_before_process_creation():
    with pytest.raises(ValueError):
        native._run_child(
            None,
            None,
            None,
            None,
            None,
            "fixed.exe",
            [],
            process_memory_limit_bytes=0,
        )

    with pytest.raises(ValueError):
        native._run_child(
            None,
            None,
            None,
            None,
            None,
            "fixed.exe",
            [],
            active_process_limit=65,
        )
