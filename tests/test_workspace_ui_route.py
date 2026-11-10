"""Regression checks for the native workspace route and route selection."""

from __future__ import annotations

from pathlib import Path


def test_project_page_is_reachable_without_changing_the_chat_routes():
    root = Path(__file__).resolve().parents[1]
    navigation = (root / "frontend/src/lib/navigation.ts").read_text(encoding="utf-8")
    app = (root / "frontend/src/app/App.tsx").read_text(encoding="utf-8")
    shell = (root / "frontend/src/features/shell/AppShell.tsx").read_text(encoding="utf-8")
    assert 'normalized === "/workspace"' in navigation
    assert 'route.kind === "workspace"' in app
    assert 'to="/workspace"' in shell
