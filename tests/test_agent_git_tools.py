"""Real temporary Git repositories with a subprocess-backed test launcher.

The test launcher stands in for Windows AppContainer on all platforms, and
records the security contract. It never creates an AppContainer profile.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from threading import Event
from time import monotonic
from typing import Any

import pytest

from cortex_backend.agent.protocol import ToolCall
from cortex_backend.agent.tool_registry import ToolContext, ToolRegistry, ToolResult, UnregisteredToolError
from cortex_backend.agent.tools.git import GIT_TOOLS, register_git_tools
from cortex_backend.agent.tools import git as git_tools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required for agent Git tests")


def _env(tmp_path: Path) -> dict[str, str]:
    git_path = shutil.which("git")
    assert git_path is not None
    home = tmp_path / "empty-home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.pathsep.join([str(Path(git_path).parent), os.environ.get("PATH", "")]),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, name: str = "copy") -> tuple[Path, dict[str, str]]:
    root = tmp_path / name
    root.mkdir()
    env = _env(tmp_path)
    _git(root, "init", "-q", env=env)
    (root / "tracked.txt").write_text("first\n", encoding="utf-8")
    _git(root, "add", "--", "tracked.txt", env=env)
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
         "-c", "core.hooksPath=", "commit", "-qm", "baseline", env=env)
    return root, env


class _CompletedHandle:
    level = "appcontainer"
    pid = 1

    def __init__(self, result: subprocess.CompletedProcess[bytes]) -> None:
        self._result = result

    def stdout_chunks(self):
        for index in range(0, len(self._result.stdout), 8192):
            yield self._result.stdout[index:index + 8192]

    def wait(self, timeout: float | None = None) -> int:
        return self._result.returncode

    def kill_tree(self) -> None:
        return None


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Execute actual Git, but no Windows profile or unsandboxed fallback."""
    invocations: list[tuple[Path, list[str], bool]] = []
    env = _env(tmp_path)

    class _LocalLauncher:
        def __init__(self, workcopy: str | Path, run_id: str) -> None:
            self.root = Path(workcopy).resolve()
            assert run_id.startswith("git-test-")

        def start(
            self, argv: list[str], cwd: str | Path, env: object = None,
            network: bool = False, limits: object = None,
        ) -> _CompletedHandle:
            assert env is None and limits is None
            assert network is False
            assert Path(cwd).resolve() == self.root
            assert argv[0] == "git"
            assert "-c" in argv
            assert any(argv[i:i + 2] == ["-c", "core.hooksPath="] for i in range(len(argv) - 1))
            assert any(argv[i:i + 2] == ["-c", "core.fsmonitor=false"] for i in range(len(argv) - 1))
            invocations.append((self.root, argv[:], network))
            result = subprocess.run(
                argv, cwd=cwd, env=env_for_child, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10, check=False,
            )
            return _CompletedHandle(result)

    env_for_child = env
    monkeypatch.setattr(git_tools, "AppContainerLauncher", _LocalLauncher)
    return invocations


def _registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_git_tools(registry, root, "git-test-run")
    return registry


def _call(registry: ToolRegistry, name: str, **arguments: Any) -> tuple[ToolResult, dict[str, Any]]:
    outcome = registry.execute(
        ToolCall("test-call", name, arguments), ToolContext(Event(), monotonic() + 60),
    )
    assert isinstance(outcome, ToolResult)
    data: dict[str, Any] = json.loads(outcome.content)
    return outcome, data


def test_explicit_registration_and_no_network_commands(tmp_path: Path, harness) -> None:
    repo, _ = _repo(tmp_path)
    registry = _registry(repo)
    expected = {"git_status", "git_diff", "git_log", "git_show", "git_commit"}
    assert {spec.name for spec in GIT_TOOLS} == expected
    assert {spec.name for spec in registry.specs()} == expected
    assert all(spec.json_schema["additionalProperties"] is False for spec in registry.specs())
    for verb in ("push", "fetch", "pull", "remote", "clone", "submodule", "config", "credential"):
        with pytest.raises(UnregisteredToolError):
            _call(registry, f"git_{verb}")
    assert harness == []


def test_basic_read_tools_and_all_runs_scoped(tmp_path: Path, harness) -> None:
    repo, _ = _repo(tmp_path)
    registry = _registry(repo)
    (repo / "tracked.txt").write_text("second\n", encoding="utf-8")
    for name, args in (
        ("git_status", {}), ("git_diff", {}),
        ("git_diff", {"path": "tracked.txt"}),
        ("git_log", {"n": 1}), ("git_show", {"rev": "HEAD", "path": "tracked.txt"}),
    ):
        result, data = _call(registry, name, **args)
        assert result.success, (name, data)
        assert data["truncated"] is False
    _, status = _call(registry, "git_status")
    assert "tracked.txt" in status["output"]
    _, diff = _call(registry, "git_diff", path="tracked.txt")
    assert "+second" in diff["output"]
    _, shown = _call(registry, "git_show", rev="HEAD", path="tracked.txt")
    assert "first" in shown["output"]
    assert all(root == repo and network is False for root, _argv, network in harness)
    assert not any("https://" in arg for _, argv, _ in harness for arg in argv)


def test_pre_commit_hook_never_runs_and_commit_stays_inside_copy(tmp_path: Path, harness) -> None:
    repo, _ = _repo(tmp_path)
    outside, outside_env = _repo(tmp_path, "outside")
    original_head = _git(outside, "rev-parse", "HEAD", env=outside_env)
    marker = tmp_path / "hook-executed"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf fired > {shlex.quote(str(marker))}\n", encoding="utf-8")
    hook.chmod(0o755)
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    registry = _registry(repo)
    result, data = _call(registry, "git_commit", message="safe local commit")
    assert result.success, data
    assert not marker.exists()
    assert _git(repo, "show", "HEAD:tracked.txt", env=outside_env) == "committed"
    assert _git(outside, "rev-parse", "HEAD", env=outside_env) == original_head
    assert (outside / "tracked.txt").read_text(encoding="utf-8") == "first\n"
    assert _git(outside, "status", "--porcelain", env=outside_env) == ""
    assert all(root == repo for root, _, _ in harness)


def test_attributes_filter_never_runs_on_diff_or_commit(tmp_path: Path, harness) -> None:
    repo, env = _repo(tmp_path)
    marker = tmp_path / "filter-executed"
    (repo / ".gitattributes").write_text("*.txt filter=poison\n", encoding="utf-8")
    _git(repo, "add", "--", ".gitattributes", env=env)
    _git(repo, "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
         "-c", "core.hooksPath=", "commit", "-qm", "attributes", env=env)
    command = f"sh -c 'printf fired > {shlex.quote(str(marker))}; cat'"
    _git(repo, "config", "--local", "filter.poison.clean", command, env=env)
    _git(repo, "config", "--local", "filter.poison.smudge", command, env=env)
    _git(repo, "config", "--local", "filter.poison.process", command, env=env)
    _git(repo, "config", "--local", "filter.poison.required", "true", env=env)
    (repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
    registry = _registry(repo)
    result, diff = _call(registry, "git_diff")
    assert result.success, diff
    assert "+modified" in diff["output"]
    assert not marker.exists()
    result, status = _call(registry, "git_status")
    assert result.success, status
    assert not marker.exists()
    result, commit = _call(registry, "git_commit", message="no configured filter")
    assert result.success, commit
    assert not marker.exists()
    assert _git(repo, "show", "HEAD:tracked.txt", env=env) == "modified"
    assert any("filter.poison.clean=" in argv for _, argv, _ in harness)
    assert any("filter.poison.process=" in argv for _, argv, _ in harness)


@pytest.mark.parametrize("name,arguments", [
    ("git_diff", {"path": "--output=/tmp/out"}),
    ("git_diff", {"path": "../outside.txt"}),
    ("git_diff", {"path": "/etc/passwd"}),
    ("git_show", {"rev": "--help", "path": "tracked.txt"}),
    ("git_show", {"rev": "-c", "path": "tracked.txt"}),
    ("git_show", {"rev": "HEAD", "path": "-p"}),
    ("git_show", {"rev": "HEAD", "path": "../outside.txt"}),
])
def test_option_and_path_injection_rejected(
    tmp_path: Path, harness, name: str, arguments: dict[str, str],
) -> None:
    repo, _ = _repo(tmp_path)
    result, data = _call(_registry(repo), name, **arguments)
    assert result.success is False
    assert data["error"] in {"invalid_path", "invalid_rev"}
    assert harness == []


def test_git_log_caps_count_at_50(tmp_path: Path, harness) -> None:
    repo, env = _repo(tmp_path)
    for index in range(55):
        (repo / "tracked.txt").write_text(f"{index}\n", encoding="utf-8")
        _git(repo, "add", "--", "tracked.txt", env=env)
        _git(repo, "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
             "-c", "core.hooksPath=", "commit", "-qm", f"commit {index}", env=env)
    registry = _registry(repo)
    result, data = _call(registry, "git_log", n=200)
    assert result.success, data
    assert len(data["output"].splitlines()) == 50
    assert any("--max-count=50" in argv for _, argv, _ in harness)
    for value in (-1, 0, True, "50"):
        result, data = _call(registry, "git_log", n=value)
        assert result.success is False
        assert data["error"] == "invalid_arguments"


def test_bounded_output_and_unsafe_repository_config(tmp_path: Path, harness) -> None:
    repo, env = _repo(tmp_path)
    (repo / "tracked.txt").write_text("text\n" * 10000, encoding="utf-8")
    registry = _registry(repo)
    result, data = _call(registry, "git_diff")
    assert result.success, data
    assert data["truncated"] is True
    assert len(data["output"].encode("utf-8")) <= git_tools.MAX_OUTPUT_BYTES
    count = len(harness)
    _git(repo, "config", "--local", "include.path", str(tmp_path / "outside-config"), env=env)
    result, data = _call(registry, "git_status")
    assert result.success is False
    assert data["error"] == "unsafe_repository"
    assert len(harness) == count


def test_linked_worktree_uses_local_history_without_taking_original_git_objects(
    tmp_path: Path, harness,
) -> None:
    repo, env = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", "-q", str(linked), env=env)
    original_head = _git(repo, "rev-parse", "HEAD", env=env)
    registry = _registry(linked)
    result, data = _call(registry, "git_status")
    assert result.success, data
    assert (linked / ".cortex-agent-git" / "HEAD").is_file()
    assert (linked / ".git").is_file()  # The original worktree pointer is preserved.
    (linked / "tracked.txt").write_text("local only\n", encoding="utf-8")
    result, data = _call(registry, "git_commit", message="local work-copy commit")
    assert result.success, data
    assert _git(repo, "rev-parse", "HEAD", env=env) == original_head
    assert _git(repo, "show", "HEAD:tracked.txt", env=env) == "first"
    assert _git(repo, "status", "--porcelain", env=env) == ""
    assert _git(linked, "--git-dir=.cortex-agent-git", "--work-tree=.",
                "-c", "core.bare=false", "show", "HEAD:tracked.txt", env=env) == "local only"
    assert all(root == linked for root, _, _ in harness)
    assert all("--git-dir=.git" not in argv for _, argv, _ in harness)


def test_production_git_environment_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex_backend.agent.sandbox.appcontainer import _git_environment

    repo, _ = _repo(tmp_path)
    outside = tmp_path / "malicious-gitconfig"
    outside.write_text('[alias]\n  status = !echo injected\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(outside))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_ASKPASS", str(outside))
    isolated = _git_environment(repo)
    assert isolated["GIT_CONFIG_GLOBAL"] == os.devnull
    assert isolated["GIT_CONFIG_NOSYSTEM"] == "1"
    assert isolated["GIT_NO_LAZY_FETCH"] == "1"
    assert isolated["GIT_TERMINAL_PROMPT"] == "0"
    assert Path(isolated["HOME"]).is_relative_to(repo)
    assert str(outside) not in isolated.values()
    assert "GIT_ASKPASS" not in isolated
