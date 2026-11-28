"""The Dependency refresh workflow's publish step, run for real (#703, #720).

The step is lifted out of dependency-refresh.yml and run in PowerShell inside
a clone that, like the workflow's checkout, holds only main, against a local
bare repository standing in for GitHub. ``gh`` is a stub.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "dependency-refresh.yml"
BRANCH = "chore/dependency-refresh"
LOCKS = ("requirements.lock.txt", "requirements-dev.lock.txt", "frontend/package-lock.json")

needs_tools = pytest.mark.skipif(
    shutil.which("pwsh") is None or shutil.which("git") is None,
    reason="requires PowerShell 7 and git",
)

# `gh pr list` answers FAKE_GH_EXISTING; `gh pr create` is recorded.
FAKE_GH = r"""
function gh {
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'pr' -and $args[1] -eq 'list') { return $env:FAKE_GH_EXISTING }
    if ($args[0] -eq 'pr' -and $args[1] -eq 'create') {
        Set-Content -LiteralPath $env:FAKE_GH_CREATED -Value 'created'
        return
    }
    throw "unexpected gh call: $args"
}
"""


def _step_script() -> str:
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index("      - name: Open one pull request if anything changed")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")


def _remote_with_main(tmp_path: Path, *, refresh_branch: bool) -> Path:
    remote = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _identity(seed)
    for name in LOCKS:
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text("old\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", str(remote), "main")
    if refresh_branch:
        _git(seed, "checkout", "-q", "-b", BRANCH)
        (seed / LOCKS[0]).write_text("last month\n", encoding="utf-8")
        _git(seed, "commit", "-q", "-am", "earlier refresh")
        _git(seed, "push", "-q", str(remote), BRANCH)
    return remote


def _run(tmp_path: Path, remote: Path, *, existing: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    # Like actions/checkout: one branch, shallow.
    clone = tmp_path / "work"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--single-branch", "--branch", "main", remote.as_uri(), str(clone))
    _identity(clone)
    (clone / LOCKS[0]).write_text("new\n", encoding="utf-8")
    created = tmp_path / "created.txt"
    env = {
        **os.environ,
        "BRANCH": BRANCH,
        "FAKE_GH_EXISTING": existing,
        "FAKE_GH_CREATED": str(created),
    }
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", FAKE_GH + _step_script()],
        cwd=clone, capture_output=True, text=True, env=env, timeout=120, check=False,
    )
    return result, created


@needs_tools
def test_an_existing_refresh_branch_is_updated(tmp_path: Path) -> None:
    remote = _remote_with_main(tmp_path, refresh_branch=True)

    result, created = _run(tmp_path, remote, existing="7")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Updated the open pull request #7." in result.stdout
    tip = _git(tmp_path, "--git-dir", str(remote), "show", f"refs/heads/{BRANCH}:{LOCKS[0]}")
    assert tip == "new"
    # Built on main, not on last month's refresh commit.
    parent = _git(tmp_path, "--git-dir", str(remote), "rev-parse", f"refs/heads/{BRANCH}^")
    assert parent == _git(tmp_path, "--git-dir", str(remote), "rev-parse", "refs/heads/main")
    assert not created.exists()


@needs_tools
def test_a_first_refresh_creates_the_branch_and_the_pull_request(tmp_path: Path) -> None:
    remote = _remote_with_main(tmp_path, refresh_branch=False)

    result, created = _run(tmp_path, remote, existing="")

    assert result.returncode == 0, result.stdout + result.stderr
    tip = _git(tmp_path, "--git-dir", str(remote), "show", f"refs/heads/{BRANCH}:{LOCKS[0]}")
    assert tip == "new"
    assert created.exists()


def test_the_refresh_always_starts_from_main() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    checkout = text[text.index("- name: Check out repository") :]
    checkout = checkout[: checkout.index("- name: Set up Python")]

    assert "ref: refs/heads/main" in checkout
    assert "--base main" in text
