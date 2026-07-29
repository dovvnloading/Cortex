"""Regression tests for the redacted resource/watchdog qualification report."""

from __future__ import annotations

import tools.execution_spikes.resource_watchdog_qualification as qualification


def test_resource_watchdog_report_is_reproducible_without_native_details(monkeypatch):
    green = lambda: {"name": "native", "status": "pass"}
    monkeypatch.setattr(qualification, "_native_job_accounting", green)
    monkeypatch.setattr(qualification, "_native_tree_reaping", green)

    first = qualification.run_qualification()
    second = qualification.run_qualification()

    assert first == second
    assert first["status"] == "pass"
    assert first["qualification_status"] == "pass"
    assert first["corpus"] == "resource-watchdog.v1"
    assert len(first["checks"]) == 15
    assert all("path" not in str(check) for check in first["checks"])


def test_resource_watchdog_report_never_authorizes_provider(monkeypatch):
    monkeypatch.setattr(
        qualification,
        "_native_job_accounting",
        lambda: {"name": "native", "status": "blocked"},
    )
    monkeypatch.setattr(
        qualification,
        "_native_tree_reaping",
        lambda: {"name": "native", "status": "blocked"},
    )

    report = qualification.run_qualification()

    assert report["provider_launch_authorized"] is False
    assert report["status"] == "blocked"
