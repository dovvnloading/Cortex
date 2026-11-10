"""Authenticated workflow route tests (no live Ollama model or project writes)."""

from __future__ import annotations

from hashlib import sha256

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
    assert spec["paths"]["/api/v1/workspaces/{session_id}/refresh"]["post"]["security"] == [
        {"CortexSession": []}
    ]
    assert "/api/v1/workspaces/{session_id}" in spec["paths"]


def test_refresh_api_keeps_first_checkpoint_across_followup_edit(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    source = project / "answer.py"
    source.write_bytes(b"def value():\r\n    return 42\r\n")
    original = source.read_bytes()
    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))
    with TestClient(app) as client:
        headers = session_headers(client, app)
        opened = client.post("/api/v1/workspaces", headers=headers, json={
            "root_path": str(project), "confirmed": True,
        })
        assert opened.status_code == 200
        session = opened.json()["session_id"]
        path = f"/api/v1/workspaces/{session}"
        first = client.post(path + "/replace", headers=headers, json={
            "path": "answer.py",
            "expected_sha256": sha256(original).hexdigest(),
            "old_text": "return 42", "new_text": "return 43",
        })
        assert first.status_code == 200
        applied = client.post(path + "/apply", headers=headers, json={"confirmed": True})
        assert applied.status_code == 200
        assert source.read_bytes().endswith(b"43\r\n")

        denied = client.post(path + "/refresh", headers=headers, json={"confirmed": False})
        assert denied.status_code == 403
        refreshed = client.post(path + "/refresh", headers=headers, json={"confirmed": True})
        assert refreshed.status_code == 200
        assert refreshed.json()["session_id"] == session
        assert refreshed.json()["undo_available"] is True
        assert refreshed.json()["state"] == "open"
        reread = client.post(path + "/read", headers=headers, json={"path": "answer.py"})
        assert "return 43" in reread.json()["text"]

        next_patch = client.post(path + "/replace", headers=headers, json={
            "path": "answer.py",
            "expected_sha256": sha256(source.read_bytes()).hexdigest(),
            "old_text": "return 43", "new_text": "return 44",
        })
        assert next_patch.status_code == 200
        second = client.post(path + "/apply", headers=headers, json={"confirmed": True})
        assert second.status_code == 200
        assert source.read_bytes().endswith(b"44\r\n")
        restored = client.post(path + "/undo", headers=headers, json={"confirmed": True})
        assert restored.status_code == 200
        assert source.read_bytes() == original
