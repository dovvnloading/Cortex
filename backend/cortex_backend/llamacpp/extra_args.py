"""The advanced ``llama-server`` options a user may add, and how they are checked.

Cortex owns the launch contract: the model, the context window, the loopback
host, the ephemeral port, the API key (which travels in the environment), the
GPU-layer choice and the slot count. A user-chosen option list is appended
after that contract, so it is held to a short allow-list of tuning flags that
change how the model is run and nothing else -- the KV-cache element types,
flash attention and the CPU thread counts. Anything else is refused, and the
flags that would change what the contract guarantees are refused by name so the
error can say why.

Only the shape of each option is checked here. The flag spellings are the
runtime's own and are passed through as written, so a value the pinned build
does not understand fails at launch like any other bad argument.

Error messages name the position of the offending option and never repeat what
was typed: they travel back through the settings API.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

MAX_EXTRA_ARGS = 12
_MAX_ARG_CHARS = 32
_MAX_THREADS = 1024

_CACHE_TYPES = frozenset(
    {"f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "iq4_nl", "q5_0", "q5_1"}
)
_FLASH_ATTENTION_MODES = frozenset({"on", "off", "auto"})


@dataclass(frozen=True, slots=True)
class _Option:
    """One allowed flag: every spelling, and what may follow it."""

    spellings: frozenset[str]
    choices: frozenset[str] | None = None
    # A whole number between 1 and this, instead of a choice.
    maximum: int | None = None
    # The value may be left out (older builds take ``-fa`` as a bare switch,
    # newer ones ``-fa on``); a following token that is not a flag or a
    # recognised value is still refused.
    value_optional: bool = False


_OPTIONS = (
    _Option(frozenset({"-ctk", "--cache-type-k"}), choices=_CACHE_TYPES),
    _Option(frozenset({"-ctv", "--cache-type-v"}), choices=_CACHE_TYPES),
    _Option(
        frozenset({"-fa", "--flash-attn"}),
        choices=_FLASH_ATTENTION_MODES,
        value_optional=True,
    ),
    _Option(frozenset({"-t", "--threads"}), maximum=_MAX_THREADS),
    _Option(frozenset({"-tb", "--threads-batch"}), maximum=_MAX_THREADS),
)
_BY_SPELLING = {spelling: option for option in _OPTIONS for spelling in option.spellings}

# Refused with an explanation: Cortex sets each of these itself, and letting one
# through would change the address the runtime listens on, how requests are
# authenticated, which model is loaded, or how much memory it is given.
_MANAGED_BY_CORTEX = frozenset(
    {
        "-m", "--model", "-mu", "--model-url", "-hf", "-hfr", "--hf-repo", "-hff", "--hf-file",
        "-c", "--ctx-size",
        "--host", "--port", "--path", "--api-prefix", "--reuse-port",
        "--api-key", "--api-key-file", "--ssl-key-file", "--ssl-cert-file",
        "-ngl", "--gpu-layers", "--n-gpu-layers",
        "-np", "--parallel",
        "--ui", "--no-ui", "--webui", "--no-webui", "--reasoning-format",
    }
)


def _describe_problem(index: int, token: str, following: str | None) -> str | None:
    """Why the token at ``index`` cannot stand, or ``None`` if it can."""
    place = f"option {index + 1}"
    if token in _MANAGED_BY_CORTEX:
        return f"{place} is set by Cortex and cannot be changed"
    if token not in _BY_SPELLING:
        return f"{place} is not an allowed runtime option"
    option = _BY_SPELLING[token]
    if following is None or following.startswith("-"):
        if option.value_optional:
            return None
        return f"{place} needs a value"
    if option.maximum is not None:
        if not (following.isascii() and following.isdigit()) or not 1 <= int(following) <= option.maximum:
            return f"the value after option {index + 1} must be a whole number from 1 to {option.maximum}"
        return None
    if option.choices is not None and following not in option.choices:
        return f"the value after option {index + 1} is not one this runtime option accepts"
    return None


def validate_extra_args(values: Iterable[str]) -> tuple[str, ...]:
    """Return ``values`` as a tuple if every option is allowed, else raise ``ValueError``.

    A flag that takes a value is followed by exactly that value, each flag
    appears at most once (under any of its spellings), and the list is short.
    """
    tokens = tuple(values)
    if len(tokens) > MAX_EXTRA_ARGS:
        raise ValueError(f"at most {MAX_EXTRA_ARGS} runtime option words are allowed")
    # Every word, value or flag, is one short plain word before anything is
    # read into its meaning.
    for position, word in enumerate(tokens, start=1):
        if not isinstance(word, str) or not word or len(word) > _MAX_ARG_CHARS or not word.isascii():
            raise ValueError(f"option {position} is not a valid runtime option word")
        if any(character.isspace() or not character.isprintable() for character in word):
            raise ValueError(f"option {position} must be a single word")
    seen: set[_Option] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        problem = _describe_problem(index, token, following)
        if problem is not None:
            raise ValueError(problem)
        option = _BY_SPELLING[token]
        if option in seen:
            raise ValueError(f"option {index + 1} repeats a runtime option")
        seen.add(option)
        takes_value = following is not None and not following.startswith("-")
        index += 2 if takes_value else 1
    return tokens
