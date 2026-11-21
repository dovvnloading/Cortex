"""Disposable, consent-gated source work copies for future coding-agent runs.

A work copy is not a security sandbox for *executing* untrusted code. It is a
bounded, source-only filesystem view. Git worktrees are created without a
checkout: blobs are materialized without invoking checkout filters or hooks.
Only an explicit ``accept`` may apply an agent's edits to the real project;
all accepted bytes are journaled before the first write for confirmed undo.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import difflib
import fnmatch
from hashlib import sha256
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import subprocess
import tempfile
from threading import RLock
from typing import Literal

from cortex_backend.core.paths import AppPathError, AppPaths, secure_private_path
from cortex_backend.services.workspace_checkpoint import (
    WorkspaceCheckpointError,
    WorkspaceCheckpointStore,
)
from cortex_backend.services.workspace_context import (
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_FILES,
    MAX_WORKSPACE_FILE_BYTES,
    WorkspaceAccessError,
    _capture_files,
    _is_reparse,
    _permitted_name,
    _read_regular_source,
    _relative_file_name,
    _safe_root,
)

MAX_DIFF_CHARS = 60_000
MAX_GIT_TREE_BYTES = 2 * 1024 * 1024
_DELETE_FLAG = 1 << 20  # Stored in the journal's mode field; stat.S_IMODE ignores it.
_EMPTY_SHA = sha256(b"").hexdigest()
_PROJECT_WRITE_LOCK = RLock()
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class WorkCopyError(RuntimeError):
    """Stable errors which never disclose a project path or source bytes."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The agent work copy operation could not be completed.")


@dataclass(frozen=True, slots=True)
class WorkCopyChange:
    kind: Literal["add", "modify", "delete"]
    baseline_sha256: str | None
    new_sha256: str | None
    diff: str


@dataclass(frozen=True, slots=True)
class _File:
    contents: bytes
    mode: int


@dataclass(frozen=True, slots=True)
class _IgnoreRule:
    directory: str
    pattern: str
    negated: bool
    directories_only: bool
    anchored: bool


def _digest(contents: bytes) -> str:
    return sha256(contents).hexdigest()


def _local_root(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or PureWindowsPath(raw).anchor.startswith("\\\\"):
        raise WorkCopyError("invalid_root")
    try:
        root = _safe_root(raw).resolve(strict=True)
    except (WorkspaceAccessError, OSError, ValueError, RuntimeError) as exc:
        raise WorkCopyError("invalid_root") from exc
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise WorkCopyError("protected_root")
    for variable in ("WINDIR", "SystemRoot", "USERPROFILE"):
        location = os.environ.get(variable)
        if location:
            try:
                protected = Path(location).resolve(strict=False)
                if root == protected or (variable != "USERPROFILE" and root.is_relative_to(protected)):
                    raise WorkCopyError("protected_root")
            except (OSError, RuntimeError, ValueError) as exc:
                raise WorkCopyError("invalid_root") from exc
    return root


def _valid_path(path: str) -> str:
    try:
        relative = _relative_file_name(path)
    except WorkspaceAccessError as exc:
        raise WorkCopyError("invalid_path") from exc
    parts = relative.split("/")
    if not all(
        (part == ".gitignore" and index == len(parts) - 1)
        or _permitted_name(part, directory=index < len(parts) - 1)
        for index, part in enumerate(parts)
    ):
        raise WorkCopyError("invalid_path")
    return relative


def _check_root(root: Path, identity: tuple[int, int]) -> None:
    try:
        checked = _safe_root(str(root))
        info = checked.lstat()
    except (OSError, WorkspaceAccessError) as exc:
        raise WorkCopyError("stale_root") from exc
    if (info.st_dev, info.st_ino) != identity:
        raise WorkCopyError("stale_root")


def _file_at(root: Path, relative: str) -> _File | None:
    """Read an optional regular file; refuse every linked intermediate component."""
    cursor = root
    parts = relative.split("/")
    for name in parts[:-1]:
        cursor /= name
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WorkCopyError("unsafe_path") from exc
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise WorkCopyError("unsafe_path")
    cursor /= parts[-1]
    try:
        info = cursor.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkCopyError("unsafe_path") from exc
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise WorkCopyError("unsafe_path")
    contents = _read_regular_source(cursor, info)
    if contents is None:
        raise WorkCopyError("unsafe_file")
    return _File(contents, info.st_mode)


def _parent_for(root: Path, relative: str) -> Path:
    """Make only missing, validated directory components beneath a verified root."""
    cursor = root
    for component in relative.split("/")[:-1]:
        cursor /= component
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            cursor.mkdir(mode=0o700)
            info = cursor.lstat()
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise WorkCopyError("unsafe_path")
    return cursor


def _write(root: Path, relative: str, contents: bytes, mode: int | None) -> None:
    parent = _parent_for(root, relative)
    name: str | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".cortex-copy-", dir=parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        # File contents are written in binary mode, without newline translation.
        # Journal deletion flags (or a missing mode) are not filesystem modes.
        saved_mode = 0 if mode is None else mode & ~_DELETE_FLAG
        if saved_mode > 0:
            permissions = stat.S_IMODE(saved_mode)
            if permissions:
                os.chmod(name, permissions)
        os.replace(name, parent / relative.split("/")[-1])
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def _remove(root: Path, relative: str) -> None:
    path = root / relative
    info = path.lstat()
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise WorkCopyError("unsafe_path")
    path.unlink()


def _purge_onexc(path: Path, error: PermissionError) -> None:
    """Retry read-only regular files, without changing permissions on links."""
    try:
        info = path.lstat()
    except OSError:
        raise error from None
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise error
    os.chmod(path, stat.S_IWRITE)
    path.unlink()


def _purge(directory: Path, *, keep_gitfile: bool = False) -> None:
    """Delete only directory entries under our copy, never traverse a link."""
    with os.scandir(directory) as iterator:
        children = list(iterator)
    for child in children:
        if keep_gitfile and child.name == ".git":
            continue
        path = Path(child.path)
        info = path.lstat()
        if _is_reparse(info):
            if stat.S_ISLNK(info.st_mode):
                path.unlink()
            else:
                path.rmdir()  # On Windows this removes the junction itself.
        elif stat.S_ISDIR(info.st_mode):
            _purge(path)
            path.rmdir()
        else:
            try:
                path.unlink()
            except PermissionError as exc:
                _purge_onexc(path, exc)


def _rules(files: Mapping[str, _File]) -> tuple[_IgnoreRule, ...]:
    rules: list[_IgnoreRule] = []
    for path, item in sorted(files.items()):
        if path.rsplit("/", 1)[-1] != ".gitignore":
            continue
        directory = path.rpartition("/")[0]
        for raw in item.contents.decode("utf-8").splitlines():
            rule = raw.rstrip()
            if not rule or rule.startswith("#"):
                continue
            negated = rule.startswith("!")
            pattern = rule[1:] if negated else rule
            if pattern.startswith("\\#") or pattern.startswith("\\!"):
                pattern = pattern[1:]
            if pattern and len(pattern) <= 256:
                rules.append(_IgnoreRule(
                    directory, pattern.lstrip("/"), negated,
                    pattern.endswith("/"), pattern.startswith("/"),
                ))
                if len(rules) > 2048:
                    raise WorkCopyError("ignore_limit")
    return tuple(rules)


def _git_glob(value: str, pattern: str) -> bool:
    """A separator-aware subset of git wildmatch, including double stars."""
    expression = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression.append("(?:.*/)?")
                else:
                    expression.append(".*")
                    continue
            else:
                expression.append("[^/]*")
        elif char == "?":
            expression.append("[^/]")
        else:
            expression.append(re.escape(char))
        index += 1
    return re.fullmatch("".join(expression), value) is not None


def _ignored(relative: str, rules: tuple[_IgnoreRule, ...]) -> bool:
    def matches(path: str, directory: bool) -> bool:
        ignored = False
        for rule in rules:
            if rule.directory:
                if not path.startswith(rule.directory + "/"):
                    continue
                scoped = path[len(rule.directory) + 1:]
            else:
                scoped = path
            pattern = rule.pattern.rstrip("/")
            parts = scoped.split("/")
            candidates = parts if directory else parts[:-1] if rule.directories_only else parts
            if "/" in pattern or rule.anchored:
                hit = any(
                    _git_glob("/".join(parts[:index + 1]), pattern)
                    for index in range(len(candidates))
                )
            else:
                hit = any(fnmatch.fnmatchcase(part, pattern) for part in candidates)
            if hit:
                ignored = not rule.negated
        return ignored

    # Git refuses to re-include a file in a still-ignored parent directory.
    parts = relative.split("/")
    return matches(relative, False) or any(
        matches("/".join(parts[:index]), True)
        for index in range(1, len(parts))
    )


def _snapshot(root: Path, ignores: tuple[_IgnoreRule, ...] = ()) -> dict[str, _File]:
    try:
        captured, total, _scanned, _skipped, truncated = _capture_files(root)
    except WorkspaceAccessError as exc:
        raise WorkCopyError("scan_failed") from exc
    if truncated:
        raise WorkCopyError("copy_limit")
    files: dict[str, _File] = {}
    # The read-only snapshot deliberately omits .gitignore as a source file.
    # Read those rules separately without widening its other exclusion policy.
    directories = {""}
    for relative in captured:
        parts = relative.split("/")[:-1]
        directories.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    for directory in sorted(directories):
        relative = f"{directory}/.gitignore" if directory else ".gitignore"
        item = _file_at(root, relative)
        if item is not None:
            if (len(files) + len(captured) >= MAX_WORKSPACE_FILES
                    or total + len(item.contents) > MAX_WORKSPACE_BYTES):
                raise WorkCopyError("copy_limit")
            total += len(item.contents)
            try:
                item.contents.decode("utf-8")
            except UnicodeError as exc:
                raise WorkCopyError("unsafe_file") from exc
            files[relative] = item
    for relative, source in captured.items():
        if _ignored(relative, ignores):
            continue
        item = _file_at(root, relative)
        if item is None or _digest(item.contents) != source.sha256:
            raise WorkCopyError("source_changed")
        files[relative] = item
    return files


_GIT_CONFIG_LIMIT = 1024 * 1024
_GIT_INCLUDE_SECTION = re.compile(r"^\s*\[\s*include(?:if)?(?=\s|\])", re.IGNORECASE)


def _refuse_external_git_includes(root: Path) -> None:
    """Reject local include/includeIf sections before Git can read their targets."""
    metadata = root / ".git"
    try:
        info = metadata.lstat()
    except FileNotFoundError:
        return
    if _is_reparse(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise WorkCopyError("git_unavailable")
    # Git-linked source worktrees have a .git pointer file rather than local config.
    if stat.S_ISREG(info.st_mode):
        return
    for name in ("config", "config.worktree"):
        config = metadata / name
        try:
            info = config.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > _GIT_CONFIG_LIMIT:
            raise WorkCopyError("git_unavailable")
        try:
            contents = config.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise WorkCopyError("git_unavailable") from None
        if any(_GIT_INCLUDE_SECTION.match(line) for line in contents.splitlines()):
            raise WorkCopyError("git_unavailable")


def _git(root: Path, *arguments: str, max_bytes: int = MAX_GIT_TREE_BYTES) -> bytes:
    # Work-copy creation invokes local-only Git verbs. Do not let repository
    # include directives or inherited GIT_* variables expand their authority.
    _refuse_external_git_includes(root)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(root), "XDG_CONFIG_HOME": str(root),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }
    # Git for Windows needs these OS paths to start. Never inherit arbitrary
    # environment entries such as GIT_CONFIG_COUNT or GIT_EXEC_PATH.
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TMP", "TEMP"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    try:
        result = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}",
             "-c", "core.fsmonitor=false", "-c", "credential.helper=",
             "-c", "core.sshCommand=", *arguments],
            cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkCopyError("git_unavailable") from exc
    if result.returncode != 0 or len(result.stdout) > max_bytes:
        raise WorkCopyError("git_failed")
    return result.stdout


def _head(root: Path) -> str | None:
    try:
        metadata = (root / ".git").lstat()
        if _is_reparse(metadata):
            return None
    except FileNotFoundError:
        return None
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve(strict=True)
        if top != root:
            return None
        return _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    except (WorkCopyError, UnicodeError):
        return None


def _from_head(root: Path, copy_root: Path, ignores: tuple[_IgnoreRule, ...]) -> None:
    entries = _git(root, "ls-tree", "-rlz", "--full-tree", "HEAD").split(b"\0")
    count = 0
    total = 0
    for entry in entries:
        if not entry:
            continue
        try:
            metadata, name = entry.split(b"\t", 1)
            mode, kind, blob, length = metadata.decode("ascii").split(" ")
            relative = name.decode("utf-8")
            _valid_path(relative)
        except (ValueError, UnicodeError, WorkCopyError):
            continue
        if kind != "blob" or mode not in {"100644", "100755"} or _ignored(relative, ignores):
            continue
        size = int(length) if length != "-" else MAX_WORKSPACE_FILE_BYTES + 1
        if size > MAX_WORKSPACE_FILE_BYTES:
            continue
        count += 1
        total += size
        if count > MAX_WORKSPACE_FILES or total > MAX_WORKSPACE_BYTES:
            raise WorkCopyError("copy_limit")
        data = _git(root, "cat-file", "blob", blob, max_bytes=MAX_WORKSPACE_FILE_BYTES)
        if len(data) != size or b"\0" in data:
            continue
        try:
            data.decode("utf-8")
        except UnicodeError:
            continue
        _write(copy_root, relative, data, 0o755 if mode == "100755" else 0o644)


class WorkCopy:
    """A single run's disposable tree and independently hashed starting point."""

    def __init__(
        self, *, root: Path, path: Path, data_dir: Path, run_id: str,
        root_identity: tuple[int, int], copy_identity: tuple[int, int],
        baseline: dict[str, _File], ignores: tuple[_IgnoreRule, ...],
        git_worktree: bool, checkpoint_store: WorkspaceCheckpointStore | None,
        owner: str,
    ) -> None:
        self.project_root = root
        self.path = path
        self.data_dir = data_dir
        self.run_id = run_id
        self.checkpoint_id = f"agent-workcopy-{run_id}"
        self._root_identity = root_identity
        self._copy_identity = copy_identity
        self._baseline = baseline
        self._ignores = ignores
        self._git_worktree = git_worktree
        self._store = checkpoint_store
        self._owner = owner
        self._lock = RLock()
        self._discarded = False

    @classmethod
    def create(
        cls, project_root: str | os.PathLike[str], run_id: str, confirmed: bool = True,
        *, data_dir: str | os.PathLike[str] | None = None,
        checkpoint_store: WorkspaceCheckpointStore | None = None,
        owner: str = "agent-workcopy",
    ) -> WorkCopy:
        if confirmed is not True:
            raise WorkCopyError("consent_required")
        if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
            raise WorkCopyError("invalid_run_id")
        root = _local_root(project_root)
        try:
            paths = AppPaths.from_data_dir(data_dir) if data_dir is not None else AppPaths.for_current_user()
        except (AppPathError, OSError, ValueError) as exc:
            raise WorkCopyError("invalid_data_dir") from exc
        private = paths.data_dir
        if root == private or root.is_relative_to(private) or private.is_relative_to(root):
            raise WorkCopyError("protected_root")
        # _head() treats Git errors as a missing HEAD. Refuse includes before
        # Windows private-path setup can start icacls.exe or any work-copy Git.
        _refuse_external_git_includes(root)
        root_info = root.lstat()
        root_identity = (root_info.st_dev, root_info.st_ino)
        source = _snapshot(root)
        _check_root(root, root_identity)
        ignores = _rules(source)
        source = {p: item for p, item in source.items() if not _ignored(p, ignores)}
        head = _head(root)
        _check_root(root, root_identity)
        try:
            paths.ensure_data_dir()
            container = private / "agent_workcopies"
            container.mkdir(mode=0o700, exist_ok=True)
            if _is_reparse(container.lstat()):
                raise WorkCopyError("unsafe_path")
            secure_private_path(container, directory=True)
            destination = container / run_id
            if destination.exists() or destination.is_symlink():
                raise WorkCopyError("run_exists")
            git = head is not None
            if head is None:
                if os.path.lexists(root / ".git"):
                    raise WorkCopyError("git_unavailable")
                destination.mkdir(mode=0o700)
            else:
                _git(root, "worktree", "add", "--detach", "--no-checkout", str(destination), head)
            try:
                secure_private_path(destination, directory=True)
                if git:
                    _from_head(root, destination, ignores)
                    # Mirror tracked deletions as well as modified and untracked files.
                    current = _snapshot(destination)
                    for relative in current.keys() - source.keys():
                        _remove(destination, relative)
                for relative, item in source.items():
                    _write(destination, relative, item.contents, item.mode)
                baseline = _snapshot(destination, ignores)
                _check_root(root, root_identity)
                return cls(
                    root=root, path=destination, data_dir=private, run_id=run_id,
                    root_identity=root_identity,
                    copy_identity=(destination.lstat().st_dev, destination.lstat().st_ino),
                    baseline=baseline, ignores=ignores, git_worktree=git,
                    checkpoint_store=checkpoint_store, owner=owner,
                )
            except BaseException:
                _purge(destination, keep_gitfile=git)
                if git:
                    _git(root, "worktree", "remove", "--force", str(destination))
                else:
                    destination.rmdir()
                raise
        except (OSError, AppPathError, WorkspaceCheckpointError) as exc:
            raise WorkCopyError("copy_failed") from exc

    def _verified(self) -> None:
        if self._discarded:
            raise WorkCopyError("discarded")
        _check_root(self.project_root, self._root_identity)
        _check_root(self.path, self._copy_identity)

    def _changes(self) -> tuple[dict[str, WorkCopyChange], dict[str, _File]]:
        self._verified()
        current = _snapshot(self.path, self._ignores)
        changes: dict[str, WorkCopyChange] = {}
        for relative in sorted(self._baseline.keys() | current.keys()):
            initial = self._baseline.get(relative)
            latest = current.get(relative)
            if initial is not None and latest is not None and initial.contents == latest.contents:
                continue
            if initial is not None and latest is None:
                # Missing is a supported deletion; a linked replacement isn't.
                if _file_at(self.path, relative) is not None:
                    raise WorkCopyError("unsafe_path")
            before = initial.contents if initial else b""
            after = latest.contents if latest else b""
            diff = difflib.unified_diff(
                before.decode("utf-8").splitlines(keepends=True),
                after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{relative}" if initial else "/dev/null",
                tofile=f"b/{relative}" if latest else "/dev/null",
            )
            rendered = "".join(
                line if line.endswith(("\n", "\r"))
                else line + "\n\\ No newline at end of file\n"
                for line in diff
            )
            if len(rendered) > MAX_DIFF_CHARS:
                raise WorkCopyError("diff_too_large")
            changes[relative] = WorkCopyChange(
                kind="add" if initial is None else "delete" if latest is None else "modify",
                baseline_sha256=_digest(before) if initial else None,
                new_sha256=_digest(after) if latest else None,
                diff=rendered,
            )
        return changes, current

    def changeset(self) -> dict[str, WorkCopyChange]:
        with self._lock:
            changes, _current = self._changes()
            return changes

    def list_paths(self) -> tuple[str, ...]:
        """List only eligible source files inside the verified disposable copy."""
        with self._lock:
            self._verified()
            return tuple(sorted(_snapshot(self.path, self._ignores)))

    def read_text(self, path: str) -> tuple[str, str]:
        """Read an eligible copy file and its content digest, never the checkout."""
        with self._lock:
            self._verified()
            relative = _valid_path(path)
            if relative not in _snapshot(self.path, self._ignores):
                raise WorkCopyError("invalid_path")
            item = _file_at(self.path, relative)
            if item is None:
                raise WorkCopyError("invalid_path")
            return item.contents.decode("utf-8"), _digest(item.contents)

    def replace_text(
        self, path: str, expected_sha256: str, old_text: str, new_text: str,
    ) -> str:
        """Stage exactly one bounded edit in the copy; acceptance is separate."""
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or not isinstance(old_text, str) or not 0 < len(old_text) <= 8000
            or not isinstance(new_text, str) or len(new_text) > 8000
        ):
            raise WorkCopyError("invalid_patch")
        with self._lock:
            self._verified()
            relative = _valid_path(path)
            if relative not in _snapshot(self.path, self._ignores):
                raise WorkCopyError("invalid_path")
            current = _file_at(self.path, relative)
            if current is None:
                raise WorkCopyError("invalid_path")
            if _digest(current.contents) != expected_sha256:
                raise WorkCopyError("stale_source")
            source = current.contents.decode("utf-8")
            if source.count(old_text) != 1 or new_text == old_text:
                raise WorkCopyError("invalid_patch")
            updated = source.replace(old_text, new_text, 1).encode("utf-8")
            if len(updated) > MAX_WORKSPACE_FILE_BYTES:
                raise WorkCopyError("copy_limit")
            _write(self.path, relative, updated, current.mode)
            return _digest(updated)

    def _checkpoint_store(self) -> WorkspaceCheckpointStore:
        if self._store is None:
            self._store = WorkspaceCheckpointStore(
                self.data_dir / "workspace_checkpoints.sqlite"
            )
        return self._store

    def accept(self, paths: Iterable[str], confirmed: bool) -> tuple[str, ...]:
        """Reject the *entire* batch if any selected source is stale or linked."""
        if confirmed is not True:
            raise WorkCopyError("consent_required")
        with _PROJECT_WRITE_LOCK, self._lock:
            changes, current = self._changes()
            if isinstance(paths, str):
                raise WorkCopyError("invalid_path")
            selected = tuple(sorted({_valid_path(path) for path in paths}))
            if not selected or len(selected) > 32 or not all(p in changes for p in selected):
                raise WorkCopyError("invalid_selection")
            baselines: dict[str, _File | None] = {}
            proposed: dict[str, tuple[bytes, bytes, int]] = {}
            # Preflight ALL selected paths before writing anything to the journal or project.
            for relative in selected:
                actual = _file_at(self.project_root, relative)
                baseline = self._baseline.get(relative)
                if (actual is None) != (baseline is None) or (
                    actual is not None and baseline is not None
                    and _digest(actual.contents) != _digest(baseline.contents)
                ):
                    raise WorkCopyError("stale_source")
                baselines[relative] = actual
                new = current.get(relative)
                proposed[relative] = (
                    actual.contents if actual is not None else b"",
                    new.contents if new is not None else b"",
                    (actual.mode | (_DELETE_FLAG if new is None else 0)) if actual is not None else 0,
                )
            store = self._checkpoint_store()
            try:
                store.prepare(
                    owner=self._owner, checkpoint_id=self.checkpoint_id,
                    root=self.project_root, root_identity=self._root_identity,
                    candidates=proposed,
                )
            except WorkspaceCheckpointError as exc:
                raise WorkCopyError(exc.code) from exc
            written: list[str] = []
            try:
                for relative in selected:
                    # Re-check after journal persistence, directly before each write.
                    actual = _file_at(self.project_root, relative)
                    before = baselines[relative]
                    if (actual is None) != (before is None) or (
                        actual is not None and before is not None
                        and _digest(actual.contents) != _digest(before.contents)
                    ):
                        raise WorkCopyError("stale_source")
                    new = current.get(relative)
                    if new is None:
                        _remove(self.project_root, relative)
                    else:
                        _write(self.project_root, relative, new.contents, new.mode)
                    written.append(relative)
            except (OSError, WorkCopyError) as exc:
                # Restore the previous batch contents when it is still safe.
                remaining = list(written)
                for relative in reversed(written):
                    try:
                        new = current.get(relative)
                        actual = _file_at(self.project_root, relative)
                        if (new is None and actual is not None) or (
                            new is not None and (
                                actual is None or actual.contents != new.contents
                            )
                        ):
                            continue
                        old = baselines[relative]
                        if old is None:
                            _remove(self.project_root, relative)
                        else:
                            _write(self.project_root, relative, old.contents, old.mode)
                        remaining.remove(relative)
                    except (OSError, WorkCopyError):
                        continue
                if remaining:
                    raise WorkCopyError("partial_rollback") from exc
                try:
                    store.abort_pending(owner=self._owner, checkpoint_id=self.checkpoint_id)
                except WorkspaceCheckpointError as storage_exc:
                    raise WorkCopyError("checkpoint_pending") from storage_exc
                raise WorkCopyError("write_failed") from exc
            try:
                store.commit_pending(owner=self._owner, checkpoint_id=self.checkpoint_id)
            except WorkspaceCheckpointError as exc:
                raise WorkCopyError("checkpoint_pending") from exc
            for relative in selected:
                latest = current.get(relative)
                if latest is None:
                    self._baseline.pop(relative, None)
                else:
                    self._baseline[relative] = latest
            return selected

    def undo(self, confirmed: bool) -> tuple[str, ...]:
        """Explicitly restore the journaled first originals, including absence."""
        if confirmed is not True:
            raise WorkCopyError("consent_required")
        with _PROJECT_WRITE_LOCK, self._lock:
            # Accepted edits remain recoverable after the disposable copy is gone.
            _check_root(self.project_root, self._root_identity)
            store = self._checkpoint_store()
            record = store.get(owner=self._owner, checkpoint_id=self.checkpoint_id)
            if record.root != str(self.project_root) or record.root_identity != self._root_identity:
                raise WorkCopyError("recovery_required")
            verified: dict[str, _File | None] = {}
            for entry in record.files:
                relative = _valid_path(entry.path)
                actual = _file_at(self.project_root, relative)
                acceptable = {entry.original_sha, entry.expected_sha}
                if entry.pending_sha is not None:
                    acceptable.add(entry.pending_sha)
                if actual is None:
                    if not ((entry.mode & ~_DELETE_FLAG) == 0 or (
                        entry.mode & _DELETE_FLAG and _EMPTY_SHA in acceptable
                    )):
                        raise WorkCopyError("stale_source")
                elif _digest(actual.contents) not in acceptable or (
                    entry.mode & _DELETE_FLAG and actual.contents == b""
                    and entry.original_sha != _EMPTY_SHA
                ):
                    # A deleted file recreated as empty is not the approved deletion.
                    raise WorkCopyError("stale_source")
                verified[relative] = actual
            for entry in record.files:
                relative = entry.path
                try:
                    now = _file_at(self.project_root, relative)
                    if now != verified[relative]:
                        raise WorkCopyError("stale_source")
                    original_mode = entry.mode & ~_DELETE_FLAG
                    if original_mode == 0:
                        if now is not None:
                            _remove(self.project_root, relative)
                    elif now is None or now.contents != entry.original:
                        _write(self.project_root, relative, entry.original, original_mode)
                except (OSError, WorkCopyError) as exc:
                    # Keep the durable checkpoint for retry after interruption.
                    raise WorkCopyError("restore_failed") from exc
            store.delete(owner=self._owner, checkpoint_id=self.checkpoint_id)
            if not self._discarded:
                self._baseline = _snapshot(self.project_root, self._ignores)
            return tuple(sorted(verified))

    def discard(self) -> None:
        """Remove only this copy; never traverse links or other run directories."""
        with self._lock:
            self._verified()
            try:
                _purge(self.path, keep_gitfile=self._git_worktree)
                if self._git_worktree:
                    _git(self.project_root, "worktree", "remove", "--force", str(self.path))
                else:
                    self.path.rmdir()
            except (OSError, WorkCopyError) as exc:
                raise WorkCopyError("discard_failed") from exc
            self._discarded = True


def create(
    project_root: str | os.PathLike[str], run_id: str, confirmed: bool = True,
    *, data_dir: str | os.PathLike[str] | None = None,
    checkpoint_store: WorkspaceCheckpointStore | None = None,
    owner: str = "agent-workcopy",
) -> WorkCopy:
    """Convenience factory for hosts that do not instantiate ``WorkCopy``."""
    return WorkCopy.create(
        project_root, run_id, confirmed, data_dir=data_dir,
        checkpoint_store=checkpoint_store, owner=owner,
    )


def restore(
    project_root: str | os.PathLike[str], run_id: str, confirmed: bool,
    *, data_dir: str | os.PathLike[str] | None = None,
    owner: str = "agent-workcopy",
) -> tuple[str, ...]:
    """Restore a durable accept even after a restart or discarded copy."""
    if confirmed is not True:
        raise WorkCopyError("consent_required")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise WorkCopyError("invalid_run_id")
    root = _local_root(project_root)
    try:
        paths = AppPaths.from_data_dir(data_dir) if data_dir is not None else AppPaths.for_current_user()
    except (AppPathError, OSError, ValueError) as exc:
        raise WorkCopyError("invalid_data_dir") from exc
    private = paths.data_dir
    if root == private or root.is_relative_to(private) or private.is_relative_to(root):
        raise WorkCopyError("protected_root")
    info = root.lstat()
    work = WorkCopy(
        root=root, path=private / "agent_workcopies" / run_id, data_dir=private,
        run_id=run_id, root_identity=(info.st_dev, info.st_ino),
        copy_identity=(0, 0), baseline={}, ignores=(), git_worktree=False,
        checkpoint_store=WorkspaceCheckpointStore(paths.workspace_checkpoints_database),
        owner=owner,
    )
    work._discarded = True
    return work.undo(confirmed=True)
