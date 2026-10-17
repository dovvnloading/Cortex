"""The browser tests' real backend has its own port and names itself."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from scripts import e2e_backend

ROOT = e2e_backend.ROOT


def test_the_liveness_route_carries_the_marker_and_nothing_else_does() -> None:
    identity = e2e_backend.load_identity()

    with TestClient(e2e_backend.build_app(identity)) as client:
        live = client.get("/api/v1/health/live")
        unauthenticated = client.get("/api/v1/settings")

    assert live.status_code == 200
    assert live.headers[identity.marker_header] == identity.marker
    assert unauthenticated.status_code == 401
    assert identity.marker_header not in unauthenticated.headers


def test_the_port_is_not_the_screenshot_servers() -> None:
    """The README demo server defaults to 8799; a demo left running there was
    silently reused as "the real API"."""

    port = e2e_backend.load_identity().port
    demo_default = re.search(
        r'add_argument\("--port", type=int, default=(\d+)\)',
        (ROOT / "tools" / "screenshots" / "demo_server.py").read_text(encoding="utf-8"),
    )

    assert demo_default is not None
    assert port != int(demo_default.group(1))
    assert 1024 <= port <= 65535


def test_the_playwright_config_takes_the_port_from_the_shared_file() -> None:
    config = (ROOT / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")

    # No literal backend port may creep back into the config.
    assert not re.search(r"\b87\d\d\b", config)
    assert "E2E_BACKEND.port" in config
