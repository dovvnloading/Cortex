"""Name why the local model runtime failed to start, without repeating what it said.

A launch that does not reach "ready" used to end in one sentence, and the
crash-loop guard then blamed available memory for every one of them. Bad
architecture, damaged or truncated files, a split set with a part missing, a
projector chosen as a model, a blocked program and a machine with no Vulkan
device are different problems with different fixes.

llama-server's output is untrusted and can contain prompts, paths and model
metadata, so it is never shown, logged or stored. It is only *matched*, against
the fixed allow-list below, and the result is one of a closed set of codes. The
text a user reads is written here, per code, and contains nothing taken from the
child. The one thing that could still steer the outcome is text a model file
echoes into the log; the lines that print model metadata are skipped for that
reason, and the worst remaining effect of a hostile file is a wrong (but still
fixed) message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final, Literal

LaunchFailureCode = Literal[
    "unsupported_architecture",
    "model_unreadable",
    "memory",
    "missing_shards",
    "projector_not_a_model",
    "no_gpu",
    "port_unavailable",
    "runtime_unusable",
    "startup_timeout",
    "health_check_failed",
    "runtime_exited",
]

# Each message names the cause and the change that fixes it. None interpolates
# anything the child produced.
_MESSAGES: Final[dict[LaunchFailureCode, str]] = {
    "unsupported_architecture": (
        "This model needs a newer local runtime than this version of Cortex "
        "includes. Update Cortex, or choose a different model."
    ),
    "model_unreadable": (
        "The model file could not be read. It may be incomplete or damaged. "
        "Download it again, or choose a different model."
    ),
    "memory": (
        "The model does not fit in available memory. Choose a smaller model or "
        "quantization, or lower the context window in Settings."
    ),
    "missing_shards": (
        "This model is split into several files and at least one is missing. "
        "Put every part in the same folder, or download the complete set."
    ),
    "projector_not_a_model": (
        "This file is a vision projector that goes with a model, not a model "
        "itself. Choose the main model file instead."
    ),
    "no_gpu": (
        "The local model runtime could not use a graphics device (Vulkan). "
        "Update the graphics driver, or set the GPU backend to CPU in System "
        "settings."
    ),
    "port_unavailable": (
        "The local model runtime could not open a local network port. Security "
        "software or another program may be blocking it. Close it and try again."
    ),
    "runtime_unusable": (
        "The local model runtime program could not be started. A file it needs "
        "may be missing, or security software may have blocked or removed it. "
        "Check your antivirus, then try again."
    ),
    "startup_timeout": (
        "The model took too long to load. A slow or busy disk, a USB or network "
        "drive, or a very large file can cause this. Try again, or move the "
        "model to a faster drive."
    ),
    "health_check_failed": (
        "The local model runtime is running but did not pass Cortex's health "
        "check. Try again; if it keeps happening, restart Cortex."
    ),
    "runtime_exited": (
        "The local model runtime stopped or failed to start, and Cortex could "
        "not tell why. It may not fit in available memory, or the model file "
        "may be damaged. Try a smaller model or a lower context window, or "
        "download the file again."
    ),
}

LAUNCH_FAILURE_CODES: Final[tuple[LaunchFailureCode, ...]] = tuple(_MESSAGES)


def launch_failure_message(code: LaunchFailureCode) -> str:
    """The user-facing explanation for a cause: what happened and what to change."""
    return _MESSAGES[code]


def crash_loop_message(count: int, restart_reason: str, code: LaunchFailureCode | None) -> str:
    """The refusal shown once the same configuration has failed repeatedly.

    ``restart_reason`` must already be one of the fixed phrases from
    ``_safe_restart_reason``. A failure whose cause was not identified gets the
    "could not tell why" text, not a guess.
    """
    advice = launch_failure_message(code or "runtime_exited")
    return (
        f"The local model runtime failed {count} times in the last few minutes "
        f"(most recently: {restart_reason}). {advice} Cortex tries again when "
        "you change the model or the context window, or after a few minutes."
    )


def _rule(code: LaunchFailureCode, *patterns: str) -> tuple[LaunchFailureCode, re.Pattern[str]]:
    return code, re.compile("|".join(f"(?:{pattern})" for pattern in patterns), re.IGNORECASE)


# Rules that relate two parts of a line allow only a bounded gap between them,
# and lines are cut before matching, so a hostile file that makes the child
# print an enormous, repetitive line cannot make the match itself expensive.
_MAX_CLASSIFIED_LINE_CHARS = 512
_GAP = r".{0,160}"
_ERROR_WORDS = r"(?:error|failed|unable|cannot|can't|invalid|no such|not found|missing)"
# A shard's file name appears in nearly every line about a split model, so a
# name alone (or any "failed") says nothing about a missing part. Only a failure
# to open or find the file does.
_OPEN_FAILURES = r"(?:failed to open|cannot open|could not open|unable to open|no such file|not found|missing)"

# Checked in this order, and the first cause with any matching line wins. The
# order matters: an out-of-memory failure ends in a generic "failed to load
# model" line, an mmproj file loaded as a model reports an unknown architecture
# named "clip", and a missing part of a split set surfaces as a failure to
# open a file. Each specific cause therefore precedes the more general one.
# Memory precedes missing parts because an allocation can only fail once every
# part was found, and the failed load of a split model names its parts too.
_OUTPUT_RULES: Final[tuple[tuple[LaunchFailureCode, re.Pattern[str]], ...]] = (
    _rule(
        "projector_not_a_model",
        r"unknown model architecture:?\s*['\"]?clip\b",
        rf"\bmmproj\b{_GAP}\b{_ERROR_WORDS}\b",
        rf"\b{_ERROR_WORDS}\b{_GAP}\bmmproj\b",
    ),
    _rule(
        "unsupported_architecture",
        r"unknown model architecture",
        r"error loading model architecture",
        r"unsupported (?:model )?architecture",
    ),
    _rule(
        "memory",
        r"out of memory",
        r"failed to allocate",
        r"unable to allocate",
        r"cannot allocate",
        r"not enough memory",
        r"insufficient memory",
        r"bad_alloc",
        r"erroroutof(?:device|host)memory",
        r"memory allocation of size \d+ failed",
    ),
    _rule(
        "missing_shards",
        r"\b(?:illegal|invalid|missing)\s+split\b",
        r"\bsplit file\b",
        r"\bmissing\s+(?:a\s+)?shard",
        rf"-\d{{5}}-of-\d{{5}}{_GAP}\b{_OPEN_FAILURES}\b",
        rf"\b{_OPEN_FAILURES}\b{_GAP}-\d{{5}}-of-\d{{5}}",
    ),
    _rule(
        "no_gpu",
        r"no devices found",
        r"no vulkan",
        r"vkcreateinstance",
        r"vk::createinstance",
        r"vulkan.{0,60}\b(?:initiali[sz]ation|instance)\b.{0,20}\b(?:failed|error)\b",
        r"failed to (?:create|initiali[sz]e).{0,60}vulkan",
        r"error(?:incompatibledriver|initializationfailed|devicelost|extensionnotpresent|featurenotpresent|layernotpresent)",
    ),
    _rule(
        "port_unavailable",
        r"couldn'?t bind",
        r"failed to bind",
        r"address already in use",
        r"only one usage of each socket address",
        r"wsaeaddrinuse",
    ),
    _rule(
        "model_unreadable",
        r"error loading model",
        r"failed to load model",
        r"unable to load model",
        r"invalid magic",
        r"bad magic",
        r"failed to open gguf",
        r"gguf_init_from_file",
        r"not within the file bounds",
        r"\b(?:corrupt|truncated)\b",
        r"unexpected end of file",
        r"not a valid gguf",
    ),
)

# Lines where llama.cpp prints what the model file itself says about itself
# (its name, description, chat template, ...). That text is chosen by whoever
# made the file, so it must not be able to pick a cause.
_ECHOED_MODEL_TEXT: Final = re.compile(r"\bkv\s+\d+\s*:|\bprint_info\s*:|\bgeneral\.[a-z_.]+\s*=", re.IGNORECASE)


def echoes_model_text(line: str) -> bool:
    """Whether ``line`` prints what the model file says about itself.

    Anything read from such a line is chosen by whoever made the file, so it
    must not be used to report how the runtime behaved.
    """
    return _ECHOED_MODEL_TEXT.search(line) is not None

# Windows exit codes (NTSTATUS) that identify the cause without any output,
# which is how a process that cannot even load its libraries ends. Keys are the
# unsigned 32-bit values; a signed exit code is masked to match.
_EXIT_CODE_CAUSES: Final[dict[int, LaunchFailureCode]] = {
    0xC0000135: "runtime_unusable",  # STATUS_DLL_NOT_FOUND
    0xC0000139: "runtime_unusable",  # STATUS_ENTRYPOINT_NOT_FOUND
    0xC000007B: "runtime_unusable",  # STATUS_INVALID_IMAGE_FORMAT
    0xC0000142: "runtime_unusable",  # STATUS_DLL_INIT_FAILED
    0xC0000017: "memory",  # STATUS_NO_MEMORY
    0xC000012D: "memory",  # STATUS_COMMITMENT_LIMIT
}


def classify_child_exit(lines: Iterable[str], exit_code: int | None) -> LaunchFailureCode:
    """The most specific known cause of a child that exited instead of serving.

    ``lines`` is the retained tail of its output; ``exit_code`` may be None when
    it is unknown. Returns ``runtime_exited`` when nothing identifies the cause,
    so the caller never has to guess one.
    """
    candidates = [
        line
        for line in (raw[:_MAX_CLASSIFIED_LINE_CHARS] for raw in lines)
        if not _ECHOED_MODEL_TEXT.search(line)
    ]
    for code, pattern in _OUTPUT_RULES:
        if any(pattern.search(line) for line in candidates):
            return code
    if exit_code is not None:
        by_exit_code = _EXIT_CODE_CAUSES.get(exit_code & 0xFFFFFFFF)
        if by_exit_code is not None:
            return by_exit_code
    return "runtime_exited"
