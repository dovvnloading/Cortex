"""Run the real local API for the browser tests, on deterministic dependencies.

Every Playwright spec intercepts ``**/api/v1/**`` and answers from a
hand-written fixture, so the suite has never executed the real routes. The
contract between the two halves of Cortex was checked only by the generated
TypeScript types -- and the fixtures had already drifted from the API, which is
how a removed settings field survived in seven of them.

This serves the actual application: real routing, real Pydantic serialisation,
real status codes, real SSE framing, and the real session exchange. Only the
model runtime and the stores behind it are deterministic stand-ins, because a
browser test cannot depend on Ollama being installed.

The port and the liveness marker come from ``frontend/e2e/e2e-backend.json``,
the file the Playwright config reads too. The port is not the README
screenshot server's, and the marker header on the liveness route is how the
real-API spec tells this server from anything else left listening there.

The handoff secret is fixed and printed nowhere. It is a test credential for a
loopback server that holds no real data, and the frontend needs it in the URL
to complete the exchange the way the launcher does.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from starlette.types import ASGIApp, Message, Receive, Scope, Send  # noqa: E402

from cortex_backend.api import create_app  # noqa: E402
from cortex_backend.testing import build_demo_dependencies  # noqa: E402

# Matches E2E_HANDOFF_SECRET in frontend/e2e/fixtures.ts.
HANDOFF_SECRET = "e2e-handoff-secret"

IDENTITY_FILE = ROOT / "frontend" / "e2e" / "e2e-backend.json"
LIVENESS_PATH = "/api/v1/health/live"


@dataclass(frozen=True)
class E2EBackendIdentity:
    port: int
    marker_header: str
    marker: str


def load_identity(path: Path = IDENTITY_FILE) -> E2EBackendIdentity:
    data = json.loads(path.read_text(encoding="utf-8"))
    return E2EBackendIdentity(
        port=int(data["port"]),
        marker_header=str(data["markerHeader"]),
        marker=str(data["marker"]),
    )


class LivenessMarker:
    """Put the e2e marker header on liveness responses, and nowhere else."""

    def __init__(self, app: ASGIApp, *, header: str, marker: str) -> None:
        self.app = app
        self._header = (header.lower().encode("latin-1"), marker.encode("latin-1"))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != LIVENESS_PATH:
            await self.app(scope, receive, send)
            return

        async def send_marked(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", ()), self._header]}
            await send(message)

        await self.app(scope, receive, send_marked)


def build_app(identity: E2EBackendIdentity | None = None) -> FastAPI:
    identity = identity or load_identity()
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("127.0.0.1", "localhost", "::1", "testserver"),
        handoff_secret=HANDOFF_SECRET,
    )
    app.add_middleware(LivenessMarker, header=identity.marker_header, marker=identity.marker)
    return app


def main(argv: list[str] | None = None) -> int:
    identity = load_identity()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=identity.port)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")

    uvicorn.run(build_app(identity), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
