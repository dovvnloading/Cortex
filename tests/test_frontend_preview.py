"""Preview static serving stays optional and never intercepts API routes."""

from pathlib import Path

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies


def test_preview_serves_frontend_bundle_and_preserves_api_boundary(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div></body></html>",
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('cortex');", encoding="utf-8")
    app = create_app(
        build_demo_dependencies(),
        allowed_hosts=("testserver", "127.0.0.1", "localhost", "::1"),
        serve_frontend=True,
        frontend_dist=dist,
    )

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/settings").status_code == 200
        assert client.get("/assets/app.js").text == "console.log('cortex');"
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/unknown").status_code == 404


def test_unknown_api_paths_are_json_404_even_when_the_spa_is_mounted(tmp_path: Path):
    """An unknown ``/api/`` path is answered the way a headless app answers it.

    The catch-all that serves the SPA used to reply to these with ``index.html``
    and a 404, so a client that asked for JSON got HTML, and the packaged app
    disagreed with the headless one about the same request.
    """

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")
    hosts = ("testserver", "127.0.0.1", "localhost", "::1")
    packaged = create_app(
        build_demo_dependencies(), allowed_hosts=hosts, serve_frontend=True, frontend_dist=dist
    )
    headless = create_app(build_demo_dependencies(), allowed_hosts=hosts)

    with TestClient(packaged) as spa, TestClient(headless) as bare:
        for path in ("/api/v1/unknown", "/api/v1/chats/one/two/three", "/api/", "/api/v2/health"):
            served = spa.get(path)
            assert served.status_code == 404, path
            assert served.headers["content-type"].startswith("application/json"), path
            assert "<html" not in served.text.lower(), path
            assert served.json() == {"detail": "Not Found"}, path
            assert served.json() == bare.get(path).json(), path

        # The SPA still owns everything that is not the API.
        page = spa.get("/settings")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert spa.get("/api-notes").status_code == 200
