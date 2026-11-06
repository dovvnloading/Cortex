"""The offline trajectory evaluator is a stable and honest quality gate."""

from __future__ import annotations

import json

import pytest

from tools import harness_eval


def test_every_synthetic_harness_contract_passes() -> None:
    report = harness_eval.run_suite()
    assert report["suite"] == "cortex-harness-contract-v1"
    assert report["interpretation"].startswith("deterministic harness contracts")
    assert report["case_count"] >= 12
    assert report["failed"] == 0, report["cases"]
    assert report["pass_rate"] == 1.0
    assert report["model_calls"] >= 5
    assert len({case["name"] for case in report["cases"]}) == report["case_count"]
    assert all(case["checks"] and all(case["checks"].values()) for case in report["cases"])


def test_subset_is_stable_and_includes_only_named_case() -> None:
    report = harness_eval.run_suite("approved-result-feedback")
    assert report["case_count"] == 1
    assert report["cases"][0]["name"] == "approved-result-feedback"
    assert report["cases"][0]["category"] == "feedback"


def test_command_line_can_emit_machine_readable_results(capsys) -> None:
    assert harness_eval.main(["--check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == report["case_count"]
    assert report["interpretation"].endswith("task-success scores")


def test_unknown_case_is_an_error_not_an_empty_green_suite() -> None:
    with pytest.raises(SystemExit) as error:
        harness_eval.main(["--case", "typo-case", "--check"])
    assert error.value.code == 2


def test_a_failed_contract_exits_nonzero_and_is_redacted(capsys, monkeypatch) -> None:
    def failed() -> harness_eval.CaseResult:
        raise RuntimeError("SECRET-FIXTURE-PROMPT-SHOULD-NOT-BE-LOGGED")

    monkeypatch.setattr(
        harness_eval,
        "SCENARIOS",
        (("synthetic-failure", failed),),
    )
    assert harness_eval.main(["--json", "--check"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failed"] == 1
    assert report["cases"][0]["error_kind"] == "RuntimeError"
    assert "SECRET-FIXTURE-PROMPT-SHOULD-NOT-BE-LOGGED" not in json.dumps(report)


def test_no_approval_or_execution_worker_is_needed(monkeypatch) -> None:
    """The evaluator is a pure prompt/agent replay, not an execution harness."""
    def forbidden(*args, **kwargs):
        raise AssertionError("the evaluator must never start a code worker")

    from cortex_backend.execution import code_execution

    monkeypatch.setattr(code_execution, "run_code_in_worker", forbidden)
    report = harness_eval.run_suite()
    assert report["failed"] == 0
