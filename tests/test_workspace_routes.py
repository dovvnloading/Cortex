"""Authenticated workflow route tests (no live Ollama model or project writes)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cortex_backend.api import create_app
from cortex_backend.testing import build_demo_dependencies
from support import session_headers


def test_workspace_requires_bearer_and_explicit_consent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "answer.py").write_text("answer = 42\n", encoding="utf-8")
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        target = "/api/v1/workspaces"
        no_auth = client.post(target, json={"root_path": str(project), "confirmed": True})
        assert no_auth.status_code == 401
        headers = session_headers(client, app)
        refused = client.post(
            target, headers=headers, json={"root_path": str(project), "confirmed": False},
        )
        assert refused.status_code == 403
        accepted = client.post(
            target, headers=headers, json={"root_path": str(project), "confirmed": True},
        )
        assert accepted.status_code == 200
        session_id = accepted.json()["session_id"]
        assert accepted.json()["file_count"] == 1
        listed = client.get(
            f"/api/v1/workspaces/{session_id}/paths", headers=headers,
        )
        assert listed.json()["paths"] == ["answer.py"]
        # No route makes a model-generated patch executable without another
        # explicit write grant.
        denied = client.post(
            f"/api/v1/workspaces/{session_id}/apply",
            headers=headers, json={"confirmed": False},
        )
        assert denied.status_code == 403
        assert (project / "answer.py").read_text(encoding="utf-8") == "answer = 42\n"
        revoked = client.delete(f"/api/v1/workspaces/{session_id}", headers=headers)
        assert revoked.status_code == 200
        assert client.get(f"/api/v1/workspaces/{session_id}/paths", headers=headers).status_code == 404


def test_workspace_routes_are_described_in_the_generated_api_contract():
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    spec = app.openapi()
    assert spec["paths"]["/api/v1/workspaces"]["post"]["security"] == [
        {"CortexSession": []}
    ]
    assert "/api/v1/workspaces/{session_id}/checks" in spec["paths"]
    assert "/api/v1/workspaces/{session_id}" in spec["paths"]
