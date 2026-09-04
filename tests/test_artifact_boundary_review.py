"""The artifact security review corpus is deterministic and fail-closed."""

from __future__ import annotations

from tools.artifact_boundary_review import run_review


def test_artifact_security_review_is_reproducible_and_complete():
    first = run_review()
    second = run_review()

    assert first == second
    assert first["status"] == "passed"
    assert first["cases"] == 12
    assert first["passed"] == 12
    assert first["blocked"] == 0
    assert first["failed"] == 0
    assert all(outcome["status"] == "passed" for outcome in first["outcomes"])
