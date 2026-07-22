"""Tests for the non-production recipe sandbox qualification gate."""

from __future__ import annotations

import json
import subprocess

import tools.execution_spikes.recipe_sandbox_qualification as qualification


def test_missing_signed_worker_blocks_without_authorizing_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(qualification, "EXPECTED_WORKER_ROOT", tmp_path / "missing")

    result = qualification._probe_signed_worker_precondition()

    assert result["status"] == "blocked"
    assert result["details"]["signature_verified"] is False


def test_present_unsigned_worker_still_blocks(tmp_path, monkeypatch):
    root = tmp_path / "recipe-runtime"
    root.mkdir()
    (root / "manifest.json").write_text("{}", encoding="ascii")
    (root / qualification.EXPECTED_WORKER_ENTRYPOINT).write_bytes(b"not-a-signed-worker")
    monkeypatch.setattr(qualification, "EXPECTED_WORKER_ROOT", root)

    result = qualification._probe_signed_worker_precondition()

    assert result["status"] == "blocked"
    assert result["details"]["signature_verified"] is False
    assert result["details"]["launch_refused"] is True


def test_fixed_helper_timeout_fails_closed(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("", encoding="ascii")

    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixed-helper", 1)

    result = qualification._run_fixed_helper(
        helper,
        "fixed_check",
        1,
        runner=timeout_runner,
    )

    assert result["name"] == "fixed_check"
    assert result["status"] == "fail"


def test_fixed_helper_rejects_unexpected_evidence(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("", encoding="ascii")

    def invalid_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["fixed-helper"],
            returncode=0,
            stdout=json.dumps({"name": "wrong-check"}),
            stderr="",
        )

    result = qualification._run_fixed_helper(
        helper,
        "fixed_check",
        1,
        runner=invalid_runner,
    )

    assert result["name"] == "fixed_check"
    assert result["status"] == "fail"


def test_report_remains_blocked_when_worker_gate_is_blocked(monkeypatch):
    monkeypatch.setattr(
        qualification,
        "_probe_os_controls",
        lambda: [
            {"name": "recipe_appcontainer_control", "status": "pass"},
            {"name": "recipe_cancellation_control", "status": "pass"},
        ],
    )
    monkeypatch.setattr(
        qualification,
        "_probe_provider_core",
        lambda: {"name": "recipe_decoder_corpus", "status": "pass"},
    )
    monkeypatch.setattr(
        qualification,
        "_probe_signed_worker_precondition",
        lambda: {"name": "recipe_signed_worker_provenance", "status": "blocked"},
    )

    report = qualification.build_report()

    assert report["qualification_status"] == "blocked"
    assert report["provider_launch_authorized"] is False
