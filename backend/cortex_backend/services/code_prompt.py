"""Just-in-time policy for the optional local code capability.

The normal chat prompt deliberately says nothing about code execution.  This
module keeps the small, deterministic admission gate next to the prompt
assembly so the detailed execution contract is added only when the current
user turn looks like an explicit local task.
"""

from __future__ import annotations

import re


_REFERENTIAL_EXECUTION_RE = re.compile(
    r"\b(?:run|execute|launch|invoke|start)\b\s+(?:this|that|it|these|those|the attached)\b"
)
_USE_CODE_RE = re.compile(
    r"\b(?:use|with|in)\s+(?:python|a python script|a script|code)\b\s+(?:to|and)\b"
)
_COMPUTATION_RE = re.compile(
    r"\b(?:calculate|compute)\b.*\b(?:python|script|code|data|csv|json|spreadsheet|file|attachment)\b"
)
_LOCAL_TASK_RE = re.compile(
    r"\b(?:process|transform|automate|analyze|inspect|fetch|download|read|write|"
    r"modify|create|generate)\b.*\b(?:file|folder|directory|attachment|data|csv|json|"
    r"spreadsheet|document|url|network|request|api|process|command)\b"
)
_CODE_TARGET_RE = re.compile(
    r"\b(?:python|script|code|program|command|shell|test|tests|file|folder|directory|"
    r"attachment|data|csv|json|spreadsheet|app|application|process|request|url|network)\b"
)
_NEGATED_EXECUTION_RE = re.compile(
    r"\b(?:do not|don't|never|without)\s+(?:run|execute|launch|invoke|start)\b"
)
_EXPLANATION_ONLY_RE = re.compile(
    r"\b(?:explain|describe|what is|what does|how do i|how can i|why does)\b"
)


def should_offer_code_execution(query: str) -> bool:
    """Return whether this turn merits the just-in-time code contract.

    This is intentionally conservative.  Mentioning code, Python, or a
    fenced snippet is not enough; the request must also contain an execution,
    computation, or local-resource action.  The model still has a second
    responsibility gate in the prompt, and the backend carries this decision
    into proposal validation so a spontaneous envelope cannot start a job.
    """

    normalized = " ".join(str(query or "").split()).casefold()
    if not normalized:
        return False
    if _NEGATED_EXECUTION_RE.search(normalized):
        return False

    direct_execution = bool(
        _REFERENTIAL_EXECUTION_RE.search(normalized)
        or (
            re.search(r"\b(?:run|execute|launch|invoke|start)\b", normalized)
            and _CODE_TARGET_RE.search(normalized)
        )
    )
    if direct_execution:
        return True
    if _USE_CODE_RE.search(normalized):
        return True
    if _COMPUTATION_RE.search(normalized):
        return True
    if _LOCAL_TASK_RE.search(normalized):
        return True

    # A purely educational request should not pay for the execution contract,
    # even when it mentions Python or a code sample.
    if _EXPLANATION_ONLY_RE.search(normalized):
        return False
    return False
