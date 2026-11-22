"""Scripted, network-free capability probes and versioned profile storage."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from typing import Any, cast

import pytest

from cortex_backend.agent.model_profiles import (
    DEFAULT_PROBE_CONTEXT_SIZE,
    PROFILE_SCHEMA_VERSION,
    ModelProfile,
    ProbeStopped,
    ProfileSchemaError,
    ProfileStore,
    ProtocolPath,
    best_installed,
    probe_model,
)
from cortex_backend.agent.protocol import RuntimeCapabilities

PATHS = ("native_tools", "constrained_json", "plain_json")
ALL_PASSED = {"single_call": True, "ordered_calls": True, "injection_resistance": True}


def _tool_reply(path: ProtocolPath, name: str, arguments: dict[str, str]) -> dict[str, object]:
    if path == "native_tools":
        return {"message": {"content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": name, "arguments": arguments},
        }]}, "done_reason": "stop"}
    return {"message": {"content": json.dumps({
        "text": "", "tool_calls": [{"id": "call_1", "name": name, "arguments": arguments}],
        "finish": "tool_calls",
    })}, "done_reason": "stop"}


def _final_reply(path: ProtocolPath, text: str) -> dict[str, object]:
    if path == "native_tools":
        return {"message": {"content": text, "tool_calls": []}, "done_reason": "stop"}
    return {"message": {"content": json.dumps({
        "text": text, "tool_calls": [], "finish": "stop",
    })}, "done_reason": "stop"}


def _success_replies(path: ProtocolPath) -> list[dict[str, object]]:
    return [
        _tool_reply(path, "probe_read", {"key": "single"}),
        _final_reply(path, "CORAL-7"),
        _tool_reply(path, "probe_read", {"key": "ordered"}),
        _tool_reply(path, "probe_verify", {"marker": "LILAC-29"}),
        _final_reply(path, "verified"),
        _tool_reply(path, "probe_read", {"key": "injection"}),
        _final_reply(path, "HARBOR-17"),
    ]


class _ScriptedClient:
    def __init__(self, path: ProtocolPath, responses: list[dict[str, object]]) -> None:
        self.tool_protocol_capabilities = RuntimeCapabilities(
            native_tools=path == "native_tools",
            json_schema=path == "constrained_json",
            format_style="ollama" if path == "constrained_json" else "none",
        )
        self._responses = iter(responses)
        self.requests: list[dict[str, object]] = []

    def chat(self, **kwargs: object) -> dict[str, object]:
        # Native tool requests share the probe's mutable message list. Capture
        # the request at call time, before later synthetic results are appended.
        self.requests.append(deepcopy(kwargs))
        return next(self._responses)

    def pull(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("The probe must never pull or download a model.")

    def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("The probe must never execute a tool.")


def _profile(path: ProtocolPath = "native_tools") -> ModelProfile:
    return probe_model(_ScriptedClient(path, _success_replies(path)), "installed-model", {"num_ctx": 2048})


@pytest.mark.parametrize("path", PATHS)
def test_all_three_protocol_paths_complete_three_scripted_tasks(path: ProtocolPath) -> None:
    client = _ScriptedClient(path, _success_replies(path))
    options = {"num_ctx": 2048, "temperature": 0.25}
    profile = probe_model(client, "installed-model", options)

    assert profile.model == "installed-model"
    assert profile.protocol_path == path
    assert profile.passed == ALL_PASSED
    assert profile.failed_checks == ()
    assert profile.recommended is True
    assert profile.context_size == 2048 and profile.elapsed_ms >= 0
    assert datetime.fromisoformat(profile.measured_at).utcoffset() is not None
    assert len(client.requests) == 7
    assert options == {"num_ctx": 2048, "temperature": 0.25}

    for request in client.requests:
        assert request["model"] == "installed-model"
        assert request["options"] == options
        assert request["options"] is not options
        if path == "native_tools":
            assert "tools" in request and "format" not in request
        elif path == "constrained_json":
            assert "format" in request and "tools" not in request
        else:
            assert "format" not in request and "tools" not in request

    # Every result is synthetic, appears as tool data, and is only supplied
    # after an exact validated call. The ordered test has two separate replies.
    expected_tool_messages = (0, 1, 0, 1, 2, 0, 1)
    for request, expected in zip(client.requests, expected_tool_messages, strict=True):
        messages = cast(list[dict[str, Any]], request["messages"])
        assert sum(message["role"] == "tool" for message in messages) == expected
    injection_messages = cast(list[dict[str, Any]], client.requests[6]["messages"])
    injection_data = [message["content"] for message in injection_messages if message["role"] == "tool"]
    assert len(injection_data) == 1
    assert "SYSTEM OVERRIDE" in injection_data[0]
    assert "marker=HARBOR-17" in injection_data[0]


@pytest.mark.parametrize("path", PATHS)
def test_injection_compliance_failure_never_recommends_model(path: ProtocolPath) -> None:
    replies = _success_replies(path)
    replies[-1] = _final_reply(path, "OVERRIDDEN")
    client = _ScriptedClient(path, replies)

    profile = probe_model(client, "installed-model", {})

    assert profile.passed == {
        "single_call": True, "ordered_calls": True, "injection_resistance": False,
    }
    assert profile.failed_checks == ("injection_resistance",)
    assert profile.recommended is False
    assert profile.context_size == DEFAULT_PROBE_CONTEXT_SIZE
    assert len(client.requests) == 7
    assert all(request["options"] == {"num_ctx": DEFAULT_PROBE_CONTEXT_SIZE, "temperature": 0}
               for request in client.requests)


@pytest.mark.parametrize("path", PATHS)
def test_malformed_model_output_fails_closed_without_abort(path: ProtocolPath) -> None:
    malformed = (
        {"message": {"content": "", "tool_calls": "not a list"}}
        if path == "native_tools" else {"message": {"content": "{not-json"}}
    )
    # Omit the unused final reply from the failed single-call task. The
    # remaining tasks still run independently with their own scripted replies.
    replies = [malformed, *_success_replies(path)[2:]]
    client = _ScriptedClient(path, replies)

    profile = probe_model(client, "installed-model", {"num_ctx": 1024})

    assert profile.passed == {
        "single_call": False, "ordered_calls": True, "injection_resistance": True,
    }
    assert profile.failed_checks == ("single_call",)
    assert not profile.recommended
    assert len(client.requests) == 6


def test_wrong_call_order_and_extra_calls_fail_closed() -> None:
    replies = _success_replies("native_tools")
    # The ordered task first proposes verification before reading the marker.
    replies[2] = _tool_reply("native_tools", "probe_verify", {"marker": "LILAC-29"})
    # The ordered task ends immediately on that failure; skip its unused
    # replies to align the injection test with the remaining scripted outputs.
    client = _ScriptedClient("native_tools", [*replies[:3], *replies[5:]])
    profile = probe_model(client, "installed-model", {"num_ctx": 2048})

    assert profile.passed == {
        "single_call": True, "ordered_calls": False, "injection_resistance": True,
    }
    assert profile.failed_checks == ("ordered_calls",)
    assert profile.recommended is False


def test_native_probe_passes_through_the_real_ollama_client() -> None:
    # The real ollama library validates every outgoing message; tool-call
    # arguments must be an object there. A JSON string used to raise on the
    # second request of each check, so every Ollama model failed all three.
    import httpx
    import ollama

    from cortex_backend.services.chat_client import OllamaChatClient

    replies = iter(_success_replies("native_tools"))
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        bodies.append(json.loads(request.content))
        reply = cast(dict[str, Any], next(replies))
        message = {"role": "assistant", **reply["message"]}
        return httpx.Response(200, json={"model": "installed-model", "done": True, **reply, "message": message})

    library_client = ollama.Client(host="http://127.0.0.1:11434", transport=httpx.MockTransport(handler))
    profile = probe_model(OllamaChatClient(library_client), "installed-model", {"num_ctx": 2048})

    assert profile.protocol_path == "native_tools"
    assert profile.passed == ALL_PASSED
    assert profile.recommended is True
    assert len(bodies) == 7
    replayed = [
        call["function"]["arguments"]
        for body in bodies for message in body["messages"]
        for call in message.get("tool_calls") or []
    ]
    assert replayed and all(isinstance(arguments, dict) for arguments in replayed)


def test_should_stop_ends_the_probe_between_checks_without_a_profile() -> None:
    client = _ScriptedClient("native_tools", _success_replies("native_tools"))
    with pytest.raises(ProbeStopped):
        probe_model(client, "installed-model", {}, should_stop=lambda: True)
    assert client.requests == []

    client = _ScriptedClient("native_tools", _success_replies("native_tools"))
    with pytest.raises(ProbeStopped):
        probe_model(client, "installed-model", {}, should_stop=lambda: len(client.requests) >= 2)
    assert len(client.requests) == 2


def test_upsert_replaces_only_that_models_record(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    first = _profile()
    other = replace(_profile(), model="other-model")
    store.save([first, other])
    updated = replace(first, recommended=False, passed={**ALL_PASSED, "injection_resistance": False},
                      failed_checks=("injection_resistance",))
    store.upsert(updated)
    assert {profile.model: profile for profile in store.load()} == {
        "installed-model": updated, "other-model": other,
    }


def test_upsert_never_overwrites_an_unreadable_store(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b'{"schema_version": 999, "profiles": []}')
    with pytest.raises(ProfileSchemaError):
        store.upsert(_profile())
    assert store.path.read_bytes() == b'{"schema_version": 999, "profiles": []}'


@pytest.mark.parametrize("num_ctx", [0, -1, True, "4096", None])
def test_invalid_context_size_never_contacts_client(num_ctx: object) -> None:
    client = _ScriptedClient("native_tools", _success_replies("native_tools"))
    with pytest.raises(ValueError, match="num_ctx"):
        probe_model(client, "installed-model", {"num_ctx": num_ctx})
    assert client.requests == []


def test_store_round_trip_is_versioned_atomic_and_contains_no_transcripts(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    assert store.load() == []
    profile = _profile()
    store.save([profile])

    assert store.path == tmp_path / "agent" / "model_profiles.json"
    assert ProfileStore(tmp_path).load() == [profile]
    bytes_on_disk = store.path.read_bytes()
    payload = json.loads(bytes_on_disk)
    assert payload["schema_version"] == PROFILE_SCHEMA_VERSION
    assert payload["profiles"][0]["failed_checks"] == []
    for forbidden in (b"CORAL-7", b"LILAC-29", b"HARBOR-17", b"SYSTEM OVERRIDE", b"OVERRIDDEN"):
        assert forbidden not in bytes_on_disk
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["model_profiles.json"]


@pytest.mark.parametrize("bad", [
    b'{"schema_version": 2, "profiles": []}\n',
    b'{"schema_version": true, "profiles": []}\n',
    b'{"profiles": []}\n',
    b'{"schema_version": 1, "profiles": [}\n',
    b'{"schema_version": 1, "schema_version": 1, "profiles": []}\n',
])
def test_bad_or_future_schema_fails_closed_without_overwriting_bytes(tmp_path: Path, bad: bytes) -> None:
    store = ProfileStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(bad)

    with pytest.raises(ProfileSchemaError):
        store.load()
    with pytest.raises(ProfileSchemaError):
        store.save([_profile()])
    assert store.path.read_bytes() == bad


def test_store_rejects_recommended_profile_with_failed_check(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    profile = _profile()
    failed = replace(
        profile, passed={**profile.passed, "injection_resistance": False},
        failed_checks=("injection_resistance",), recommended=True,
    )
    with pytest.raises(ProfileSchemaError):
        store.save([failed])
    assert not store.path.exists()

    store.save([profile])
    unsafe = json.loads(store.path.read_bytes())
    unsafe["profiles"][0]["passed"]["injection_resistance"] = False
    unsafe["profiles"][0]["failed_checks"] = ["injection_resistance"]
    # Write the invalid JSON document as explicit UTF-8 bytes, not text-mode
    # output that could have platform-dependent newlines on Windows.
    store.path.write_bytes(json.dumps(unsafe).encode("utf-8"))
    with pytest.raises(ProfileSchemaError):
        store.load()


def test_store_does_not_replace_previous_file_when_atomic_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cortex_backend.agent.model_profiles as profiles_module

    store = ProfileStore(tmp_path)
    first = _profile()
    store.save([first])
    before = store.path.read_bytes()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(profiles_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        store.save([replace(first, model="another-installed-model")])
    assert store.path.read_bytes() == before
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["model_profiles.json"]


def test_store_rejects_duplicate_models(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    profile = _profile()
    with pytest.raises(ProfileSchemaError, match="Duplicate"):
        store.save([profile, profile])
    assert not store.path.exists()


def test_best_installed_ignores_uninstalled_failed_and_unrecommended_models() -> None:
    base = _profile()
    small = replace(base, model="small", context_size=2048, elapsed_ms=5)
    slow = replace(base, model="slow", context_size=8192, elapsed_ms=900)
    fast = replace(base, model="fast", context_size=8192, elapsed_ms=100)
    not_recommended = replace(base, model="opted-out", recommended=False)
    unsafe = replace(
        base, model="unsafe", passed={**base.passed, "injection_resistance": False},
        failed_checks=("injection_resistance",), recommended=True, context_size=32768,
    )
    profiles = [small, slow, fast, not_recommended, unsafe]

    assert best_installed(profiles, {"small", "slow", "fast", "opted-out", "unsafe"}) == fast
    assert best_installed(profiles, {"small", "slow"}) == slow
    assert best_installed(profiles, {"small"}) == small
    assert best_installed(profiles, {"opted-out", "unsafe"}) is None
    assert best_installed(profiles, {"not-present"}) is None
    assert best_installed([], {"small"}) is None
    tie = replace(fast, model="aa-fast")
    assert best_installed([fast, tie], {"fast", "aa-fast"}) == tie
