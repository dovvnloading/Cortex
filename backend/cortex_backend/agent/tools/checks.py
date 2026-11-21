"""Detect project checks from disposable source files; execute only via ShellTools.

Manifests are data, never an executable command supplied by a model. The
work-copy snapshot has bounded reads and refuses links/reparse points. Each
check is an independent approval-gated tool call so multiple approvals never
share a one-shot grant.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Final, Literal

from ..policy import classify
from ..protocol import ToolCall, ToolSpec
from ..sandbox.base import canonical_workcopy
from ..tool_registry import NeedsApproval, ToolContext, ToolRegistry, ToolResult
from ..workcopy import _snapshot
from cortex_backend.services.workspace_context import _safe_root
from .shell import ShellTools

_CHECK_TIMEOUT_S: Final = 60.0
_JS_SCRIPTS: Final = ("test", "lint", "build", "typecheck")


@dataclass(frozen=True, slots=True)
class DetectedCheck:
    key: str
    command: str
    # Opaque project hooks can run code that the fixed command conceals.
    risk: Literal["safe", "ask", "network", "dangerous"] = "safe"


def _quoted(value: str) -> str:
    """Literal PowerShell argument; never interpolate a manifest path."""
    return "'" + value.replace("'", "''") + "'"


def _files(root: str | os.PathLike[str]) -> dict[str, bytes]:
    """Only content in the verified, bounded disposable work copy is visible."""
    # Refuse a swapped symlink/junction root before canonicalizing it. A
    # resolved outside directory must never become the detection input.
    requested = Path(root)
    verified = _safe_root(str(requested))
    source = _snapshot(canonical_workcopy(verified))
    return {name: item.contents for name, item in source.items()}


def workcopy_fingerprint(root: str | os.PathLike[str]) -> str:
    """Content (not mtime) identity, so a tool edit invalidates prior checks."""
    digest = sha256()
    for name, contents in sorted(_files(root).items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256(contents).digest())
    return digest.hexdigest()


def detect_checks(root: str | os.PathLike[str]) -> tuple[DetectedCheck, ...]:
    """Build only fixed, code-owned commands from work-copy manifest presence.

    JavaScript script *names* are allowlisted; script bodies are never turned
    into check commands. The sandbox must handle their execution as untrusted.
    """
    files = _files(root)
    checks: list[DetectedCheck] = []
    if any(PurePosixPath(p).name in {"pyproject.toml", "pytest.ini", "setup.cfg"} for p in files):
        # Pytest addopts can load arbitrary plugins; conftest is itself executable.
        hooks = any(PurePosixPath(p).name == "conftest.py" for p in files)
        options = any(
            PurePosixPath(p).name in {"pyproject.toml", "pytest.ini", "setup.cfg"}
            and re.search(rb"(?m)^\s*addopts\s*=", body) is not None
            for p, body in files.items()
        )
        checks.append(DetectedCheck("pytest", "python -m pytest", "ask" if hooks or options else "safe"))

    for path in sorted(files):
        item = PurePosixPath(path)
        directory = str(item.parent)
        if item.name == "package.json":
            try:
                manifest = json.loads(files[path].decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                continue
            scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
            if not isinstance(scripts, dict):
                continue
            for name in _JS_SCRIPTS:
                script = scripts.get(name)
                if not isinstance(script, str) or not script.strip():
                    continue
                command = f"npm run {name}" if directory == "." else f"npm --prefix {_quoted(directory)} run {name}"
                # The launcher sees "npm run test", not the script body.
                # npm also executes lifecycle pre/post scripts for each target.
                # Classify explicit danger/network intent in all three before
                # the wrapper can gain automatic build/test permission.
                script_classes = []
                for phase in (f"pre{name}", name, f"post{name}"):
                    body = scripts.get(phase)
                    if isinstance(body, str) and body.strip():
                        script_classes.append(classify(body, "cmd", "appcontainer").cls)
                risk: Literal["safe", "network", "dangerous"] = "safe"
                if "dangerous" in script_classes:
                    risk = "dangerous"
                elif "network" in script_classes:
                    risk = "network"
                checks.append(DetectedCheck(f"npm:{name}:{path}", command, risk))
        elif item.name == "Cargo.toml":
            command = "cargo test" if directory == "." else f"cargo test --manifest-path {_quoted(path)}"
            build_hook = str(item.parent / "build.rs") in files or re.search(
                rb"(?m)^\s*build\s*=", files[path],
            ) is not None
            checks.append(DetectedCheck(f"cargo:{path}", command, "ask" if build_hook else "safe"))
        elif item.name == "go.mod":
            command = "go test ./..." if directory == "." else f"go -C {_quoted(directory)} test ./..."
            checks.append(DetectedCheck(f"go:{path}", command))

    # A solution already contains its projects; do not run every csproj again.
    solutions = sorted(path for path in files if path.casefold().endswith(".sln"))
    projects = sorted(path for path in files if path.casefold().endswith(".csproj"))
    # MSBuild targets can execute commands before tests, including when a
    # solution rather than an individual project is selected.
    build_hooks = any(
        b"<exec" in files[path].lower()
        for path in files
        if path.casefold().endswith((".csproj", "directory.build.targets", "directory.build.props"))
    )
    for path in solutions or projects:
        checks.append(DetectedCheck(
            f"dotnet:{path}", f"dotnet test {_quoted(path)}", "ask" if build_hooks else "safe",
        ))
    return tuple(checks)


CHECK_TOOL: Final = ToolSpec(
    "run_checks",
    "Run a detected work-copy project check by index. Commands come only from local manifests, "
    "never model arguments; execution uses the policy-gated, network-disabled shell.",
    {
        "type": "object",
        "properties": {"index": {"type": "integer", "minimum": 0}},
        "required": [],
        "additionalProperties": False,
    },
)


class CheckTools:
    def __init__(self, workcopy: str | os.PathLike[str], shell: ShellTools) -> None:
        root = canonical_workcopy(Path(workcopy))
        if root != shell.workcopy:
            raise ValueError("checks and shell must use the same work copy")
        self.root = root
        self.shell = shell

    def handle(self, call: ToolCall, context: ToolContext) -> ToolResult | NeedsApproval:
        index = call.arguments.get("index", 0)
        if set(call.arguments) - {"index"} or type(index) is not int or index < 0:
            return ToolResult('{"status":"not_run","reason":"invalid_arguments"}', success=False)
        try:
            checks = detect_checks(self.root)
        except (OSError, ValueError, RuntimeError):
            return ToolResult('{"status":"not_run","reason":"workcopy_unavailable"}', success=False)
        if not checks:
            return ToolResult('{"status":"not_run","reason":"no_test_command_found"}')
        if index >= len(checks):
            return ToolResult('{"status":"not_run","reason":"invalid_index"}', success=False)
        check = checks[index]
        # Crucially, call the *same* ShellTools path as run_command. Its
        # classify/decide, one-shot approval and AppContainer launcher govern
        # every attempt; never start a process here or enable network.
        outcome = self.shell.handle(ToolCall(call.id, "run_command", {
            "command": check.command, "timeout_s": _CHECK_TIMEOUT_S,
            "network": False, "shell": "powershell",
        }), context, check_risk=check.risk)
        if isinstance(outcome, NeedsApproval):
            return NeedsApproval({**outcome.request, "tool": "run_checks"})
        try:
            detail = json.loads(outcome.content)
        except (ValueError, TypeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        timed_out = detail.get("timed_out") is True
        exit_code = detail.get("exit_code")
        status = (
            "timed_out" if timed_out else "passed" if outcome.success and exit_code == 0
            else "failed" if isinstance(exit_code, int) else "not_run"
        )
        result = {
            "check": check.key, "status": status,
            "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            "timed_out": timed_out,
            "output": str(detail.get("output", ""))[:4096],
        }
        if status == "not_run":
            result["reason"] = str(detail.get("error", "shell_unavailable"))[:80]
        return ToolResult(
            json.dumps(result, ensure_ascii=False),
            summary="Project check passed" if status == "passed" else "Project check not passed",
            success=status == "passed",
        )


def register_check_tools(
    registry: ToolRegistry, workcopy: str | os.PathLike[str], shell: ShellTools,
) -> None:
    """Offer one code-owned check selector alongside the registered shell."""
    checks = CheckTools(workcopy, shell)
    registry.register(CHECK_TOOL, checks.handle)
