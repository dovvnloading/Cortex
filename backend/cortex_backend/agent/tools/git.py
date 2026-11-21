"""Allowlisted, bounded Git operations in an explicitly selected agent work copy.

Every Git process is launched through the network-disabled AppContainer. Git's
configuration is additionally isolated by AppContainerLauncher; repository
hooks, external diff tools, signing, fsmonitor and configured filters are
neutralized here. Linked worktrees use an isolated local Git directory,
never the metadata belonging to the original checkout.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Any, Final
from uuid import uuid4

from cortex_backend.agent.sandbox import AppContainerLauncher
from cortex_backend.agent.sandbox.base import canonical_workcopy
from cortex_backend.services.workspace_context import _is_reparse

from ..protocol import ToolCall, ToolSpec
from ..tool_registry import ToolContext, ToolInterrupted, ToolRegistry, ToolResult
from ..workcopy import WorkCopyError, _check_root, _valid_path

MAX_OUTPUT_BYTES: Final = 16_384
MAX_GIT_SECONDS: Final = 30.0
MAX_CONFIG_BYTES: Final = 1024 * 1024
_MAX_MESSAGE: Final = 2000
_MAX_FILTERS: Final = 128
_SECTION = re.compile(r'^\s*\[\s*([^\]]+)\]\s*(?:[#;].*)?$')
_FILTER = re.compile(r'^filter\s+"([A-Za-z0-9_.-]{1,128})"$', re.IGNORECASE)
_REV = re.compile(r'(?!-)[A-Za-z0-9@][A-Za-z0-9._/@{}^~:+-]{0,127}\Z')


def _schema(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object", "properties": {**required, **(optional or {})},
        "required": list(required), "additionalProperties": False,
    }


_PATH: Final = {"type": "string", "minLength": 1, "maxLength": 1024}
_REVISION: Final = {"type": "string", "minLength": 1, "maxLength": 128}
GIT_TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec("git_status", "Show bounded porcelain status of this work copy.", _schema({})),
    ToolSpec("git_diff", "Show bounded, filter-free changes in this work copy.", _schema({}, {"path": _PATH})),
    ToolSpec("git_log", "Show at most 50 recent local commits.",
             _schema({}, {"n": {"type": "integer", "minimum": 1, "maximum": 50}})),
    ToolSpec("git_show", "Show a revision's changes for one work-copy path, without external drivers.",
             _schema({"rev": _REVISION, "path": _PATH})),
    ToolSpec("git_commit", "Stage and commit changes in the work copy only; no hooks or filter drivers.",
             _schema({"message": {"type": "string", "minLength": 1, "maxLength": _MAX_MESSAGE}})),
)


class GitToolError(RuntimeError):
    """An error category that never includes a path or Git output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The agent Git operation was refused.")


def _string(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise GitToolError("invalid_arguments")
    return value


class _Git:
    def __init__(self, root: str | os.PathLike[str], run_id: str) -> None:
        self.root = canonical_workcopy(Path(root))
        info = self.root.lstat()
        self.identity = (info.st_dev, info.st_ino)
        if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id) is None:
            raise ValueError("invalid run ID")
        self.run_id = run_id
        self._git_dir = ".git"
        self._initialization_attempted = False
        self._lock = RLock()

    def _check(self, context: ToolContext) -> None:
        if context.stopped():
            raise ToolInterrupted("Git operation interrupted")
        _check_root(self.root, self.identity)

    def _path(self, value: str) -> str:
        if value.startswith("-") or len(value) > 1024:
            raise GitToolError("invalid_path")
        try:
            path = _valid_path(value)
        except WorkCopyError:
            raise GitToolError("invalid_path") from None
        # No symlink, junction or reparse component may redirect a Git path.
        cursor = self.root
        for part in path.split("/"):
            cursor /= part
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                break  # A missing pathspec is harmless; Git returns an error.
            if _is_reparse(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise GitToolError("invalid_path")
        return path

    def _filters(self) -> list[str]:
        """Inspect local config only; refuse external includes and Git-dir links.

        A working-tree .gitattributes can activate a named filter only when a
        command is configured for it. Disable every configured local driver;
        system/global config and attributes are suppressed by the launcher.
        """
        metadata = self.root / self._git_dir
        try:
            info = metadata.lstat()
        except FileNotFoundError:
            raise GitToolError("not_a_repository") from None
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise GitToolError("unsafe_repository")
        # A .git file, linked metadata directory, commondir or alternate
        # could send Git back to the source checkout or another directory.
        for name in ("objects", "objects/info", "objects/pack", "refs", "info", "hooks"):
            directory = metadata / name
            try:
                child = directory.lstat()
            except FileNotFoundError:
                continue
            if _is_reparse(child) or not stat.S_ISDIR(child.st_mode):
                raise GitToolError("unsafe_repository")
        for name in ("commondir", "objects/info/alternates"):
            if (metadata / name).exists() or (metadata / name).is_symlink():
                raise GitToolError("unsafe_repository")
        attributes = metadata / "info" / "attributes"
        try:
            attrs_info = attributes.lstat()
        except FileNotFoundError:
            pass
        else:
            if _is_reparse(attrs_info) or not stat.S_ISREG(attrs_info.st_mode):
                raise GitToolError("unsafe_repository")
        names: set[str] = set()
        for name in ("config", "config.worktree"):
            config = metadata / name
            try:
                info = config.lstat()
            except FileNotFoundError:
                continue
            if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
                raise GitToolError("unsafe_repository")
            try:
                contents = config.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise GitToolError("unsafe_repository") from None
            section = ""
            for line in contents.splitlines():
                match = _SECTION.fullmatch(line)
                if line.lstrip().startswith("[") and match is None:
                    raise GitToolError("unsafe_repository")
                if match is not None:
                    section = match.group(1).strip()
                    if re.match(r"include(?:If)?(?:\s|$)", section, re.IGNORECASE):
                        raise GitToolError("unsafe_repository")
                    if section.lower().startswith("filter "):
                        driver = _FILTER.fullmatch(section)
                        if driver is None:
                            raise GitToolError("unsafe_repository")
                        names.add(driver.group(1))
                        if len(names) > _MAX_FILTERS:
                            raise GitToolError("unsafe_repository")
                elif section.lower().startswith("include") and line.strip() and not line.lstrip().startswith(("#", ";")):
                    raise GitToolError("unsafe_repository")
        return sorted(names)

    def _argv(self, command: list[str]) -> list[str]:
        argv = [
            "git", "--no-pager", "--no-replace-objects", f"--git-dir={self._git_dir}", "--work-tree=.",
            "-c", "core.bare=false",
            "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
            "-c", "commit.gpgSign=false", "-c", "credential.helper=",
            "-c", "diff.external=", "-c", f"core.attributesFile={os.devnull}",
            "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
            "-c", f"safe.directory={self.root}",
            "-c", "user.name=Cortex Agent", "-c", "user.email=agent@cortex.invalid",
        ]
        for name in self._filters():
            for suffix, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
                argv.extend(("-c", f"filter.{name}.{suffix}={value}"))
        return [*argv, *command]

    def _ensure_repository(self, context: ToolContext) -> None:
        """Detach a linked worktree from the source project's object database.

        A Git worktree's .git is a pointer to metadata in the original
        checkout. Rather than follow it, create a disposable bare Git directory
        inside this copy and snapshot its current files into a local baseline.
        """
        if self._git_dir != ".git":
            return
        metadata = self.root / ".git"
        try:
            info = metadata.lstat()
        except FileNotFoundError:
            raise GitToolError("not_a_repository") from None
        if _is_reparse(info):
            raise GitToolError("unsafe_repository")
        if stat.S_ISDIR(info.st_mode):
            return
        if not stat.S_ISREG(info.st_mode) or self._initialization_attempted:
            raise GitToolError("unsafe_repository")
        shadow = self.root / ".cortex-agent-git"
        if shadow.exists() or shadow.is_symlink():
            raise GitToolError("unsafe_repository")
        self._initialization_attempted = True
        self._launch(
            ["git", "--no-pager", "-c", "core.hooksPath=", "-c", "core.fsmonitor=false",
             "-c", "commit.gpgSign=false", "init", "--bare", "-q", ".cortex-agent-git"],
            context,
        )
        self._git_dir = ".cortex-agent-git"
        excluded = shadow / "info" / "exclude"
        if _is_reparse(excluded.lstat()) or not excluded.is_file():
            raise GitToolError("unsafe_repository")
        excluded.write_text(
            ".cortex-agent-git/\n.cortex-agent-git-home/\n.cortex-agent-tmp/\n",
            encoding="utf-8",
        )
        self._launch(self._argv(["add", "--all", "--", "."]), context)
        self._launch(self._argv(["commit", "--allow-empty", "--no-gpg-sign", "-m", "Work-copy baseline"]),
                     context)

    def _run(self, command: list[str], context: ToolContext) -> tuple[str, bool]:
        with self._lock:
            self._check(context)
            self._ensure_repository(context)
            return self._launch(self._argv(command), context)

    def _launch(self, argv: list[str], context: ToolContext) -> tuple[str, bool]:
        self._check(context)
        # Each invocation owns a separate one-shot AppContainer profile, which
        # the merged launcher deletes after the child and its job exit.
        profile = f"{self.run_id[:32]}-{uuid4().hex[:16]}"
        launcher = AppContainerLauncher(self.root, profile)
        handle = launcher.start(argv, cwd=self.root, network=False)
        remaining = max(0.01, min(MAX_GIT_SECONDS, context.deadline - context.clock()))
        try:
            code = handle.wait(timeout=remaining)
            data = bytearray()
            truncated = False
            for chunk in handle.stdout_chunks():
                space = MAX_OUTPUT_BYTES - len(data)
                if len(chunk) > space:
                    truncated = True
                data.extend(chunk[:space])
        except TimeoutError as exc:
            raise ToolInterrupted("Git command timed out") from exc
        if code != 0:
            raise GitToolError("git_failed")
        self._check(context)
        return data.decode("utf-8", errors="replace"), truncated

    def handle(self, call: ToolCall, context: ToolContext) -> ToolResult:
        try:
            self._check(context)
            args = call.arguments
            expected = {
                "git_status": set(), "git_diff": {"path"}, "git_log": {"n"},
                "git_show": {"rev", "path"}, "git_commit": {"message"},
            }.get(call.name)
            if expected is None or set(args) - expected:
                raise GitToolError("invalid_arguments")
            if call.name == "git_status":
                command = ["status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=all"]
            elif call.name == "git_diff":
                path = self._path(_string(args, "path")) if "path" in args else None
                command = ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color",
                           "--ignore-submodules=all", "--", *([path] if path is not None else [])]
            elif call.name == "git_log":
                count = args.get("n", 10)
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    raise GitToolError("invalid_arguments")
                command = ["log", f"--max-count={min(50, count)}", "--format=%h %s"]
            elif call.name == "git_show":
                revision = _string(args, "rev")
                if _REV.fullmatch(revision) is None:
                    raise GitToolError("invalid_rev")
                path = self._path(_string(args, "path"))
                command = ["show", "--no-ext-diff", "--no-textconv", "--no-color",
                           revision, "--", path]
            elif call.name == "git_commit":
                message = _string(args, "message")
                if len(message) > _MAX_MESSAGE or not message.strip():
                    raise GitToolError("invalid_arguments")
                self._run(["add", "--all", "--", "."], context)
                command = ["commit", "--no-gpg-sign", "-m", message]
                if self._git_dir == ".cortex-agent-git":
                    # First-use initialization may already have included edits
                    # in the isolated baseline. Still record the user's commit.
                    command.insert(1, "--allow-empty")
            else:
                raise GitToolError("invalid_arguments")
            output, truncated = self._run(command, context)
            return ToolResult(
                json.dumps({"output": output, "truncated": truncated}, ensure_ascii=False),
                summary="Work-copy Git tool completed",
            )
        except GitToolError as exc:
            code = exc.code
        except WorkCopyError as exc:
            code = exc.code
        except (OSError, ValueError):
            code = "git_unavailable"
        return ToolResult(json.dumps({"error": code}), summary="Work-copy Git tool refused", success=False)


def register_git_tools(
    registry: ToolRegistry, workcopy_root: str | os.PathLike[str], run_id: str,
) -> None:
    """Register only the five allowlisted, network-disabled Git tools."""
    git = _Git(workcopy_root, run_id)
    for spec in GIT_TOOLS:
        registry.register(spec, git.handle)
