"""The artifact security review corpus is deterministic and fail-closed."""

from __future__ import annotations

import json

import tools.artifact_boundary_review as review
from tools.artifact_boundary_review import run_review


def test_artifact_security_review_is_reproducible_and_complete():
    first = run_review()
    second = run_review()

    assert first == second
    assert first["corpus"] == "artifact-boundary-review.v1"
    assert first["cases"] == 26
    assert first["failed"] == 0
    assert first["passed"] + first["blocked"] == first["cases"]
    assert first["status"] == ("blocked" if first["blocked"] else "passed")
    assert [outcome["case"] for outcome in first["outcomes"][:12]] == [
        "copy_in_preserves_source", "copy_in_owner_binding", "copy_in_ads_path",
        "copy_in_untyped_grant", "copy_in_hardlink", "copy_in_symlink",
        "mime_active_and_nonfinite_corpus", "output_exact_claims_and_quarantine",
        "output_symlink_rejection", "output_hardlink_rejection",
        "output_publication_rollback", "repository_size_integrity",
    ]
    for outcome in first["outcomes"]:
        assert set(outcome) <= {"case", "status", "reason"}
        if outcome["status"] == "blocked":
            assert outcome["reason"] in {
                "symlink_unavailable", "hardlink_unavailable", "junction_unavailable",
            }


def _passing_case(_root, _repository, _boundary, _job_id) -> None:
    return None


def _other_passing_case(_root, _repository, _boundary, _job_id) -> None:
    del _root  # Different code under the same name must change the digest.


def _failing_case(_root, _repository, _boundary, _job_id) -> None:
    raise review._ReviewFailure("accepted:artifact_reparse_point")


def _blocked_case(_root, _repository, _boundary, _job_id) -> None:
    raise review._BlockedCapability("symlink_unavailable")


def _digest_with(monkeypatch, cases) -> str:
    monkeypatch.setattr(review, "_CASES", tuple(cases))
    return str(run_review()["corpus_digest"])


def test_the_digest_tracks_the_cases_that_actually_run(monkeypatch):
    """A digest of a name list kept beside the corpus did not move when a case
    was deleted or rewritten; it has to fingerprint what executes."""

    full = _digest_with(monkeypatch, [("one", _passing_case), ("two", _passing_case)])
    dropped = _digest_with(monkeypatch, [("one", _passing_case)])
    rewritten = _digest_with(monkeypatch, [("one", _passing_case), ("two", _other_passing_case)])
    reordered = _digest_with(monkeypatch, [("two", _passing_case), ("one", _passing_case)])

    assert len({full, dropped, rewritten, reordered}) == 4
    assert _digest_with(monkeypatch, [("one", _passing_case), ("two", _passing_case)]) == full


def test_a_failing_case_is_reported_as_failed_not_blocked(monkeypatch):
    monkeypatch.setattr(review, "_CASES", (("bad", _failing_case), ("host", _blocked_case)))

    result = run_review()

    assert result["status"] == "failed"
    assert (result["passed"], result["blocked"], result["failed"]) == (0, 1, 1)
    assert result["outcomes"] == [
        {"case": "bad", "status": "failed", "reason": "accepted:artifact_reparse_point"},
        {"case": "host", "status": "blocked", "reason": "symlink_unavailable"},
    ]


def test_only_a_missing_host_capability_is_blocked(monkeypatch):
    monkeypatch.setattr(review, "_CASES", (("ok", _passing_case), ("host", _blocked_case)))

    assert run_review()["status"] == "blocked"


def test_an_empty_corpus_does_not_pass(monkeypatch):
    monkeypatch.setattr(review, "_CASES", ())

    assert run_review()["status"] == "failed"


def _canned(status: str, *, failed: int = 0, blocked: int = 0) -> dict[str, object]:
    outcomes: list[dict[str, str]] = []
    if failed:
        outcomes.append({"case": "bad", "status": "failed", "reason": "accepted:invalid_artifact"})
    if blocked:
        outcomes.append({"case": "host", "status": "blocked", "reason": "hardlink_unavailable"})
    return {
        "status": status,
        "corpus": "artifact-boundary-review.v1",
        "corpus_digest": "0123456789abcdef",
        "cases": len(outcomes),
        "passed": 0,
        "blocked": blocked,
        "failed": failed,
        "outcomes": outcomes,
    }


def test_strict_is_the_default(monkeypatch):
    """Without --strict a total failure used to exit zero."""

    monkeypatch.setattr(review, "run_review", lambda: _canned("failed", failed=1))
    assert review.main([]) == 1
    assert review.main(["--strict"]) == 1

    monkeypatch.setattr(review, "run_review", lambda: _canned("blocked", blocked=1))
    assert review.main([]) == 2
    assert review.main(["--json", "--strict"]) == 2

    monkeypatch.setattr(review, "run_review", lambda: _canned("passed"))
    assert review.main([]) == 0


def test_json_is_opt_in_and_the_default_output_is_readable(monkeypatch, capsys):
    """--json used to change only the separators; both modes printed JSON."""

    monkeypatch.setattr(review, "run_review", lambda: _canned("failed", failed=1, blocked=1))

    review.main(["--json"])
    printed = capsys.readouterr().out
    assert json.loads(printed)["status"] == "failed"
    assert ", " not in printed and ": " not in printed

    review.main([])
    readable = capsys.readouterr().out
    assert readable.startswith("Artifact boundary review: failed")
    assert "failed  bad (accepted:invalid_artifact)" in readable
    assert "blocked host (hardlink_unavailable)" in readable
