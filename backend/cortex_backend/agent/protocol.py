"""A deliberately inert, fail-closed tool-call boundary for future agent loops.

This module *describes* calls; it never resolves, executes, or dispatches them.
Ordinary text chat sends exactly the caller's original chat arguments. Only a
caller that offers tools opts into native calling or the JSON fallbacks.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from threading import Event
from typing import Any

MAX_ARGUMENT_BYTES = 64 * 1024
_MAX_CALLS = 32


class ProtocolError(ValueError):
    """Invalid tool envelope, schema or arguments. 'code' is stable for callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    json_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    finish: str


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    native_tools: bool = False
    json_schema: bool = False
    # Ollama accepts JSON Schema through format; llama.cpp uses response_format.
    format_style: str = "none"


def runtime_capabilities(client: object, *, model: str | None = None) -> RuntimeCapabilities:
    """Report adapter capabilities, not guessed model-specific abilities.

    Test/future adapters can expose 'tool_protocol_capabilities' explicitly.
    Routing clients are resolved from the *model*, never a model-generated call.
    Unknown clients get the strict-JSON fallback, not optimistic tool support.
    """
    override = getattr(client, "tool_protocol_capabilities", None)
    if isinstance(override, RuntimeCapabilities):
        return override
    runtime = getattr(client, "tool_protocol_runtime", None)
    if runtime == "routing" and model is not None:
        from cortex_backend.services.chat_client import GGUF_PREFIX, is_cloud_model

        target = getattr(client, "_llamacpp") if model.startswith(GGUF_PREFIX) else getattr(client, "_ollama")
        if is_cloud_model(model):
            target = getattr(client, "_cloud", None)
        return runtime_capabilities(target, model=model)
    if runtime == "ollama":
        return RuntimeCapabilities(native_tools=True, json_schema=True, format_style="ollama")
    if runtime == "llamacpp":
        return RuntimeCapabilities(native_tools=True, json_schema=True, format_style="openai")
    return RuntimeCapabilities()


def _field(value: object, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in items:
            if name in result:
                raise ProtocolError("duplicate_key", "Duplicate JSON object key.")
            result[name] = value
        return result

    def constant(_value: str) -> Any:
        raise ProtocolError("invalid_json", "Non-finite numbers are not JSON.")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("invalid_json", "Model returned invalid JSON.") from exc


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy the offered schema and close every object, without mutating it."""
    result = copy.deepcopy(schema)

    def close(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if (node.get("type") == "object" or
                isinstance(node.get("type"), list) and "object" in node["type"] or
                "properties" in node):
            node["additionalProperties"] = False
        for key in ("properties", "$defs", "definitions"):
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    close(child)
        for key in ("items", "additionalProperties", "not"):
            close(node.get(key))
        for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    close(child)

    close(result)
    return result


def _specs_by_name(tools: list[ToolSpec]) -> dict[str, ToolSpec]:
    by_name: dict[str, ToolSpec] = {}
    for spec in tools:
        if not isinstance(spec.name, str) or not spec.name or spec.name in by_name:
            raise ProtocolError("invalid_tool_schema", "Tool names must be nonempty and unique.")
        schema = spec.json_schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ProtocolError("invalid_tool_schema", "Tool parameters must have an object JSON Schema.")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list) or not all(
            isinstance(name, str) and name in properties for name in required
        ):
            raise ProtocolError("invalid_tool_schema", "Tool properties and required fields are invalid.")
        by_name[spec.name] = spec
    return by_name


def _resolve_ref(reference: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    if not reference.startswith("#/"):
        raise ProtocolError("invalid_tool_schema", "Only local JSON Schema references are supported.")
    node: Any = root
    for segment in reference[2:].split("/"):
        if not isinstance(node, Mapping):
            raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema reference.")
        node = node.get(segment.replace("~1", "/").replace("~0", "~"))
    if not isinstance(node, Mapping):
        raise ProtocolError("invalid_tool_schema", "Unresolved JSON Schema reference.")
    return node


def _matches_type(value: Any, name: str) -> bool:
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }.get(name, lambda: False)()


def _check_schema(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any], depth: int = 0) -> None:
    """Validate supported JSON Schema assertions; unsupported ones fail closed.

    Extra properties are forbidden even if the caller's schema omitted the
    additionalProperties: false keyword. $ref stays inside this schema.
    """
    if depth > 40:
        raise ProtocolError("invalid_tool_schema", "JSON Schema is too deeply nested.")
    allowed = {
        "$schema", "$id", "$defs", "definitions", "$ref", "title", "description", "default", "examples",
        "type", "properties", "required", "additionalProperties", "items", "prefixItems", "enum", "const",
        "oneOf", "anyOf", "allOf", "not", "minLength", "maxLength", "pattern", "format", "minItems",
        "maxItems", "minProperties", "maxProperties", "minimum", "maximum", "exclusiveMinimum",
        "exclusiveMaximum", "multipleOf", "uniqueItems",
    }
    if set(schema) - allowed:
        raise ProtocolError("invalid_tool_schema", "Unsupported JSON Schema assertion.")
    if "$ref" in schema:
        reference = schema["$ref"]
        if not isinstance(reference, str):
            raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema reference.")
        _check_schema(value, _resolve_ref(reference, root), root, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if key not in schema:
            continue
        variants = schema[key]
        if not isinstance(variants, list) or not variants or not all(isinstance(s, dict) for s in variants):
            raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema alternatives.")
        matches = 0
        for alternative in variants:
            try:
                _check_schema(value, alternative, root, depth + 1)
            except ProtocolError as exc:
                if exc.code == "invalid_tool_schema":
                    raise
                if key == "allOf":
                    raise
            else:
                matches += 1
        if (key == "anyOf" and matches == 0) or (key == "oneOf" and matches != 1):
            raise ProtocolError("invalid_arguments", "Arguments do not match the tool schema.")
    if "not" in schema:
        exclusion = schema["not"]
        if not isinstance(exclusion, dict):
            raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema exclusion.")
        try:
            _check_schema(value, exclusion, root, depth + 1)
        except ProtocolError as exc:
            if exc.code == "invalid_tool_schema":
                raise
        else:
            raise ProtocolError("invalid_arguments", "Arguments violate the tool schema.")
    types = schema.get("type")
    if types is not None:
        names = [types] if isinstance(types, str) else types
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema type.")
        if not any(_matches_type(value, name) for name in names):
            raise ProtocolError("invalid_argument_type", "Argument has an invalid type.")
    if "const" in schema and value != schema["const"]:
        raise ProtocolError("invalid_arguments", "Argument does not match its constant.")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list):
            raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema enum.")
        if value not in choices:
            raise ProtocolError("invalid_arguments", "Argument is not one of the allowed values.")
    if isinstance(value, dict) and (
        schema.get("type") == "object"
        or isinstance(schema.get("type"), list) and "object" in schema["type"]
        or "properties" in schema
    ):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ProtocolError("invalid_tool_schema", "Invalid object schema.")
        if any(name not in value for name in required):
            raise ProtocolError("missing_argument", "Required tool argument is missing.")
        if set(value) - set(properties):
            raise ProtocolError("extra_argument", "Undeclared tool argument is present.")
        for name, item in value.items():
            child = properties[name]
            if not isinstance(child, dict):
                raise ProtocolError("invalid_tool_schema", "Invalid property schema.")
            _check_schema(item, child, root, depth + 1)
        for keyword, bound in (("minProperties", 0), ("maxProperties", 0)):
            if keyword in schema and (not isinstance(schema[keyword], int) or schema[keyword] < bound):
                raise ProtocolError("invalid_tool_schema", "Invalid property count bound.")
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", math.inf):
            raise ProtocolError("invalid_arguments", "Tool argument object has invalid field count.")
    if isinstance(value, list):
        minimum, maximum = schema.get("minItems", 0), schema.get("maxItems", math.inf)
        if len(value) < minimum or len(value) > maximum:
            raise ProtocolError("invalid_arguments", "Tool argument array has invalid length.")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise ProtocolError("invalid_arguments", "Tool argument array requires unique items.")
        prefix = schema.get("prefixItems", [])
        if not isinstance(prefix, list):
            raise ProtocolError("invalid_tool_schema", "Invalid array tuple schema.")
        for index, item in enumerate(value):
            child = prefix[index] if index < len(prefix) else schema.get("items")
            if child is False:
                raise ProtocolError("invalid_arguments", "Tool argument array has extra items.")
            if isinstance(child, dict):
                _check_schema(item, child, root, depth + 1)
            elif child is not None and child is not True:
                raise ProtocolError("invalid_tool_schema", "Invalid array item schema.")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ProtocolError("invalid_arguments", "String argument has invalid length.")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    raise ProtocolError("invalid_arguments", "String argument does not match its pattern.")
            except (re.error, TypeError) as exc:
                raise ProtocolError("invalid_tool_schema", "Invalid JSON Schema pattern.") from exc
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, operator in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b),
                              ("exclusiveMinimum", lambda a, b: a > b),
                              ("exclusiveMaximum", lambda a, b: a < b)):
            if key in schema and not operator(value, schema[key]):
                raise ProtocolError("invalid_arguments", "Number argument is out of bounds.")
        if "multipleOf" in schema and value % schema["multipleOf"] != 0:
            raise ProtocolError("invalid_arguments", "Number argument is not an allowed multiple.")


def _arguments(raw: Any, spec: ToolSpec) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            byte_length = len(raw.encode("utf-8"))
        except UnicodeError as exc:
            raise ProtocolError("invalid_arguments", "Tool arguments are not valid UTF-8.") from exc
        if byte_length > MAX_ARGUMENT_BYTES:
            raise ProtocolError("arguments_too_large", "Tool arguments exceed 64 KiB.")
        raw = _strict_json(raw)
    if not isinstance(raw, dict):
        raise ProtocolError("arguments_not_object", "Tool arguments must be a JSON object.")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError("invalid_arguments", "Tool arguments are not JSON values.") from exc
    if len(encoded) > MAX_ARGUMENT_BYTES:
        raise ProtocolError("arguments_too_large", "Tool arguments exceed 64 KiB.")
    schema = _strict_schema(spec.json_schema)
    _check_schema(raw, schema, schema)
    return raw


def _keys(value: Any, required: set[str], optional: Set[str] = frozenset()) -> None:
    if not isinstance(value, dict):
        raise ProtocolError("malformed_response", "Expected a JSON object.")
    if set(value) - required - optional:
        raise ProtocolError("extra_fields", "Unrecognized tool envelope fields.")
    if required - set(value):
        raise ProtocolError("missing_fields", "Tool envelope lacks required fields.")


def _parse_calls(raw: Any, offered: dict[str, ToolSpec], *, native: bool) -> list[ToolCall]:
    if not isinstance(raw, list):
        raise ProtocolError("malformed_response", "tool_calls must be a list.")
    if len(raw) > _MAX_CALLS:
        raise ProtocolError("malformed_response", "Too many tool calls in one turn.")
    calls: list[ToolCall] = []
    ids: set[str] = set()
    for index, call in enumerate(raw):
        if native:
            if isinstance(call, Mapping):
                _keys(dict(call), {"function"}, {"id", "type", "index"})
            function = _field(call, "function")
            if isinstance(function, Mapping):
                _keys(dict(function), {"name", "arguments"})
            name, arguments = _field(function, "name"), _field(function, "arguments")
            id_value = _field(call, "id")
        else:
            _keys(call, {"name", "arguments"}, {"id"})
            name, arguments, id_value = call["name"], call["arguments"], call.get("id")
        if not isinstance(name, str) or name not in offered:
            raise ProtocolError("unknown_tool", "Model called a tool that was not offered.")
        call_id = f"call_{index}" if id_value is None else id_value
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
            raise ProtocolError("malformed_response", "Invalid tool call identifier.")
        if call_id in ids:
            raise ProtocolError("duplicate_call_id", "Duplicate tool call identifier.")
        ids.add(call_id)
        calls.append(ToolCall(call_id, name, _arguments(arguments, offered[name])))
    return calls


def _envelope_schema(tools: list[ToolSpec]) -> dict[str, Any]:
    choices = [{
        "type": "object", "additionalProperties": False, "required": ["name", "arguments"],
        "properties": {"id": {"type": "string"}, "name": {"const": tool.name},
                       "arguments": _strict_schema(tool.json_schema)},
    } for tool in tools]
    return {
        "type": "object", "additionalProperties": False,
        "required": ["text", "tool_calls", "finish"],
        "properties": {
            "text": {"type": "string"},
            "finish": {"type": "string", "enum": ["stop", "tool_calls"]},
            "tool_calls": {"type": "array", "items": {"oneOf": choices}},
        },
    }


def _instructions(tools: list[ToolSpec]) -> str:
    offered = [{"name": tool.name, "description": tool.description, "parameters": tool.json_schema} for tool in tools]
    return (
        "Respond with ONLY a JSON object with exactly: text (string), tool_calls (array), "
        "finish ('stop' or 'tool_calls'). For a final answer use finish='stop', "
        "tool_calls=[], and put the answer in text. For tool calls use "
        "finish='tool_calls' and one or more {name, arguments} entries. "
        "Arguments must be objects conforming to these offered tools: "
        + json.dumps(offered, ensure_ascii=False, allow_nan=False)
    )


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ProtocolError("malformed_response", "Model message content must be a string.")
    return value


def _finish(response: object) -> str:
    finish = _field(response, "done_reason") or _field(response, "finish_reason")
    if finish is None:
        return "stop"
    if not isinstance(finish, str):
        raise ProtocolError("malformed_response", "Invalid model finish reason.")
    return finish


def call_model(
    client: Any,
    *,
    model: str,
    messages: list[dict],
    tools: list[ToolSpec],
    options: dict,
    cancellation_event: Event | None = None,
) -> ModelTurn:
    """One model request -> validated descriptions of calls; never execution.

    Do not attach new request keys to ordinary chat. With offered tools choose
    native, then schema format, then strict prompt+JSON from known capabilities.
    """
    offered = _specs_by_name(tools)
    if cancellation_event is not None and cancellation_event.is_set():
        return ModelTurn("", [], "cancelled")
    capabilities = runtime_capabilities(client, model=model) if tools else RuntimeCapabilities()
    request: dict[str, Any] = {"model": model, "messages": messages, "options": options}
    if cancellation_event is not None:
        request["cancellation_event"] = cancellation_event
    if tools:
        if capabilities.native_tools:
            request["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": _strict_schema(tool.json_schema),
            }} for tool in tools]
        else:
            request["messages"] = [{"role": "system", "content": _instructions(tools)}, *messages]
            if capabilities.json_schema:
                request["format"] = _envelope_schema(tools)
    response = client.chat(**request)
    if cancellation_event is not None and cancellation_event.is_set():
        return ModelTurn("", [], "cancelled")
    message = _field(response, "message")
    if message is None:
        raise ProtocolError("malformed_response", "Model response lacks a message.")
    content = _field(message, "content")
    if tools and not capabilities.native_tools:
        envelope = _strict_json(_text(content))
        _keys(envelope, {"text", "tool_calls", "finish"})
        finish = envelope["finish"]
        calls = _parse_calls(envelope["tool_calls"], offered, native=False)
        text = _text(envelope["text"])
        if (finish == "tool_calls") != bool(calls) or finish not in ("stop", "tool_calls"):
            raise ProtocolError("invalid_finish", "Finish reason disagrees with tool calls.")
        if _finish(response) == "length":
            raise ProtocolError("invalid_finish", "Tool-call JSON was truncated.")
        return ModelTurn(text, calls, finish)
    calls_raw = _field(message, "tool_calls")
    calls = _parse_calls(calls_raw, offered, native=True) if calls_raw is not None else []
    finish = _finish(response)
    if calls:
        if finish in ("length", "cancelled"):
            raise ProtocolError("invalid_finish", "Model did not complete tool calls.")
        finish = "tool_calls"
    return ModelTurn(_text(content or ""), calls, finish)
