"""Preview static serving stays optional and never intercepts API routes."""

from pathlib import Path

from fastapi.testclient import TestClient

from cortex_backend.api import create_app


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
