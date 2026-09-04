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

The handoff secret is fixed and printed nowhere. It is a test credential for a
loopback server that holds no real data, and the frontend needs it in the URL
to complete the exchange the way the launcher does.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from cortex_backend.api import create_app  # noqa: E402
from cortex_backend.testing import build_demo_dependencies  # noqa: E402

# Matches E2E_HANDOFF_SECRET in frontend/e2e/fixtures.ts.
HANDOFF_SECRET = "e2e-handoff-secret"


def build_app():
    return create_app(
        build_demo_dependencies(),
        allowed_hosts=("127.0.0.1", "localhost", "::1", "testserver"),
        handoff_secret=HANDOFF_SECRET,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")

    uvicorn.run(build_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
