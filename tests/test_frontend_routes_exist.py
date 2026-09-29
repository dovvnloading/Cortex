"""Every route the SPA's API client calls must exist in the server's OpenAPI document.

``contracts/`` types the request and response bodies, but the client writes its
paths and methods by hand in ``frontend/src/api/client.ts``, and nothing tied
them to the server. Renaming or removing a route broke the app only when someone
clicked the button. This reads the client's call sites, the way a reviewer
would, and checks each ``(method, path)`` against ``app.openapi()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import textwrap

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies

_FRONTEND_API = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api"
_CLIENT = _FRONTEND_API / "client.ts"
_BASE_URL = _FRONTEND_API / "baseUrl.ts"

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_ENCODED_SEGMENT = re.compile(r"^encodeURIComponent\(\s*(\w+)\s*\)$")


@dataclass(frozen=True, order=True)
class ClientCall:
    method: str
    path: str  # relative to the API base, path parameters as ``{name}``
    where: str  # the method of the client that makes the call, for the failure message


# ---------------------------------------------------------------------------
# A small reader for the TypeScript in client.ts: enough of the string and
# template-literal grammar to find a call's first argument and its closing
# parenthesis without being fooled by a "(" inside a string or a ``${...}``.
# ---------------------------------------------------------------------------


def _read_quoted(source: str, start: int) -> tuple[str, int]:
    """Read a ``"..."`` or ``'...'`` literal starting at ``start``; return (text, end)."""
    quote = source[start]
    index = start + 1
    text: list[str] = []
    while source[index] != quote:
        if source[index] == "\\":
            index += 1
        text.append(source[index])
        index += 1
    return "".join(text), index + 1


def _read_template(source: str, start: int) -> tuple[list[tuple[str, str]], int]:
    """Read a template literal starting at its opening backtick.

    Returns its parts, each ``("text", literal)`` or ``("expr", source)``, and
    the index just past the closing backtick.
    """
    assert source[start] == "`"
    parts: list[tuple[str, str]] = []
    text: list[str] = []
    index = start + 1
    while source[index] != "`":
        if source[index] == "\\":
            text.append(source[index + 1])
            index += 2
        elif source.startswith("${", index):
            if text:
                parts.append(("text", "".join(text)))
                text = []
            end = _skip_braces(source, index + 1)
            parts.append(("expr", source[index + 2 : end - 1].strip()))
            index = end
        else:
            text.append(source[index])
            index += 1
    if text:
        parts.append(("text", "".join(text)))
    return parts, index + 1


def _skip_braces(source: str, start: int) -> int:
    """Return the index just past the ``}`` that closes the ``{`` at ``start``."""
    depth = 0
    index = start
    while True:
        character = source[index]
        if character in "\"'":
            index = _read_quoted(source, index)[1]
            continue
        if character == "`":
            index = _read_template(source, index)[1]
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1


def _closing_paren(source: str, open_index: int) -> int:
    """Return the index of the ``)`` that closes the ``(`` at ``open_index``."""
    depth = 0
    index = open_index
    while True:
        character = source[index]
        if character in "\"'":
            index = _read_quoted(source, index)[1]
            continue
        if character == "`":
            index = _read_template(source, index)[1]
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1


def _skip_generic(source: str, start: int) -> int:
    """Return the index just past the ``<...>`` type argument at ``start``."""
    assert source[start] == "<"
    depth = 0
    index = start
    while True:
        if source[index] == "<":
            depth += 1
        elif source[index] == ">":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1


# ---------------------------------------------------------------------------
# Turning a first argument into a route.
# ---------------------------------------------------------------------------


def _route_from_literal(source: str, start: int) -> tuple[str | None, int]:
    """Read the string or template literal at ``start`` as an API-relative path.

    Returns ``(None, end)`` when the literal is not a route (for example the
    ``${this.baseUrl}${path}`` a streaming call builds from a variable).
    """
    if source[start] in "\"'":
        text, end = _read_quoted(source, start)
        return text.split("?")[0], end
    assert source[start] == "`", "a client call must start with a string literal"
    parts, end = _read_template(source, start)
    if parts and parts[0] == ("expr", "this.baseUrl"):
        parts = parts[1:]
        if not parts or parts[0][0] != "text":
            return None, end  # `${this.baseUrl}${path}`: the caller passes the path
    route: list[str] = []
    for position, (kind, value) in enumerate(parts):
        if kind == "text":
            if "?" in value:
                route.append(value.split("?")[0])
                break
            route.append(value)
            continue
        segment = _ENCODED_SEGMENT.match(value)
        if segment is not None:
            route.append("{" + segment.group(1) + "}")
            continue
        # Anything else is a query string built at run time, and only at the end.
        assert position == len(parts) - 1, f"unexpected expression in the path: ${{{value}}}"
    return "".join(route), end


def _method_of(arguments: str) -> str:
    match = re.search(r"\bmethod:\s*[\"'](" + "|".join(_METHODS) + r")[\"']", arguments)
    return match.group(1) if match else "GET"


def _enclosing_method(source: str, index: int) -> str:
    names = re.findall(
        r"^  (?:private\s+)?(?:async\s+)?(\w+)\s*(?:<[^>]*>)?\(", source[:index], re.MULTILINE
    )
    return names[-1] if names else "?"


_CALL_SITES = re.compile(
    r"this\.(?P<callee>request|streamEvents|fetchWithSession)(?P<generic><)?"
)


def client_calls(source: str) -> tuple[list[ClientCall], int]:
    """Return the routes the client calls, and how many call sites it looked at.

    A call site whose path is not a literal in that call (the shared ``request``
    and ``streamEvents`` helpers themselves) is counted as looked at but yields
    no route; the caller checks the two numbers against each other.
    """
    calls: list[ClientCall] = []
    looked_at = 0
    for match in _CALL_SITES.finditer(source):
        callee = match.group("callee")
        index = match.end()
        if match.group("generic"):
            index = _skip_generic(source, index - 1)
        assert source[index] == "(", f"unexpected shape of a call to {callee}"
        close = _closing_paren(source, index)
        first = index + 1
        while source[first].isspace():
            first += 1
        looked_at += 1
        if source[first] not in "\"'`":
            continue  # `this.fetchWithSession(url, init)`: the helper's own forwarding
        path, end = _route_from_literal(source, first)
        if path is None:
            continue
        arguments = source[end:close]
        method = _method_of(arguments) if callee != "streamEvents" else "GET"
        calls.append(ClientCall(method, path, _enclosing_method(source, match.start())))
    return calls, looked_at


def _api_prefix() -> str:
    match = re.search(r"DEFAULT_API_BASE_URL\s*=\s*\"([^\"]+)\"", _BASE_URL.read_text(encoding="utf-8"))
    assert match is not None, "baseUrl.ts no longer declares DEFAULT_API_BASE_URL"
    return match.group(1).rstrip("/")


def _canonical(path: str) -> str:
    """Compare path templates by shape, not by what the parameter is called."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def _served_routes() -> set[tuple[str, str]]:
    document = create_app(build_demo_dependencies()).openapi()
    return {
        (method.upper(), _canonical(path))
        for path, operations in document["paths"].items()
        for method in operations
    }


# ---------------------------------------------------------------------------
# The reader itself: a wrong reader would let a wrong route through.
# ---------------------------------------------------------------------------


def test_the_reader_understands_every_shape_of_call_the_client_uses() -> None:
    source = textwrap.dedent("""
      export class CortexApi {
        chat(id: string): Promise<ChatResponse> {
          return this.request<ChatResponse>(`/chats/${encodeURIComponent(id)}`);
        }
        rename(id: string, body: string): Promise<ChatResponse> {
          return this.request<ChatResponse>(
            `/chats/${encodeURIComponent(id)}`,
            { method: "PATCH", body: JSON.stringify(body) },
          );
        }
        list(): Promise<Array<ChatSummary>> {
          return this.request<Array<ChatSummary>>("/chats");
        }
        tasks(query: string): Promise<Tasks> {
          return this.request<Tasks>(`/execution/tasks${query ? `?${query}` : ""}`);
        }
        remove(id: string): Promise<void> {
          return this.request<void>(`/chats/${encodeURIComponent(id)}`, {
            method: "DELETE",
          });
        }
        files(repo: string): Promise<Files> {
          return this.request<Files>(`/models/gguf/huggingface-files?${repo}`);
        }
        stream(job: string) {
          return this.streamEvents(`/jobs/${encodeURIComponent(job)}/events`, onEvent, options);
        }
        async download(id: string) {
          return this.fetchWithSession(
            `${this.baseUrl}/execution/artifacts/${encodeURIComponent(id)}`,
            {},
          );
        }
        private async streamEvents<T>(path: string) {
          return this.fetchWithSession(`${this.baseUrl}${path}`, { headers });
        }
        private async request<T>(path: string) {
          return await this.fetchWithSession(url, init);
        }
      }
    """)

    calls, looked_at = client_calls(source)

    assert [(call.method, call.path, call.where) for call in calls] == [
        ("GET", "/chats/{id}", "chat"),
        ("PATCH", "/chats/{id}", "rename"),
        ("GET", "/chats", "list"),
        ("GET", "/execution/tasks", "tasks"),
        ("DELETE", "/chats/{id}", "remove"),
        ("GET", "/models/gguf/huggingface-files", "files"),
        ("GET", "/jobs/{job}/events", "stream"),
        ("GET", "/execution/artifacts/{id}", "download"),
    ]
    # Two more sites are the helpers' own forwarding, which name no route.
    assert looked_at == len(calls) + 2


# ---------------------------------------------------------------------------
# The check.
# ---------------------------------------------------------------------------


def test_the_client_makes_the_calls_this_test_expects_to_check() -> None:
    """Guard against the reader silently finding nothing."""
    calls, looked_at = client_calls(_CLIENT.read_text(encoding="utf-8"))

    assert len(calls) >= 40
    # Only the helpers' own forwarding sites are allowed to name no route.
    assert looked_at - len(calls) == 2
    assert {"generate", "regenerate", "cancelGeneration", "stageChatAttachment"} <= {
        call.where for call in calls
    }


def test_every_route_the_client_calls_exists_in_the_openapi_document() -> None:
    prefix = _api_prefix()
    calls, _ = client_calls(_CLIENT.read_text(encoding="utf-8"))
    served = _served_routes()

    missing = sorted(
        f"{call.method} {prefix}{call.path}  (CortexApi.{call.where})"
        for call in calls
        if (call.method, _canonical(prefix + call.path)) not in served
    )

    assert missing == [], "the client calls routes the server does not serve:\n" + "\n".join(missing)


def test_the_check_catches_a_route_that_is_gone() -> None:
    """A client call to a removed route must fail this check, not pass it."""
    source = _CLIENT.read_text(encoding="utf-8")
    assert "/chats/${encodeURIComponent(threadId)}/messages" not in source
    stale = source.replace(
        "`/chats/${encodeURIComponent(threadId)}/forks`",
        "`/chats/${encodeURIComponent(threadId)}/messages`",
        1,
    )
    assert stale != source

    calls, _ = client_calls(stale)
    served = _served_routes()
    prefix = _api_prefix()
    unserved = [
        call for call in calls if (call.method, _canonical(prefix + call.path)) not in served
    ]

    assert [(call.method, call.path) for call in unserved] == [("POST", "/chats/{threadId}/messages")]


def test_the_client_uses_the_default_api_prefix_the_server_mounts() -> None:
    assert _api_prefix() == "/api/v1"


def test_every_header_the_client_sends_passes_a_cors_preflight() -> None:
    """A header the client sets but the server does not allow fails only cross-origin.

    That is exactly the case nobody exercises by hand, which is how the launcher
    handoff header was left off the list.
    """
    source = _CLIENT.read_text(encoding="utf-8")
    sent = set(re.findall(r"headers\.set\(\s*\"([\w-]+)\"", source))
    sent |= set(re.findall(r"headers:\s*\{\s*\"([\w-]+)\"", source))
    assert {"Authorization", "Content-Type", "Last-Event-ID", "X-Cortex-Handoff"} <= sent

    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        for header in sorted(sent):
            preflight = client.options(
                "/api/v1/session/handoff",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": header.lower(),
                },
            )
            assert preflight.status_code == 200, header
