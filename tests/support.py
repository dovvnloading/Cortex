"""Shared helpers for the headless API test suite."""

from __future__ import annotations

from fastapi.testclient import TestClient


def session_headers(client: TestClient, app) -> dict[str, str]:
    """Exchange the app's bootstrap token for bearer auth headers."""

    response = client.post(
        "/api/v1/session/exchange",
        json={"bootstrap_token": app.state.session_manager.bootstrap_token},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['session_token']}"}
