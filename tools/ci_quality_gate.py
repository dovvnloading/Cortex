"""Fail-closed final Quality gate for PRs, pushes and manual test runs.

This is intentionally standard-library-only, so a bad dependency install or
skipped predecessor cannot turn the historically required 'heavy' check green.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


CODE_GATES = ("LINT_RESULT", "BACKEND_RESULT", "FRONTEND_RESULT", "PACKAGE_RESULT")
EVENTS = frozenset({"pull_request", "push", "workflow_dispatch"})


def validate_quality_gate(values: Mapping[str, str]) -> list[str]:
    """Return every incorrect result, never assuming a missing value passed."""

    errors: list[str] = []
    changes = values.get("CHANGES_RESULT", "")
    code = values.get("CODE_CHANGED", "")
    event = values.get("EVENT_NAME", "")
    browser_class = values.get("BROWSER_CHANGED", "")
    browser_opt_in = values.get("BROWSER_OPT_IN", "")

    if changes != "success":
        errors.append(f"Change classification must succeed; got {changes!r}")
    if code not in {"true", "false"}:
        errors.append(f"Change classification must produce code=true/false; got {code!r}")
    if event not in EVENTS:
        errors.append(f"Unrecognized Quality event {event!r}")
    if event == "pull_request" and browser_class not in {"true", "false"}:
        errors.append(f"Browser classification must be true/false; got {browser_class!r}")
    if event == "workflow_dispatch" and browser_opt_in not in {"true", "false"}:
        errors.append(f"Manual browser input must be true/false; got {browser_opt_in!r}")

    dependency_review = values.get("DEPENDENCY_REVIEW_RESULT", "")
    expected_review = "success" if event == "pull_request" else "skipped"
    if dependency_review != expected_review:
        errors.append(f"DEPENDENCY_REVIEW_RESULT must be {expected_review}; got {dependency_review!r}")

    # An unknown classification is never a reason to accept skipped checks.
    if changes != "success" or code not in {"true", "false"} or event not in EVENTS:
        return errors

    expected_code_gate = "success" if code == "true" else "skipped"
    for gate in CODE_GATES:
        actual = values.get(gate, "")
        if actual != expected_code_gate:
            errors.append(f"{gate} must be {expected_code_gate}; got {actual!r}")

    browser_expected = code == "true" and (
        event == "push"
        or (event == "pull_request" and browser_class == "true")
        or (event == "workflow_dispatch" and browser_opt_in == "true")
    )
    expected_browser = "success" if browser_expected else "skipped"
    actual_browser = values.get("BROWSER_RESULT", "")
    if actual_browser != expected_browser:
        errors.append(f"BROWSER_RESULT must be {expected_browser}; got {actual_browser!r}")
    return errors


def main() -> int:
    failures = validate_quality_gate(os.environ)
    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        return 1
    print("Quality gate passed: classification, dependency review, tests, packaging, and browser requirements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
