"""The tool-call boundary never dispatches calls; only validates descriptions."""

from __future__ import annotations

import copy
import json
from threading import Event
from types import SimpleNamespace

import pytest

from cortex_backend.agent.protocol import (
    MAX_ARGUMENT_BYTES,
    ModelTurn,
    ProtocolError,
    RuntimeCapabilities,
    ToolCall,
    ToolSpec,
    call_model,
    runtime_capabilities,
)


TOOL = ToolSpec(
    "lookup", "Look up a synthetic key", {
        "type": "object", "properties": {"key": {"type": "string", "minLength": 1}},
        "required": ["key"], "additionalProperties": False,
    },
)
EXPECTED = ModelTurn("", [ToolCall("call_0", "lookup", {"key": "abc"})], "tool_calls")


class _ScriptedClient:
    def __init__(self, reply: object, capabilities: RuntimeCapabilities | None = None) -> None:
        self.reply = reply
        self.tool_protocol_capabilities = capabilities or RuntimeCapabilities()
        self.requests: list[dict] = []

    def chat(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        return self.reply


def _json_envelope(*, tool_calls: list | None = None, finish: str = "tool_calls", text: str = "") -> str:
    return json.dumps({
        "text": text, "finish": finish,
        "tool_calls": [{"name": "lookup", "arguments": {"key": "abc"}}] if tool_calls is None else tool_calls,
    })


@pytest.mark.parametrize("path", ["native", "schema", "strict"])
def test_three_runtime_paths_return_identical_validated_tool_calls(path: str) -> None:
    native = path == "native"
    schema = path == "schema"
    if native:
        # Attribute-based response is the shape returned by ollama.ChatResponse.
        reply = SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[SimpleNamespace(
                function=SimpleNamespace(name="lookup", arguments={"key": "abc"}),
            )]),
            done_reason="stop",
        )
    else:
        reply = {"message": {"content": _json_envelope()}, "done_reason": "stop"}
    client = _ScriptedClient(reply, RuntimeCapabilities(native, schema, "ollama" if schema else "none"))
    messages = [{"role": "user", "content": "test"}]
    options = {"temperature": 0}
    original_schema = copy.deepcopy(TOOL.json_schema)

    result = call_model(client, model="model", messages=messages, tools=[TOOL], options=options)

    assert result == EXPECTED
    request = client.requests[0]
    assert request["model"] == "model" and request["options"] is options
    if native:
        assert request["tools"] == [{"type": "function", "function": {
            "name": "lookup", "description": TOOL.description, "parameters": TOOL.json_schema,
        }}]
        assert request["messages"] is messages
        assert "format" not in request
    else:
        assert "tools" not in request
        assert request["messages"][0]["role"] == "system"
        assert request["messages"][1:] == messages
        if schema:
            format_schema = request["format"]
            assert format_schema["properties"]["tool_calls"]["items"]["oneOf"][0]["properties"][
                "arguments"
            ] == TOOL.json_schema
        else:
            assert "format" not in request
    assert TOOL.json_schema == original_schema  # never mutate offered tools
    assert messages == [{"role": "user", "content": "test"}]
    assert options == {"temperature": 0}


def test_plain_chat_request_is_byte_identical_and_does_not_add_fields() -> None:
    reply = {"message": {"content": "hello"}, "done_reason": "stop"}
    client = _ScriptedClient(reply, RuntimeCapabilities(True, True, "ollama"))
    messages = [{"role": "user", "content": "hi"}]
    options = {"temperature": 0.2}

    result = call_model(client, model="example", messages=messages, tools=[], options=options)

    assert result == ModelTurn("hello", [], "stop")
    assert client.requests == [{"model": "example", "messages": messages, "options": options}]
    assert client.requests[0]["messages"] is messages
    assert client.requests[0]["options"] is options


@pytest.mark.parametrize(
    ("calls", "expected_code"),
    [
        ([{"name": "shell", "arguments": {"key": "abc"}}], "unknown_tool"),
        ([{"name": "lookup", "arguments": {"key": "abc", "execute": True}}], "extra_argument"),
        ([{"name": "lookup", "arguments": {}}], "missing_argument"),
        ([{"name": "lookup", "arguments": ["abc"]}], "arguments_not_object"),
        ([{"name": "lookup", "arguments": {"key": ""}}], "invalid_arguments"),
        ([{"name": "lookup", "arguments": {"key": "x" * (MAX_ARGUMENT_BYTES + 1)}}],
         "arguments_too_large"),
        ([{"name": "lookup", "arguments": {"key": 4}}], "invalid_argument_type"),
        ([{"name": "lookup", "arguments": "{\"key\":\"a\",\"key\":\"b\"}"}], "duplicate_key"),
    ],
)
def test_hostile_calls_fail_closed_in_strict_fallback(calls: list, expected_code: str) -> None:
    client = _ScriptedClient({"message": {"content": _json_envelope(tool_calls=calls)}})
    with pytest.raises(ProtocolError) as caught:
        call_model(client, model="x", messages=[], tools=[TOOL], options={})
    assert caught.value.code == expected_code


@pytest.mark.parametrize("path", ["native", "schema", "strict"])
def test_unoffered_tool_is_rejected_on_every_path(path: str) -> None:
    native = path == "native"
    schema = path == "schema"
    if native:
        reply = {"message": {"content": "", "tool_calls": [
            {"function": {"name": "unoffered", "arguments": {}}},
        ]}}
    else:
        reply = {"message": {"content": _json_envelope(tool_calls=[
            {"name": "unoffered", "arguments": {}},
        ])}}
    client = _ScriptedClient(reply, RuntimeCapabilities(native, schema))
    with pytest.raises(ProtocolError) as caught:
        call_model(client, model="x", messages=[], tools=[TOOL], options={})
    assert caught.value.code == "unknown_tool"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ('{"text":"a","text":"b","tool_calls":[],"finish":"stop"}', "duplicate_key"),
        ('{"text":"a","tool_calls":[],"finish":"stop","route":"shell"}', "extra_fields"),
        ('{"text":"a","finish":"stop"}', "missing_fields"),
        ('{"text":"a","tool_calls":[],"finish":"tool_calls"}', "invalid_finish"),
        ('```json\n{}\n```', "invalid_json"),
    ],
)
def test_strict_fallback_rejects_untrusted_json_envelopes(content: str, code: str) -> None:
    client = _ScriptedClient({"message": {"content": content}})
    with pytest.raises(ProtocolError) as caught:
        call_model(client, model="x", messages=[], tools=[TOOL], options={})
    assert caught.value.code == code


def test_openai_native_arguments_are_strict_json_and_tool_ids_are_preserved() -> None:
    reply = {"message": {"content": None, "tool_calls": [{
        "id": "call_example", "type": "function",
        "function": {"name": "lookup", "arguments": '{"key":"abc"}'},
    }]}}
    client = _ScriptedClient(reply, RuntimeCapabilities(native_tools=True))
    assert call_model(client, model="x", messages=[], tools=[TOOL], options={}) == ModelTurn(
        "", [ToolCall("call_example", "lookup", {"key": "abc"})], "tool_calls",
    )


def test_final_answer_in_schema_fallback_returns_no_calls() -> None:
    reply = {"message": {"content": _json_envelope(tool_calls=[], finish="stop", text="done")}}
    client = _ScriptedClient(reply, RuntimeCapabilities(json_schema=True))
    assert call_model(client, model="x", messages=[], tools=[TOOL], options={}) == ModelTurn("done", [], "stop")


def test_cancellation_before_request_does_not_call_model() -> None:
    cancelled = Event()
    cancelled.set()
    client = _ScriptedClient({"message": {"content": "ignored"}})
    assert call_model(client, model="x", messages=[], tools=[TOOL], options={},
                      cancellation_event=cancelled) == ModelTurn("", [], "cancelled")
    assert not client.requests


def test_runtime_capabilities_default_to_fail_closed() -> None:
    assert runtime_capabilities(object()) == RuntimeCapabilities()


def test_real_ollama_client_only_sends_optional_tool_fields_when_supplied() -> None:
    from cortex_backend.services.chat_client import OllamaChatClient

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def chat(self, **kwargs: object) -> dict:
            self.calls.append(kwargs)
            return {"message": {"content": "ok"}}

    stub = _Recorder()
    client = OllamaChatClient(stub)
    args = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "options": {}}
    client.chat(**args)
    assert stub.calls[-1] == args
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": TOOL.json_schema}}]
    schema = {"type": "object", "properties": {"key": {"type": "string"}}}
    client.chat(**args, tools=tools, format=schema)
    assert stub.calls[-1] == {**args, "tools": tools, "format": schema}


def test_llamacpp_request_passes_through_tools_and_json_schema() -> None:
    from cortex_backend.llamacpp.chat_client import _build_request_body

    tools = [{"type": "function", "function": {"name": "lookup", "parameters": TOOL.json_schema}}]
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    args = ([{"role": "user", "content": "hi"}], {"temperature": 0})
    original = _build_request_body(*args, stream=False)
    assert "tools" not in original and "response_format" not in original
    result = _build_request_body(*args, stream=False, tools=tools, format=schema)
    assert result["tools"] == tools
    assert result["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "agent_turn", "schema": schema, "strict": True,
    }}
    assert {key: value for key, value in result.items() if key not in ("tools", "response_format")} == original


def test_nested_extra_fields_are_rejected_even_without_additional_properties_keyword() -> None:
    nested = ToolSpec("lookup", "lookup", {
        "type": "object", "properties": {"metadata": {
            "type": "object", "properties": {"allowed": {"type": "integer"}}, "required": ["allowed"],
        }}, "required": ["metadata"],
    })
    call = {"name": "lookup", "arguments": {"metadata": {"allowed": 1, "unoffered": "no"}}}
    client = _ScriptedClient({"message": {"content": _json_envelope(tool_calls=[call])}})
    with pytest.raises(ProtocolError) as caught:
        call_model(client, model="x", messages=[], tools=[nested], options={})
    assert caught.value.code == "extra_argument"


def test_optional_union_object_still_rejects_extra_keys() -> None:
    union = ToolSpec("lookup", "lookup", {
        "type": "object", "properties": {"payload": {
            "type": ["object", "null"], "properties": {"ok": {"type": "string"}},
        }}, "required": ["payload"],
    })
    call = {"name": "lookup", "arguments": {"payload": {"ok": "yes", "extra": 1}}}
    client = _ScriptedClient({"message": {"content": _json_envelope(tool_calls=[call])}},
                             RuntimeCapabilities(json_schema=True))
    with pytest.raises(ProtocolError) as caught:
        call_model(client, model="x", messages=[], tools=[union], options={})
    assert caught.value.code == "extra_argument"


def test_native_function_envelope_has_no_escape_hatch() -> None:
    reply = {"message": {"content": "", "tool_calls": [{
        "function": {"name": "lookup", "arguments": {"key": "abc"}, "execute": True},
    }]}}
    client = _ScriptedClient(reply, RuntimeCapabilities(native_tools=True))
    with pytest.raises(ProtocolError) as caught:
        call_model(client, model="x", messages=[], tools=[TOOL], options={})
    assert caught.value.code == "extra_fields"


def test_streamed_ollama_object_chunks_preserve_tool_calls() -> None:
    from cortex_backend.services.chat_client import OllamaChatClient

    class _Stream:
        def chat(self, **kwargs: object):
            assert kwargs["stream"] is True
            yield SimpleNamespace(
                message=SimpleNamespace(content="", thinking=None, tool_calls=[{
                    "function": {"name": "lookup", "arguments": {"key": "abc"}},
                }]), done=True, model_dump=lambda: {"done": True},
            )

    result = OllamaChatClient(_Stream()).chat(
        model="example", messages=[], options={}, tools=[{"type": "function"}], cancellation_event=Event(),
    )
    assert result["message"]["tool_calls"] == [
        {"function": {"name": "lookup", "arguments": {"key": "abc"}}},
    ]


def test_streamed_llamacpp_tool_call_fragments_are_assembled() -> None:
    import time
    from pathlib import Path

    import httpx

    from cortex_backend.llamacpp.chat_client import LlamaCppChatClient

    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_7",
            "function": {"name": "lookup", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '{"key":"abc"}'}}]}, "finish_reason": "tool_calls"}]},
    ]
    sse = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=sse)))
    client = LlamaCppChatClient(None, models_directory=lambda: Path("."), http_client=http)
    try:
        result = client._chat_abortable(
            "http://127.0.0.1:9999", [], {}, time.monotonic(), Event(), None,
            tools=[{"type": "function"}],
        )
    finally:
        http.close()
    assert result["message"]["tool_calls"] == [{
        "id": "call_7", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"abc"}'},
    }]
    assert result["done_reason"] == "tool_calls"
