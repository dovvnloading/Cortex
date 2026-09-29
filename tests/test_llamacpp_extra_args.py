"""The advanced runtime options a user may add, and what they may not.

Cortex owns the launch contract (model, context window, loopback address, port,
key, GPU layers, slots). What a user adds goes after it, so it is held to an
allow-list of tuning flags, and the settings model refuses anything else before
it is stored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cortex_backend.core.settings import CortexSettings, LlamaCppSettings
from cortex_backend.llamacpp.extra_args import MAX_EXTRA_ARGS, validate_extra_args


@pytest.mark.parametrize(
    "options",
    [
        (),
        ("-ctk", "q8_0"),
        ("--cache-type-k", "f16", "--cache-type-v", "q4_0"),
        ("-ctk", "bf16", "-ctv", "iq4_nl"),
        ("-fa", "on"),
        ("--flash-attn", "off"),
        ("-fa", "auto", "-t", "8"),
        # Older builds take the flag alone.
        ("-fa",),
        ("-fa", "-t", "8"),
        ("-t", "16", "-tb", "16"),
        ("--threads", "1", "--threads-batch", "1024"),
    ],
)
def test_the_tuning_flags_are_accepted_as_written(options: tuple[str, ...]) -> None:
    assert validate_extra_args(options) == options


@pytest.mark.parametrize(
    "options",
    [
        # Each of these would change something Cortex sets itself.
        ("--host", "0.0.0.0"),
        ("--port", "8080"),
        ("--api-key", "not-a-real-key"),
        ("--api-key-file", "keys.txt"),
        ("-m", "other.gguf"),
        ("--model", "other.gguf"),
        ("-hf", "someone/repo"),
        ("-c", "131072"),
        ("--ctx-size", "131072"),
        ("-ngl", "99"),
        ("--n-gpu-layers", "0"),
        ("-np", "8"),
        ("--webui",),
        ("--ui",),
        ("--no-ui",),
        ("--reasoning-format", "none"),
        ("--ssl-key-file", "k.pem"),
        # And anything else that is not on the list.
        ("--unknown-flag",),
        ("--mlock",),
        ("--no-mmap",),
        ("-ctk", "q8_0", "--host", "0.0.0.0"),
        ("--host=0.0.0.0",),
        ("--cache-type-k=q8_0",),
        ("q8_0",),
    ],
)
def test_anything_that_could_change_the_launch_contract_is_refused(options: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        validate_extra_args(options)


@pytest.mark.parametrize(
    "options",
    [
        ("-ctk",),
        ("-ctk", "q9_9"),
        ("-ctk", "Q8_0"),
        ("-ctv", "-t"),
        ("-fa", "maybe"),
        ("-t",),
        ("-t", "0"),
        ("-t", "-4"),
        ("-t", "1025"),
        ("-t", "4.5"),
        ("-t", "four"),
        ("-t", "٤"),  # an Arabic-Indic digit is not an ASCII one
        ("-t", "4", "4"),
        ("-ctk", "q8_0", "-ctk", "f16"),
        ("-ctk", "q8_0", "--cache-type-k", "f16"),
        ("-t", "4", "--threads", "8"),
        ("-fa", "-fa"),
    ],
)
def test_a_flag_needs_exactly_its_own_value_and_appears_once(options: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        validate_extra_args(options)


@pytest.mark.parametrize(
    "options",
    [
        ("",),
        (" ",),
        ("-t ", "4"),
        ("-t", "4 "),
        ("-t\n", "4"),
        ("-ctk\t", "q8_0"),
        ("-‑t", "4"),
        ("-t", "4" * 40),
        ("-" + "x" * 40,),
    ],
)
def test_words_that_are_not_single_plain_words_are_refused(options: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        validate_extra_args(options)


def test_the_list_is_bounded() -> None:
    everything = ("-ctk", "q8_0", "-ctv", "q8_0", "-fa", "on", "-t", "8", "-tb", "8")
    assert len(everything) <= MAX_EXTRA_ARGS
    assert validate_extra_args(everything) == everything
    with pytest.raises(ValueError, match="at most"):
        validate_extra_args(("-fa",) * (MAX_EXTRA_ARGS + 1))


def test_a_refusal_names_the_position_and_never_repeats_what_was_typed() -> None:
    secretive = ("-t", "4", "--api-key", "hunter2-not-a-real-key", "--host", "10.9.8.7")
    with pytest.raises(ValueError) as refused:
        validate_extra_args(secretive)

    message = str(refused.value)
    assert "option 3" in message
    for typed in ("hunter2-not-a-real-key", "10.9.8.7", "--api-key"):
        assert typed not in message


def test_settings_default_to_no_options() -> None:
    assert CortexSettings().llamacpp.extra_args == ()


def test_a_stored_document_from_before_this_field_still_loads_with_no_options() -> None:
    settings = CortexSettings.model_validate({"llamacpp": {"gpu_backend": "cpu"}})

    assert settings.llamacpp.gpu_backend == "cpu"
    assert settings.llamacpp.extra_args == ()


def test_settings_accept_a_list_from_json_and_keep_it_as_a_tuple() -> None:
    settings = LlamaCppSettings.model_validate({"extra_args": ["-ctk", "q8_0", "-fa", "on"]})

    assert settings.extra_args == ("-ctk", "q8_0", "-fa", "on")


@pytest.mark.parametrize("options", [("--host", "0.0.0.0"), ("--api-key", "x"), ("-c", "1"), ("-t",)])
def test_settings_reject_options_the_allow_list_does_not_cover(options: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        LlamaCppSettings(extra_args=options)


def test_the_settings_api_refuses_a_host_override_without_echoing_it(client, headers) -> None:
    current = client.get("/api/v1/settings", headers=headers).json()["settings"]
    current["llamacpp"] = {**current.get("llamacpp", {}), "extra_args": ["--host", "203.0.113.9"]}

    response = client.put("/api/v1/settings", headers=headers, json={"settings": current})

    assert response.status_code == 422
    assert "203.0.113.9" not in response.text
    stored = client.get("/api/v1/settings", headers=headers).json()["settings"]
    assert stored["llamacpp"].get("extra_args", []) == []


def test_the_settings_api_round_trips_the_advanced_options(client, headers) -> None:
    current = client.get("/api/v1/settings", headers=headers).json()["settings"]
    current["llamacpp"] = {**current.get("llamacpp", {}), "extra_args": ["-ctk", "q8_0", "-t", "6"]}

    saved = client.put("/api/v1/settings", headers=headers, json={"settings": current})

    assert saved.status_code == 200
    llamacpp = client.get("/api/v1/settings", headers=headers).json()["settings"]["llamacpp"]
    assert llamacpp["extra_args"] == ["-ctk", "q8_0", "-t", "6"]
