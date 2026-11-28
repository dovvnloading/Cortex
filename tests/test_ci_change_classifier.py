"""The Quality workflow's document-only classifier, run for real (issue #714).

The classifier decides whether a pull request may skip lint, tests and the
package build. These tests lift its shell script out of quality.yml and run it
against a scratch Git repository whose last commit is the change under test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "quality.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="requires bash and git",
)


def _classifier() -> tuple[str, str]:
    """Return the classifier step's script and its BROWSER_IF pattern."""

    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("      - name: Classify the changed files")
    browser_if = ""
    for offset, line in enumerate(lines[start:], start=start):
        stripped = line.strip()
        if stripped.startswith("BROWSER_IF:"):
            browser_if = stripped.split(":", 1)[1].strip().strip("'")
        if stripped == "run: |":
            indent = len(line) - len(line.lstrip()) + 2
            body: list[str] = []
            for script_line in lines[offset + 1 :]:
                if script_line.strip() and len(script_line) - len(script_line.lstrip()) < indent:
                    break
                body.append(script_line)
            return textwrap.dedent("\n".join(body)), browser_if
    raise AssertionError("classifier step not found in quality.yml")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _classify(tmp_path: Path, change: dict[str, str | None], moves: dict[str, str] | None = None) -> dict[str, str]:
    """Commit a base tree, then the change, and run the classifier on it."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "config", "core.autocrlf", "false")
    for name in ("backend/module.py", "frontend/src/app.ts", "docs/guide.md", "README.md"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n" * 20, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    for source, target in (moves or {}).items():
        (repo / target).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", source, target)
    for name, text in change.items():
        path = repo / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")

    script, browser_if = _classifier()
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "EVENT_NAME": "pull_request",
        "BROWSER_IF": browser_if,
        "GITHUB_OUTPUT": str(output),
    }
    subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True, capture_output=True, text=True)
    pairs = (line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
    return dict(pairs)


def test_moving_code_into_docs_still_runs_the_code_checks(tmp_path: Path) -> None:
    result = _classify(tmp_path, {}, moves={"backend/module.py": "docs/module.py"})

    assert result["code"] == "true"
    assert result["browser"] == "true"


def test_moving_frontend_code_into_docs_still_runs_the_browser_tests(tmp_path: Path) -> None:
    result = _classify(tmp_path, {}, moves={"frontend/src/app.ts": "docs/app.ts"})

    assert result["browser"] == "true"


def test_moving_a_document_into_code_runs_the_code_checks(tmp_path: Path) -> None:
    result = _classify(tmp_path, {}, moves={"docs/guide.md": "backend/guide.md"})

    assert result["code"] == "true"


def test_a_rename_within_docs_is_still_document_only(tmp_path: Path) -> None:
    result = _classify(tmp_path, {}, moves={"docs/guide.md": "docs/handbook.md"})

    assert result == {"code": "false", "browser": "false"}


def test_an_ordinary_document_edit_is_document_only(tmp_path: Path) -> None:
    result = _classify(tmp_path, {"README.md": "changed\n"})

    assert result == {"code": "false", "browser": "false"}


def test_an_ordinary_code_edit_runs_everything(tmp_path: Path) -> None:
    result = _classify(tmp_path, {"backend/module.py": "changed\n"})

    assert result == {"code": "true", "browser": "true"}
