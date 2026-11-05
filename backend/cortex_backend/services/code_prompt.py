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
_PYTHON_OUTPUT_RE = re.compile(
    r"\b(?:print|output|return)\b.*\b(?:in|using|with)\s+python\b"
    r"|\bpython\b.*\b(?:print|output|return)\b"
)
_COMPUTATION_RE = re.compile(
    r"\b(?:calculate|compute)\b.*\b(?:python|script|code|data|csv|json|spreadsheet|file|attachment)\b"
)
# The verb has to sit close to a concrete artefact, and the artefact has to be
# something that actually lives on disk or on the wire. The previous pattern
# allowed any distance between the two (".*") and counted "data", "document",
# "request" and "network" as artefacts, so ordinary prose matched: "write a
# blog post about data science trends", "help me write a cover letter for a
# data analyst job" and "write a haiku about the network" all took the code
# path -- paying for the ~1k-token contract and switching sampling to the
# coding profile.
_ARTEFACT = (
    r"(?:files?|folders?|director(?:y|ies)|attachments?|csv|json|xml|ya?ml|"
    r"spreadsheets?|workbooks?|scripts?|programs?|logs?|urls?|endpoints?|api)"
)
_LOCAL_TASK_RE = re.compile(
    r"\b(?:process|transform|automate|analyze|analyse|inspect|fetch|download|"
    r"parse|convert|rename|read|write|modify|edit|create|generate|save)\b"
    r"(?:\s+\S+){0,3}\s+\b" + _ARTEFACT + r"\b"
)
# The execution verbs follow the same rule: "run", "start" and friends count
# only when a concrete target sits within a few words of them. With any
# distance allowed and "data", "process", "request" and "network" as targets,
# "start the process of writing my novel", "run me through the data from the
# survey" and "launch a request for proposals" were all admitted as code turns.
_DIRECT_EXECUTION_RE = re.compile(
    r"\b(?:run|execute|launch|invoke|start)\b(?:\s+\S+){0,3}\s+"
    r"\b(?:python|scripts?|code|programs?|commands?|shell|tests?|files?|folders?|"
    r"director(?:y|ies)|attachments?|csv|json|spreadsheets?|apps?|applications?|urls?)\b"
)
_NEGATED_EXECUTION_RE = re.compile(
    r"\b(?:do not|don't|never|without)\s+(?:run|execute|launch|invoke|start)\b"
)
# Educational questions are about what the *user is asking us to do*, not
# about the mere presence of a word. An imperative "run tests and explain the
# failures" must still get the execution contract. Conversely "how do I run
# these tests?" is a question about the procedure, not authorization to run.
_EXPLANATION_ONLY_RE = re.compile(
    r"^(?:(?:please|could you|can you|would you|i want you to|i need you to)\s+)*"
    r"(?:explain|describe|what is|what does|how do i|how can i|why does|"
    r"how should i|how would i|tell me how to|show me how to)\b"
)
# An explicitly sequenced action overrides an educational lead-in. The normal
# concrete-target checks below still decide eligibility: "explain X, then
# run through the reasoning" must never look like "run the tests".
_SEQUENCED_ACTION_RE = re.compile(
    r"\b(?:and(?:\s+then)?|then|after that|afterwards?|followed by)\s+"
    r"(?:please\s+)?(?:run|execute|launch|invoke|start|use|calculate|compute|"
    r"process|transform|automate|analyze|analyse|inspect|fetch|download|parse|"
    r"convert|rename|read|write|modify|edit|create|generate|save|print|output)\b"
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
    # Educational *lead-ins* exclude execution unless the user separately
    # gives an imperative action after "then"/"and"/etc. A later "explain"
    # does not veto an initial explicit command ("run the tests and explain").
    # The positive checks below still require a concrete target or code task.
    if _EXPLANATION_ONLY_RE.search(normalized) and not _SEQUENCED_ACTION_RE.search(
        normalized
    ):
        return False

    direct_execution = bool(
        _REFERENTIAL_EXECUTION_RE.search(normalized)
        or _DIRECT_EXECUTION_RE.search(normalized)
    )
    if direct_execution:
        return True
    if _USE_CODE_RE.search(normalized):
        return True
    if _PYTHON_OUTPUT_RE.search(normalized):
        return True
    if _COMPUTATION_RE.search(normalized):
        return True
    if _LOCAL_TASK_RE.search(normalized):
        return True
    return False
