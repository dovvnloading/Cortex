"""Every sentence the local-code loop exchanges with the model and the user.

The execution layer deliberately speaks only in stable codes: the validator
raises ``CodeExecutionError("imports_not_allowed")`` and the worker answers
with ``{"ok": false, "code": ...}``.  That keeps the sandbox auditable, but a
bare code is useless to both audiences it eventually reaches -- a person who
does not write Python, and a small local model that has to correct itself.
This module is the single place that turns those codes into text, so wording
can be reviewed once instead of drifting across the API, the worker bridge and
the prompt assembly.

It is pure and deterministic on purpose: no I/O, no clock, no logging, no Qt,
and nothing imported beyond ``json``.  Callers must not log what these
functions return; observations carry program output, which is user data.

Why the specific rules here:

* **Tail-biased truncation.**  Python tracebacks, assertion messages and test
  summaries land at the *end* of a stream.  Cutting the tail to preserve a
  pretty head throws away the only part that explains a failure, so the head
  keeps a small orientation slice (``OBSERVATION_HEAD_RATIO``) and the rest of
  the budget goes to the tail.  The same reasoning gives ``stderr`` priority
  over ``stdout`` when both are large.

* **Never-empty observations.**  A program that prints nothing is the common
  case for a successful write or a silent assertion.  Handing a small model an
  empty observation reads as "the tool returned nothing", and it typically
  stalls, apologises, or silently re-proposes the same program.  Every
  observation therefore states something, even if that is only "no output".

* **Short repair prompts.**  Small local models weight the most recent
  instruction most heavily and dilute earlier ones as the correction grows.  A
  repair turn is one short paragraph -- reason, fix, output format -- with the
  output format last, and ``MAX_PROPOSAL_REPAIR_ATTEMPTS`` caps the loop at a
  single retry so a model that cannot satisfy the subset does not burn the
  user's turn budget arguing with the validator.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from cortex_backend.core.generation import CodeProposalRejection


MAX_OBSERVATION_CHARS: int = 6000
OBSERVATION_HEAD_RATIO: float = 0.3
MAX_PROPOSAL_REPAIR_ATTEMPTS: int = 1

_MAX_VALUE_CHARS = 1000
# Status line, up to three headings, a full-size rendered value, the truncation
# notice and a failure line, with slack. Held out of the stream budget so the
# whole observation stays inside MAX_OBSERVATION_CHARS.
_FIXED_PART_RESERVE = _MAX_VALUE_CHARS + 400
_TRUNCATION_MARKER = "\n... [{omitted} characters omitted] ...\n"
_GENERIC_REJECTION_MESSAGE = "Cortex could not accept that code request."
_GENERIC_REPAIR_HINT = (
    "Re-send a minimal program that uses only the operations listed in the contract."
)

# Refusals no correction can lift: the capability does not exist, or the
# request is simply too big for the transport.  Re-prompting only costs a turn.
_UNREPAIRABLE_CODES = frozenset(
    {
        "process_capability_unavailable",
        "source_too_large",
        "payload_too_large",
        "multiple_requests",
        # Admission is decided for the whole turn before the model answers, so
        # no correction the model sends can make this turn eligible.
        "not_offered",
        # The run workspace is resolved before any capability is consulted, so
        # this is a host-side problem. Dropping the filesystem grant, which is
        # the only edit a model could make, changes nothing.
        "workspace_invalid",
    }
)

# Codes a program can only reach after it was approved and started running.
# They share the tables above -- the execution loop needs the same wording --
# but a correction for them must not open by claiming nothing ran.
_RUNTIME_CODES = frozenset(
    {
        "runtime_error",
        "runtime_limit",
        "memory_limit",
        "result_duration_invalid",
        "filesystem_limit",
        "filesystem_changed",
        "filesystem_read_failed",
        "filesystem_write_failed",
        "filesystem_list_failed",
        "process_limit",
        "process_isolation_unavailable",
        "process_output_limit",
        "process_timeout",
        "network_limit",
        "network_host_unavailable",
        "network_response_limit",
        "network_response_invalid",
        "network_request_failed",
    }
)


# One plain sentence per stable code, written for someone who does not read
# Python.  Keys cover both halves of the pipeline: the codes the response
# parser produces while reading the proposal envelope, and every code
# ``execution/code_execution.py`` raises while validating or running it.
REJECTION_MESSAGES: Mapping[str, str] = {
    # Admission, before the envelope is even read.
    "not_offered": (
        "The assistant offered to run code, but this request was not admitted "
        "as a local task, so nothing was queued."
    ),
    # Envelope parsing, before the sandbox ever sees the program.
    "invalid_json": "The code request was not readable, so Cortex ignored it.",
    "invalid_fields": "The code request contained unexpected fields, so Cortex ignored it.",
    "unsupported_language": "Cortex only runs Python for local tasks.",
    "duplicate_field": "The code request repeated a field, so Cortex could not trust it.",
    "payload_too_large": "The code request was too large for Cortex to read.",
    "multiple_requests": "The assistant sent more than one code request at once, so none of them ran.",
    # Request construction and source validation.
    "source_empty": "The assistant sent an empty program, so there was nothing to run.",
    "source_too_large": "The program was too long for Cortex to run safely.",
    "source_too_complex": "The program was too complicated for Cortex to check safely.",
    "syntax_invalid": "The program was not valid Python, so Cortex could not run it.",
    "syntax_not_allowed": "The program used a Python feature Cortex does not allow.",
    "imports_not_allowed": "Cortex only runs code without imports.",
    "unbounded_loop": "Cortex does not run loops that could carry on forever.",
    "function_definitions_not_allowed": "Cortex does not run code that defines its own functions.",
    "class_definitions_not_allowed": "Cortex does not run code that defines its own classes.",
    "try_not_allowed": "Cortex does not run code that catches and hides its own errors.",
    "with_not_allowed": "Cortex does not run code that opens resources for itself.",
    "raise_not_allowed": "Cortex does not run code that raises its own errors.",
    "delete_not_allowed": "Cortex does not run code that deletes its own values.",
    "loop_target_not_allowed": "A loop in the program was written in a way Cortex does not allow.",
    "bounded_range_required": "Every loop has to count over a fixed number of steps, and this one did not.",
    "loop_work_too_large": "The program asked for far more repetitions than Cortex allows.",
    "operator_not_allowed": "The program used a maths or logic operator Cortex does not allow.",
    "exponent_too_large": "The program raised a number to a power that is far too large.",
    # Raised for any multiplication by an integer literal over 100000, whether
    # or not a sequence is involved, so the wording must cover plain arithmetic.
    "sequence_too_large": "The program multiplied by a number that is far too large.",
    "sequence_bound_required": "The program repeated a value an unpredictable number of times.",
    "comparison_not_allowed": "The program used a comparison Cortex does not allow.",
    "name_not_allowed": "The program used a name that is off limits inside Cortex.",
    "constant_not_allowed": "The program used a kind of value Cortex does not allow.",
    "call_not_allowed": "The program called something outside the small set of allowed operations.",
    "attribute_not_allowed": "The program used a method or property that is unavailable inside Cortex.",
    "capabilities_invalid": "The request asked for permissions in a form Cortex does not understand.",
    "intent_invalid": "The request did not come with a clear, short summary of what it would do.",
    "process_capability_unavailable": "Cortex cannot run other programs on your computer yet.",
    # Failures raised once an approved program is already running.
    "workspace_invalid": "Cortex could not prepare a safe folder for the run.",
    "filesystem_limit": "The run reached the limit on how much it may read or write.",
    "filesystem_changed": "A file changed while the run was reading it, so the run was stopped.",
    "filesystem_read_failed": "The run could not read one of the files it asked for.",
    "filesystem_write_failed": "The run could not write one of the files it asked for.",
    "filesystem_list_failed": "The run could not list the contents of a folder.",
    "process_limit": "The run reached the limit on how many programs it may start.",
    "process_isolation_unavailable": "Cortex could not isolate another program safely, so it did not start one.",
    "process_output_limit": "Another program produced more output than Cortex accepts.",
    "process_timeout": "Another program took too long and was stopped.",
    "network_limit": "The run reached the limit on how many web requests it may make.",
    "network_host_unavailable": "The web address the run asked for could not be reached.",
    "network_response_limit": "A web response was too large for Cortex to accept.",
    "network_response_invalid": "A web response arrived in a form Cortex could not accept.",
    "network_request_failed": "A web request made by the run did not succeed.",
    "runtime_limit": "The run took too long and was stopped.",
    "memory_limit": "The run needed more memory than Cortex allows.",
    "result_duration_invalid": "The run finished but reported an unusable timing value.",
    "runtime_error": "The program stopped with an error while it was running.",
}


# The model-facing half of the same table: one imperative instruction naming
# the exact edit that satisfies the validator.  Kept under ~200 characters
# each, because a repair turn that grows longer than the original proposal
# starts competing with the proposal for the model's attention.
REPAIR_HINTS: Mapping[str, str] = {
    "invalid_json": (
        "The block must hold one JSON object and nothing else. Escape newlines in `source` "
        'as \\n and quotes as \\".'
    ),
    "invalid_fields": (
        "Use only the keys language, source, intent_summary and capabilities. Remove every other key."
    ),
    "unsupported_language": 'Set "language": "python". No other language is accepted.',
    "duplicate_field": "Each key may appear once in the JSON object. Remove the repeated key.",
    "payload_too_large": (
        "Send a far shorter program and summary so the whole block stays small."
    ),
    "multiple_requests": (
        "Send exactly one <code_execution_request> block per reply, containing the whole task."
    ),
    "source_empty": "Put the complete Python program in `source`; it must not be empty.",
    "source_too_large": (
        "Send a far shorter program: only the few statements needed to produce the result."
    ),
    "source_too_complex": (
        "Simplify the program: fewer statements, less nesting, no deeply nested expressions. "
        "Aim for a handful of top-level lines."
    ),
    "syntax_invalid": (
        "The source is not valid Python. Fix the syntax, and make sure every newline inside "
        "the JSON string is escaped as \\n."
    ),
    "syntax_not_allowed": (
        "Use only assignments, if, for over range(), expressions and print(). Remove every "
        "other kind of statement."
    ),
    "imports_not_allowed": (
        "Remove every import statement. The sandbox has no modules; use only the built-in "
        "operations listed in the contract."
    ),
    "unbounded_loop": (
        "Replace the while-loop with `for i in range(N):` where N is a plain integer literal."
    ),
    "function_definitions_not_allowed": (
        "Remove def, lambda and class. Write the logic inline at the top level."
    ),
    "class_definitions_not_allowed": (
        "Remove every class definition. Use plain dicts, lists and top-level statements instead."
    ),
    "try_not_allowed": (
        "Remove try, except and finally. Test values with if instead and let the sandbox "
        "report any failure."
    ),
    "with_not_allowed": (
        "Remove every with-block. Call cortex.fs.read_text() or cortex.fs.write_text() directly."
    ),
    "raise_not_allowed": (
        "Remove every raise statement. Print a message instead when you need to signal a problem."
    ),
    "delete_not_allowed": (
        "Remove every del statement. Assign a new value instead of deleting a name."
    ),
    "loop_target_not_allowed": (
        "Use one simple loop variable, for example `for i in range(10):`. Unpacking or "
        "subscript targets are not accepted."
    ),
    # Both hints must name the per-range cap. A model that wrote
    # `range(50000)` is rejected with bounded_range_required, not
    # loop_work_too_large, and a hint that only says "use integer literals"
    # describes what it already did -- so it re-sends the same program and the
    # single repair turn is spent for nothing.
    "bounded_range_required": (
        "Loops and comprehensions must iterate over range() with plain integer literals, and one "
        "range may span at most 10000 steps, for example `for i in range(100):`. Iterating a list, "
        "a string or a variable is not accepted."
    ),
    "loop_work_too_large": (
        "Reduce the ranges: each range() may span at most 10000 steps, and all nested loops "
        "together at most 100000 iterations."
    ),
    # Augmented assignment (`x += 1`) IS accepted -- the contract recommends it
    # for building strings -- so this must not tell the model to remove it.
    "operator_not_allowed": (
        "Use only + - * / // % ** with plain boolean logic. Remove bitwise operators such as "
        "& | ^ ~ << >> and the matrix operator @."
    ),
    "exponent_too_large": (
        "Use a whole-number exponent between 0 and 1000, for example `2 ** 10`. Negative and "
        "fractional exponents are not accepted."
    ),
    "sequence_too_large": (
        "Multiply by a whole number of 100000 or less, whether you are scaling a number or "
        "repeating a list or string."
    ),
    "sequence_bound_required": (
        "When repeating a list or string with *, use an integer literal for the count, "
        "for example `[0] * 10`."
    ),
    "comparison_not_allowed": "Compare values with only == != < <= > >= in, not in, is and is not.",
    "name_not_allowed": (
        "Rename the variable. Names starting with __ and names such as eval, exec, open, "
        "globals, getattr and type are unavailable."
    ),
    "constant_not_allowed": (
        "Use only int, float, str, bool and None literals. Remove bytes, complex numbers, "
        "inf and nan."
    ),
    "call_not_allowed": (
        "Call only the listed built-ins such as print, len, range, sum, sorted, int and str, "
        "or the exact cortex broker methods. Do not unpack arguments with **."
    ),
    "attribute_not_allowed": (
        "Remove all attribute and method access. Methods such as .split(), .append() or "
        ".upper() are unavailable; only the exact cortex broker calls are allowed."
    ),
    "capabilities_invalid": (
        'Send capabilities as an object with only the boolean keys filesystem, process and '
        'network, for example {"filesystem": false, "process": false, "network": false}.'
    ),
    "intent_invalid": (
        "Send intent_summary as one short plain sentence, under 500 characters, saying what "
        "the program does."
    ),
    "process_capability_unavailable": (
        'Remove every cortex.process call and set "process": false. Cortex cannot start other '
        "programs; solve the task without one."
    ),
    "workspace_invalid": (
        'Re-send the program without filesystem access and set "filesystem": false; the run '
        "workspace was unavailable."
    ),
    "filesystem_limit": (
        "Read or write far fewer and far smaller files: a handful of files, each well under one megabyte."
    ),
    "filesystem_changed": (
        "The file changed while it was being read. Re-send the same program to read it once more."
    ),
    "filesystem_read_failed": (
        "Read a file the run has created itself or seen in cortex.fs.listdir('.'); host paths "
        "are not reachable."
    ),
    "filesystem_write_failed": (
        "Write to a simple relative name inside the run workspace, for example "
        "cortex.fs.write_text('out.txt', text)."
    ),
    "filesystem_list_failed": (
        "List a folder that exists in the run workspace, for example cortex.fs.listdir('.')."
    ),
    "process_limit": (
        'Remove every cortex.process call and set "process": false; the sandbox cannot start programs.'
    ),
    "process_isolation_unavailable": (
        'Remove every cortex.process call and set "process": false; the sandbox cannot start programs.'
    ),
    "process_output_limit": (
        'Remove every cortex.process call and set "process": false; the sandbox cannot start programs.'
    ),
    "process_timeout": (
        'Remove every cortex.process call and set "process": false; the sandbox cannot start programs.'
    ),
    "network_limit": (
        "Make at most four cortex.net.get() calls, and reuse text you have already fetched."
    ),
    "network_host_unavailable": (
        "Use one full public https:// address with a real host name. Local, private and "
        "metadata addresses are rejected."
    ),
    "network_response_limit": (
        "Fetch a smaller page or a specific endpoint; responses larger than 256 KB are rejected."
    ),
    "network_response_invalid": (
        "Fetch a plain-text or JSON endpoint over https and do not depend on response headers."
    ),
    "network_request_failed": (
        'The request did not succeed. Check the URL, or answer without the network and set "network": false.'
    ),
    "runtime_limit": (
        "The program ran out of time. Do far less work: smaller ranges, fewer steps, no nested "
        "loops over large ranges."
    ),
    "memory_limit": (
        "The program used too much memory. Build much smaller lists, strings and dictionaries."
    ),
    "runtime_error": (
        "The program raised an error while running. Read the Errors section above and send a "
        "corrected program."
    ),
}


def describe_rejection(code: str) -> CodeProposalRejection:
    """Turn a stable validator/parser code into a presentable refusal.

    An unknown code still yields a usable value rather than raising: the codes
    come from a layer that may grow new ones, and a missing sentence must never
    be the reason a refusal fails to reach the user.  Such a code is reported
    as not repairable, because without a specific hint a repair turn would only
    tell the model to try again.
    """

    normalized = str(code or "").strip()
    message = REJECTION_MESSAGES.get(normalized, _GENERIC_REJECTION_MESSAGE)
    repairable = normalized in REPAIR_HINTS and normalized not in _UNREPAIRABLE_CODES
    return CodeProposalRejection(
        code=normalized or "unknown",
        message=message,
        repairable=repairable,
    )


def repair_prompt(rejection: CodeProposalRejection) -> str:
    """Build the one user-role turn that asks the model to fix its proposal.

    Three sentences in a fixed order: what went wrong, the exact edit, and the
    output format.  The format instruction is deliberately last -- a small
    model that reads a long correction tends to honour whichever instruction it
    saw most recently, and an unparseable reply wastes the single retry.
    """

    hint = REPAIR_HINTS.get(rejection.code, _GENERIC_REPAIR_HINT)
    # Opening with "rejected before it ran" for a failure that happened *while*
    # running contradicts what the model can see in its own observation, and a
    # correction that starts by being wrong about the facts is easy to ignore.
    preamble = (
        "Your program started running and then failed"
        if rejection.code in _RUNTIME_CODES
        else "Your code request was rejected before it ran"
    )
    return (
        f"{preamble}: {rejection.message}\n"
        f"{hint}\n"
        "Reply with only the corrected "
        "<code_execution_request>...</code_execution_request> block and no other text."
    )


def head_tail_truncate(
    text: str,
    limit: int,
    *,
    head_ratio: float = OBSERVATION_HEAD_RATIO,
) -> tuple[str, bool]:
    """Shrink ``text`` to ``limit`` characters, keeping mostly the tail.

    Returns the text and whether anything was dropped.  The split is
    tail-biased because the interesting part of program output -- the
    traceback, the last assertion, the final summary line -- is at the end;
    the head slice exists only so the reader can tell what the output was.

    ``limit`` may be smaller than the omission marker itself (a caller can
    divide a shared budget down to almost nothing), so that case degrades to a
    bare tail slice instead of returning something longer than ``limit``.
    """

    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    ratio = min(max(head_ratio, 0.0), 1.0)
    # The marker embeds the omitted count, which depends on the space the
    # marker takes.  Size it with the full length -- an upper bound on that
    # count -- so the finished string can only come in at or under ``limit``.
    marker_width = len(_TRUNCATION_MARKER.format(omitted=len(text)))
    available = limit - marker_width
    if available <= 0:
        return text[-limit:], True
    head_size = min(int(limit * ratio), available - 1)
    tail_size = available - head_size
    head = text[:head_size]
    tail = text[len(text) - tail_size :]
    omitted = len(text) - head_size - tail_size
    return f"{head}{_TRUNCATION_MARKER.format(omitted=omitted)}{tail}", True


def format_execution_observation(
    *,
    status: str,
    stdout: str = "",
    stderr: str = "",
    value: object = None,
    truncated: bool = False,
    duration_ms: int | None = None,
    error: str | None = None,
) -> str:
    """Render what the model is told after an approved run finishes.

    The layout is fixed so the model sees the same shape every time: outcome
    first, then output, then errors, then the structured value, then any
    caveats.  The result is never empty -- a silent successful run still says
    so -- because an empty tool observation is the single most reliable way to
    make a small model stall or invent a result.
    """

    out = _clean_stream(stdout)
    err = _clean_stream(stderr)
    out_budget, err_budget = _stream_budgets(out, err)
    # The streams share what is left after the fixed parts, so the finished
    # observation honours MAX_OBSERVATION_CHARS as a whole. Budgeting only the
    # streams let the headings, the rendered value and the failure line push a
    # large run well past the ceiling the caller was promised.
    out_budget, err_budget = _reserve_for_fixed_parts(out_budget, err_budget)
    out_text, out_truncated = head_tail_truncate(out, out_budget)
    err_text, err_truncated = head_tail_truncate(err, err_budget)
    value_text, value_truncated = _render_value(value)

    lines = [_status_line(status, duration_ms)]
    if out_text:
        lines.append("Output:")
        lines.append(out_text)
    if err_text:
        lines.append("Errors:")
        lines.append(err_text)
    if value_text:
        lines.append("Value:")
        lines.append(value_text)
    if not out_text and not err_text and not value_text:
        lines.append("The program produced no output.")
    if truncated or out_truncated or err_truncated or value_truncated:
        lines.append("Part of the output was left out because it was too long.")
    if error:
        # Bounded like everything else here: this is a stable code in practice,
        # but it arrives from a caller and must not be able to blow the budget.
        failure = " ".join(str(error).split())[:200]
        lines.append(f"Failure: {failure or 'unknown'}")
    return "\n".join(lines)


def _status_line(status: str, duration_ms: int | None) -> str:
    """First line of every observation: the outcome, and how long it took."""

    label = " ".join(str(status or "").split()).casefold() or "unknown"
    if duration_ms is not None and not isinstance(duration_ms, bool) and duration_ms >= 0:
        return f"Local code run finished: {label} in {int(duration_ms)} ms"
    return f"Local code run finished: {label}"


def _clean_stream(value: str) -> str:
    """Normalize one captured stream without disturbing its interior."""

    return str(value).strip() if value else ""


def _reserve_for_fixed_parts(out_budget: int, err_budget: int) -> tuple[int, int]:
    """Shrink the stream budgets to leave room for everything else.

    The reserve covers the status line, the headings, a fully-sized rendered
    value and the closing notices. It is taken proportionally so a run with
    only one stream does not lose the whole reserve from that stream.
    """

    total = out_budget + err_budget
    if total <= 0:
        return out_budget, err_budget
    available = max(0, MAX_OBSERVATION_CHARS - _FIXED_PART_RESERVE)
    if total <= available:
        return out_budget, err_budget
    scaled_err = err_budget * available // total
    return available - scaled_err, scaled_err


def _stream_budgets(stdout: str, stderr: str) -> tuple[int, int]:
    """Divide the observation budget, favouring ``stderr`` under pressure.

    Whichever stream is absent contributes nothing, and when both fit there is
    no division to make.  Only when the pair overflows does ``stderr`` claim
    the larger share: diagnosing a failed run needs the error text far more
    than it needs the successful prints that preceded it.
    """

    total = MAX_OBSERVATION_CHARS
    if not stderr:
        return total, 0
    if not stdout:
        return 0, total
    if len(stdout) + len(stderr) <= total:
        return len(stdout), len(stderr)
    error_budget = min(len(stderr), max(total // 2, total - len(stdout)))
    return total - error_budget, error_budget


def _render_value(value: object) -> tuple[str, bool]:
    """Render the program's ``_result`` as bounded, JSON-ish text.

    ``None`` means the program set no result, which is not worth a heading.
    Anything JSON cannot express falls back to ``repr`` rather than failing,
    because a run that produced an exotic value still has a story to tell.
    """

    if value is None:
        return "", False
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        rendered = repr(value)
    return head_tail_truncate(rendered, _MAX_VALUE_CHARS)


__all__ = [
    "MAX_OBSERVATION_CHARS",
    "MAX_PROPOSAL_REPAIR_ATTEMPTS",
    "OBSERVATION_HEAD_RATIO",
    "REJECTION_MESSAGES",
    "REPAIR_HINTS",
    "describe_rejection",
    "format_execution_observation",
    "head_tail_truncate",
    "repair_prompt",
]
