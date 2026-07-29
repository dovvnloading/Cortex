"""Qualification-only coordinator probe remains explicit and fail-closed."""

from __future__ import annotations

from pathlib import Path

from tools.execution_spikes import recipe_coordinator_e2e_qualification as qualification


def test_coordinator_probe_blocks_without_native_windows(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qualification.os, "name", "posix")

    result = qualification.qualify(
        tmp_path / "recipe-runtime",
        timeout_seconds=10,
    )

    assert result == {
        "status": "blocked",
        "code": "native_windows_required",
        "stages": [],
        "cases": {},
    }


def test_coordinator_probe_reports_missing_package_without_fallback(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qualification.os, "name", "nt")

    result = qualification.qualify(
        tmp_path / "missing-recipe-runtime",
        timeout_seconds=10,
    )

    assert result["status"] == "blocked"
    assert result["code"] == "package_missing"
    assert result["stages"] == []
    assert result["cases"] == {}
