"""Regression check for retiring the Project workspace route without losing recovery."""

from __future__ import annotations

from pathlib import Path


def test_legacy_project_url_enters_agent_recovery_without_changing_chat_routes():
    root = Path(__file__).resolve().parents[1]
    navigation = (root / "frontend/src/lib/navigation.ts").read_text(encoding="utf-8")
    app = (root / "frontend/src/app/App.tsx").read_text(encoding="utf-8")
    shell = (root / "frontend/src/features/shell/AppShell.tsx").read_text(encoding="utf-8")
    agent = (root / "frontend/src/features/agent/AgentPage.tsx").read_text(encoding="utf-8")
    recovery = (root / "frontend/src/features/agent/SavedRecoveryPanel.tsx").read_text(encoding="utf-8")
    client = (root / "frontend/src/api/client.ts").read_text(encoding="utf-8")

    assert 'normalized === "/workspace") return { kind: "agent" }' in navigation
    assert 'normalized === "/agent") return { kind: "agent" }' in navigation
    assert 'normalized === "/chat/new"' in navigation
    assert 'kind: "workspace"' not in navigation
    assert 'route.kind === "workspace"' not in app
    assert "WorkspacePage" not in app
    assert 'navigate("/agent", { replace: true })' in app
    assert 'to="/workspace"' not in shell
    assert 'to="/agent"' in shell
    assert "SavedRecoveryPanel" in agent
    assert "<SavedRecoveryPanel api={api} />" in agent
    for method in ("workspaceCheckpoints", "restoreWorkspaceCheckpoint", "discardWorkspaceCheckpoint"):
        assert method in recovery
        assert method in client
    assert not (root / "frontend/src/features/workspace/WorkspacePage.tsx").exists()
