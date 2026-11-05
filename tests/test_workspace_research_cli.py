"""The experimental CLI requires an affirmative, one-shot read grant."""

from __future__ import annotations

import json

import pytest

from tools import workspace_research


def _project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "api.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    return root


def test_cli_requires_an_explicit_read_only_grant_before_any_inspection(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("must not inspect without consent")

    monkeypatch.setattr(workspace_research.WorkspaceSnapshotRegistry, "capture", forbidden)
    with pytest.raises(SystemExit) as error:
        workspace_research.main(["--root", str(root), "--list"])
    assert error.value.code == 2
    assert "--allow-read-only is required" in capsys.readouterr().err


def test_cli_lists_and_searches_a_snapshot_without_model_calls(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)
    monkeypatch.setattr(workspace_research, "_local_client", lambda: pytest.fail("Ollama must not start"))
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only", "--list", "--json",
    ]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["paths"] == ["api.py"]
    assert data["file_count"] == 1
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only", "--search", "return", "--json",
    ]) == 0
    matches = json.loads(capsys.readouterr().out)["matches"]
    assert matches[0]["path"] == "api.py"
    assert matches[0]["line"] == 2


def test_cli_reads_only_an_indexed_relative_file(tmp_path, capsys):
    root = _project(tmp_path)
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only",
        "--read", "api.py", "--start-line", "2", "--max-lines", "1",
    ]) == 0
    text = json.loads(capsys.readouterr().out)
    assert text["text"] == "2:     return 42"
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only", "--read", "../api.py",
    ]) == 2
    assert "invalid_file" in capsys.readouterr().err


def test_question_uses_scripted_client_and_returns_a_bounded_answer(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path)
    calls = []

    class LocalScript:
        def chat(self, *, model, messages, options, **kwargs):
            calls.append((model, len(messages), dict(options)))
            return {"message": {"content": '{"action":"final","answer":"See api.py:2."}'}}

    monkeypatch.setattr(workspace_research, "_local_client", LocalScript)
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only", "--model", "fake-local",
        "--question", "Where is 42 returned?", "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert result["tool_calls"] == 0
    assert result["answer"] == "See api.py:2."
    assert calls == [("fake-local", 2, {"num_ctx": workspace_research.DEFAULT_NUM_CTX})]


def test_question_requires_an_explicit_local_model(tmp_path):
    with pytest.raises(SystemExit) as error:
        workspace_research.main([
            "--root", str(tmp_path), "--allow-read-only", "--question", "Analyze this.",
        ])
    assert error.value.code == 2


def test_cli_patch_preview_returns_a_diff_without_modifying_checkout(tmp_path, monkeypatch, capsys):
    from hashlib import sha256

    root = _project(tmp_path)
    file = root / "api.py"
    original = file.read_bytes()
    digest = sha256(original).hexdigest()

    class PatchScript:
        def __init__(self):
            self.step = 0

        def chat(self, *, model, messages, options, **kwargs):
            self.step += 1
            if self.step == 1:
                action = {"action": "read", "path": "api.py"}
            elif self.step == 2:
                action = {
                    "action": "replace",
                    "path": "api.py",
                    "expected_sha256": digest,
                    "old_text": "return 42",
                    "new_text": "return 43",
                }
            else:
                action = {"action": "final", "answer": "Proposed changing the return value."}
            return {"message": {"content": json.dumps(action)}}

    monkeypatch.setattr(workspace_research, "_local_client", PatchScript)
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only", "--model", "fake-local",
        "--question", "Propose changing return 42 to 43.", "--preview-patch",
        "--json",
    ]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "completed"
    assert data["preview_replacements"] == 1
    assert data["applied_to_checkout"] is False
    assert "return 43" in data["preview_diff"]
    assert file.read_bytes() == original


def test_cli_patch_preview_is_an_explicit_question_only_option(tmp_path):
    with pytest.raises(SystemExit) as error:
        workspace_research.main([
            "--root", str(tmp_path),
            "--allow-read-only",
            "--list",
            "--preview-patch",
        ])
    assert error.value.code == 2


def test_cli_does_not_report_success_if_no_patch_was_staged_in_preview_mode(
    tmp_path, monkeypatch, capsys,
):
    root = _project(tmp_path)

    class NoChanges:
        def chat(self, *, model, messages, options, **kwargs):
            return {"message": {"content": '{"action":"final","answer":"Nothing to change."}'}}

    monkeypatch.setattr(workspace_research, "_local_client", NoChanges)
    assert workspace_research.main([
        "--root", str(root), "--allow-read-only", "--model", "fake-local",
        "--question", "Propose a code change.", "--preview-patch",
    ]) == 1
    printed = capsys.readouterr()
    assert "No patch was staged" in printed.err
    assert (root / "api.py").read_text(encoding="utf-8").endswith("return 42\n")
