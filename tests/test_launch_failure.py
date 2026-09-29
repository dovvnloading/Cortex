"""The closed set of launch-failure causes, how output and exit codes select one,
and the fixed text each cause is reported with."""

from __future__ import annotations

from typing import get_args

import pytest

from cortex_backend.llamacpp.errors import ServerLaunchError, ServerStartTimeoutError
from cortex_backend.llamacpp.launch_failure import (
    LAUNCH_FAILURE_CODES,
    LaunchFailureCode,
    classify_child_exit,
    crash_loop_message,
    launch_failure_message,
)
from cortex_backend.services.llm import _generation_failure_message


def test_every_cause_has_its_own_message_and_the_set_is_the_contract() -> None:
    assert set(LAUNCH_FAILURE_CODES) == set(get_args(LaunchFailureCode))
    messages = [launch_failure_message(code) for code in LAUNCH_FAILURE_CODES]
    assert len(set(messages)) == len(messages)
    for message in messages:
        assert message.endswith(".")
        # Fixed text: nothing to interpolate a path, a prompt or a model name into.
        assert "{" not in message and "\\" not in message and "%s" not in message


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ("llama_model_load: error loading model architecture: unknown model architecture: 'newarch'", "unsupported_architecture"),
        ("llama_model_load: error loading model architecture: unknown model architecture: 'clip'", "projector_not_a_model"),
        ("clip_init: failed to load mmproj file", "projector_not_a_model"),
        ("llama_model_load: error loading model: illegal split file idx: 1 (file: model-00002-of-00003.gguf)", "missing_shards"),
        ("gguf_init_from_file_impl: failed to open GGUF file 'model-00002-of-00003.gguf'", "missing_shards"),
        ("llama_model_loader: error: missing shard 2 of 3", "missing_shards"),
        ("ggml_backend_cuda_buffer_type_alloc_buffer: allocating 9000.00 MiB on device 0: cudaMalloc failed: out of memory", "memory"),
        ("llama_kv_cache: failed to allocate buffer for kv cache", "memory"),
        ("ggml_vulkan: Device memory allocation of size 1234567 failed.", "memory"),
        ("vk::Device::allocateMemory: ErrorOutOfDeviceMemory", "memory"),
        ("terminate called after throwing an instance of 'std::bad_alloc'", "memory"),
        ("ggml_vulkan: No devices found.", "no_gpu"),
        ("vk::createInstance: ErrorIncompatibleDriver", "no_gpu"),
        ("ggml_vulkan: Failed to initialize Vulkan instance", "no_gpu"),
        ("srv  operator(): couldn't bind HTTP server socket, hostname: 127.0.0.1, port: 0", "port_unavailable"),
        ("Only one usage of each socket address (protocol/network address/port) is normally permitted", "port_unavailable"),
        ("llama_model_load: error loading model: tensor 'blk.3.ffn_up.weight' data is not within the file bounds, model is corrupted or incomplete", "model_unreadable"),
        ("llama_model_load_from_file_impl: failed to load model", "model_unreadable"),
        ("gguf_init_from_file_impl: invalid magic characters: 'HTML'", "model_unreadable"),
    ],
)
def test_each_known_signature_selects_its_cause(line: str, code: str) -> None:
    assert classify_child_exit([line], 1) == code


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["0.01.234.567 I srv         start: listening on http://127.0.0.1:43125"],
        ["something entirely unrelated happened"],
        # Ordinary words that merely sit near a signature do not select a cause.
        ["load_tensors: offloading 12 layers to GPU", "llama_context: n_ctx = 4096"],
    ],
)
def test_output_that_explains_nothing_is_not_guessed_at(lines: list[str]) -> None:
    assert classify_child_exit(lines, 1) == "runtime_exited"
    assert classify_child_exit(lines, None) == "runtime_exited"


def test_the_more_specific_cause_wins_over_the_generic_load_failure_that_follows_it() -> None:
    # An out-of-memory failure ends in the same generic line an unreadable file does.
    assert classify_child_exit(
        ["ggml: failed to allocate buffer", "llama_model_load_from_file_impl: failed to load model"], 1
    ) == "memory"
    # A projector chosen as a model reports an unknown architecture named clip.
    assert classify_child_exit(
        ["error loading model architecture: unknown model architecture: 'clip'", "failed to load model"], 1
    ) == "projector_not_a_model"
    # The order the lines arrive in does not change the answer.
    assert classify_child_exit(
        ["failed to load model", "ggml: failed to allocate buffer"], 1
    ) == "memory"


def test_the_name_of_a_split_models_part_does_not_by_itself_make_a_part_missing() -> None:
    """Every line about a split model names its parts, so the name proves nothing."""
    naming_the_part = "common_init_from_params: failed to load model 'C:/m/big-00001-of-00003.gguf'"

    assert classify_child_exit([naming_the_part], 1) == "model_unreadable"
    # A failed allocation happens only once every part was found.
    assert classify_child_exit(["ggml: failed to allocate buffer", naming_the_part], 1) == "memory"
    # Failing to open a part is what identifies a missing one.
    assert classify_child_exit(
        ["gguf_init_from_file_impl: failed to open GGUF file 'C:/m/big-00002-of-00003.gguf'", naming_the_part], 1
    ) == "missing_shards"


def test_text_a_model_file_echoes_into_the_log_cannot_choose_the_cause() -> None:
    """The metadata block prints strings the model's author wrote."""
    lines = [
        "llama_model_loader: - kv   3: general.description str = this model ran out of memory: out of memory",
        "print_info: general.name = unknown model architecture: 'x'",
        "llama_model_loader: - kv   9: tokenizer.chat_template str = failed to allocate",
        "llama_model_load: error loading model: tensor data is not within the file bounds",
    ]

    assert classify_child_exit(lines, 1) == "model_unreadable"


@pytest.mark.parametrize(
    ("exit_code", "code"),
    [
        (0xC0000135, "runtime_unusable"),  # a required DLL is missing
        (-1073741515, "runtime_unusable"),  # the same code, signed
        (0xC000007B, "runtime_unusable"),  # a DLL of the wrong architecture
        (0xC0000139, "runtime_unusable"),
        (0xC0000142, "runtime_unusable"),
        (0xC0000017, "memory"),  # no memory
        (0xC000012D, "memory"),  # commit limit reached
        (-1073741819, "runtime_exited"),  # an access violation says nothing about why
        (1, "runtime_exited"),
        (0, "runtime_exited"),
    ],
)
def test_a_windows_exit_code_identifies_the_cause_when_there_is_no_output(exit_code: int, code: str) -> None:
    assert classify_child_exit([], exit_code) == code


def test_what_the_child_printed_outranks_its_exit_code() -> None:
    assert classify_child_exit(["ggml_vulkan: No devices found."], 0xC0000135) == "no_gpu"


def test_the_crash_loop_refusal_states_the_count_the_last_reason_and_the_cause() -> None:
    message = crash_loop_message(3, "the runtime exited before it became ready", "missing_shards")

    assert message.startswith("The local model runtime failed 3 times in the last few minutes")
    assert "(most recently: the runtime exited before it became ready)" in message
    assert launch_failure_message("missing_shards") in message
    assert "Cortex tries again when you change the model or the context window" in message
    assert "memory" not in message


def test_a_crash_loop_with_no_identified_cause_says_so() -> None:
    assert launch_failure_message("runtime_exited") in crash_loop_message(3, "the runtime exited", None)


@pytest.mark.parametrize("error_type", [ServerLaunchError, ServerStartTimeoutError])
@pytest.mark.parametrize("code", LAUNCH_FAILURE_CODES)
def test_a_classified_launch_error_is_guidance_that_reaches_the_user_unchanged(
    error_type: type[ServerLaunchError] | type[ServerStartTimeoutError], code: LaunchFailureCode
) -> None:
    error = error_type(failure_code=code)

    message, details = _generation_failure_message(error)

    assert message == launch_failure_message(code)
    assert details == f"llamacpp_{code}"
    assert error.user_message == launch_failure_message(code)
    assert error.failure_code == code


def test_an_unclassified_launch_error_is_not_mistaken_for_guidance() -> None:
    error = ServerLaunchError("The local model runtime could not start.")

    assert error.failure_code is None
    assert not getattr(error, "is_user_guidance", False)
    message, _details = _generation_failure_message(error)
    assert message != launch_failure_message("runtime_exited")


def test_a_launch_error_needs_either_a_message_or_a_cause() -> None:
    with pytest.raises(TypeError):
        ServerLaunchError()
