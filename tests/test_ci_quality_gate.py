"""Exercise positive and negative final Quality states without running Actions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ci_quality_gate import validate_quality_gate


CODE_GATES = ("LINT_RESULT", "BACKEND_RESULT", "FRONTEND_RESULT", "PACKAGE_RESULT")


def values(*, event: str = "pull_request", code: str = "true", browser: str = "true", opt_in: str = "false") -> dict[str, str]:
    normal = "success" if code == "true" else "skipped"
    browser_needed = code == "true" and (
        event == "push" or (event == "pull_request" and browser == "true") or (event == "workflow_dispatch" and opt_in == "true")
    )
    return {
        "CHANGES_RESULT": "success",
        "CODE_CHANGED": code,
        "EVENT_NAME": event,
        "BROWSER_CHANGED": browser,
        "BROWSER_OPT_IN": opt_in,
        "DEPENDENCY_REVIEW_RESULT": "success" if event == "pull_request" else "skipped",
        "BROWSER_RESULT": "success" if browser_needed else "skipped",
        **{name: normal for name in CODE_GATES},
    }


@pytest.mark.parametrize(
    "event,code,browser,opt_in",
    [
        ("pull_request", "true", "true", "false"),
        ("pull_request", "true", "false", "false"),
        ("pull_request", "false", "false", "false"),
        ("push", "true", "true", "false"),
        ("workflow_dispatch", "true", "true", "true"),
        ("workflow_dispatch", "true", "true", "false"),
    ],
)
def test_valid_states_pass(event: str, code: str, browser: str, opt_in: str) -> None:
    assert validate_quality_gate(values(event=event, code=code, browser=browser, opt_in=opt_in)) == []


@pytest.mark.parametrize("bad_value", ["failure", "cancelled", "skipped", "", "pending"])
def test_classification_failure_or_missing_output_never_passes(bad_value: str) -> None:
    baseline = values()
    baseline["CHANGES_RESULT"] = bad_value
    assert validate_quality_gate(baseline)
    baseline = values()
    baseline["CODE_CHANGED"] = bad_value
    assert validate_quality_gate(baseline)


@pytest.mark.parametrize("name", CODE_GATES + ("BROWSER_RESULT", "DEPENDENCY_REVIEW_RESULT"))
@pytest.mark.parametrize("bad_value", ["failure", "cancelled", "skipped", ""])
def test_failed_missing_or_skipped_required_gate_is_red(name: str, bad_value: str) -> None:
    baseline = values()
    baseline[name] = bad_value
    assert any(name in error for error in validate_quality_gate(baseline))


def test_docs_only_requires_all_code_jobs_to_be_skipped() -> None:
    baseline = values(code="false", browser="false")
    assert not validate_quality_gate(baseline)
    baseline["BACKEND_RESULT"] = "success"
    assert validate_quality_gate(baseline)


def test_nonimpacting_pr_skips_browser_but_required_browser_cannot_skip() -> None:
    assert not validate_quality_gate(values(browser="false"))
    baseline = values(browser="true")
    baseline["BROWSER_RESULT"] = "skipped"
    assert validate_quality_gate(baseline)


def test_unexpected_browser_execution_and_unexpected_dependency_review_fail() -> None:
    baseline = values(event="push")
    baseline["DEPENDENCY_REVIEW_RESULT"] = "success"
    assert validate_quality_gate(baseline)
    baseline = values(browser="false")
    baseline["BROWSER_RESULT"] = "success"
    assert validate_quality_gate(baseline)


def test_manual_browser_opt_in_changes_required_result() -> None:
    assert not validate_quality_gate(values(event="workflow_dispatch", opt_in="false"))
    baseline = values(event="workflow_dispatch", opt_in="true")
    baseline["BROWSER_RESULT"] = "skipped"
    assert validate_quality_gate(baseline)


def test_bad_event_and_missing_pr_browser_classification_fail_closed() -> None:
    baseline = values()
    baseline["EVENT_NAME"] = "unknown"
    assert validate_quality_gate(baseline)
    baseline = values()
    baseline["BROWSER_CHANGED"] = ""
    assert validate_quality_gate(baseline)


def test_cli_propagates_failure_as_nonzero() -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / "ci_quality_gate.py"
    clean = subprocess.run([sys.executable, str(script)], env={**os.environ, **values()}, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr + clean.stdout
    missing = values()
    missing["DEPENDENCY_REVIEW_RESULT"] = "failure"
    bad = subprocess.run([sys.executable, str(script)], env={**os.environ, **missing}, capture_output=True, text=True)
    assert bad.returncode == 1
    assert "::error::" in bad.stdout
