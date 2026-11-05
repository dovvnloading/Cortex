"""Explicitly consented, immutable, read-only repository context.

This is deliberately NOT a general filesystem tool. A trusted UI/host must
name an absolute local directory and confirm read access before a snapshot is
captured. The model receives only an opaque snapshot id and may search/read
the *in-memory copy*; it cannot name an arbitrary host path, write files,
start a process, or retain access after revoke, expiry, or app shutdown.

The snapshot uses small bounded source files only. Hidden/credential files,
dependencies, build outputs, binary files, symlinks and Windows reparse
points are excluded. Native path races during capture on a concurrently
hostile filesystem are not a security sandbox; the contents are never read
from the original tree again after capture.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import secrets
import stat
from threading import RLock
import unicodedata
import time
from types import MappingProxyType

MAX_WORKSPACE_FILES = 2048
MAX_SCANNED_ENTRIES = 12000
MAX_WORKSPACE_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 256 * 1024
MAX_SEARCH_RESULTS = 24
MAX_READ_LINES = 120
MAX_READ_CHARS = 12000
WORKSPACE_TTL_SECONDS = 15 * 60
MAX_SNAPSHOTS_PER_OWNER = 2
MAX_TOTAL_SNAPSHOTS = 8

_EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", ".next", ".nuxt", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode", "coverage",
})
_EXCLUDED_FILENAMES = frozenset({
    ".env", ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
    "credentials", "credentials.json", "secrets.json", "secrets.yaml",
    "secrets.yml", "private.key", "private.pem", "known_hosts",
})
_EXCLUDED_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".cer",
    ".sqlite", ".db", ".lock", ".map", ".min.js",
})
_SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json",
    ".jsonl", ".md", ".mdx", ".rst", ".txt", ".toml", ".yaml", ".yml",
    ".xml", ".html", ".css", ".scss", ".sql", ".sh", ".ps1", ".bat",
    ".cmd", ".go", ".rs", ".java", ".kt", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".dart", ".proto", ".vue",
    ".svelte", ".gradle", ".dockerfile", ".gitignore",
})
_WINDOWS_REPARSE_POINT = 0x0400


class WorkspaceAccessError(RuntimeError):
    """An intentionally path-free stable error category."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("Workspace context is unavailable.")


@dataclass(frozen=True, slots=True)
class WorkspaceFile:
    path: str
    # Exact UTF-8 content including CRLF, final newline and optional BOM.
    # Patch preview must not silently rewrite line endings or otherwise
    # change bytes outside the replacement the model proposed.
    text: str
    lines: tuple[str, ...]
    sha256: str

    @property
    def line_count(self) -> int:
        return len(self.lines)


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    snapshot_id: str
    owner: str
    files: Mapping[str, WorkspaceFile]
    total_bytes: int
    scanned_entries: int
    skipped_entries: int
    truncated: bool
    expires_at: datetime
    expiry_deadline: float

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass(frozen=True, slots=True)
class WorkspaceSearchHit:
    path: str
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class WorkspaceExcerpt:
    path: str
    start_line: int
    end_line: int
    total_lines: int
    sha256: str
    text: str
    truncated: bool


def _is_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _safe_root(value: str) -> Path:
    """A pre-existing absolute local directory, with no linked ancestors."""
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 4096:
        raise WorkspaceAccessError("invalid_root")
    if PureWindowsPath(value).anchor.startswith("\\\\"):
        raise WorkspaceAccessError("network_root")
    root = Path(value)
    if not root.is_absolute():
        raise WorkspaceAccessError("invalid_root")
    for candidate in (root, *root.parents):
        try:
            info = candidate.lstat()
        except (OSError, ValueError) as exc:
            raise WorkspaceAccessError("invalid_root") from exc
        if _is_reparse(info):
            raise WorkspaceAccessError("linked_root")
    if not stat.S_ISDIR(root.stat().st_mode):
        raise WorkspaceAccessError("invalid_root")
    return root


def _permitted_name(name: str, *, directory: bool) -> bool:
    # A filename is later emitted as an exact identifier, a model citation or
    # a unified-diff header. Reject invisible/bidi controls and separators
    # rather than letting one file forge an additional path or diff hunk.
    if (
        not name or len(name) > 120 or name != name.strip()
        or any(
            char in ":\\\\/" or unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
            for char in name
        )
    ):
        return False
    lowered = name.casefold()
    if name.startswith(".") and name not in {".github", ".githooks", ".gitignore"}:
        return False
    if directory:
        return lowered not in _EXCLUDED_DIRECTORIES
    if lowered in _EXCLUDED_FILENAMES or lowered.startswith(".env.") or lowered.endswith(".min.js"):
        return False
    if Path(lowered).suffix in _EXCLUDED_SUFFIXES:
        return False
    return Path(lowered).suffix in _SOURCE_SUFFIXES or lowered in {
        "dockerfile", "makefile", "justfile", "license", "agents.md",
    }


def _read_regular_source(path: Path, info: os.stat_result) -> bytes | None:
    """Read a verified regular file once; never follow its final symlink."""
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_WORKSPACE_FILE_BYTES:
        return None
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > MAX_WORKSPACE_FILE_BYTES
            or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
            or _is_reparse(before)
        ):
            return None
        # Read the already-verified descriptor directly. On Windows the
        # stdio wrapper around an existing CRT descriptor can have different
        # lifetime/close behavior from the underlying file handle; there is
        # no reason to involve a second buffering layer for a 256 KiB file.
        chunks: list[bytes] = []
        received = 0
        while received <= MAX_WORKSPACE_FILE_BYTES:
            block = os.read(
                descriptor,
                min(65536, MAX_WORKSPACE_FILE_BYTES + 1 - received),
            )
            if not block:
                break
            chunks.append(block)
            received += len(block)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(data) > MAX_WORKSPACE_FILE_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            or len(data) != before.st_size
            or b"\x00" in data
        ):
            return None
        return data
    except (OSError, ValueError):
        return None
    finally:
        os.close(descriptor)


def _capture_files(root: Path) -> tuple[Mapping[str, WorkspaceFile], int, int, int, bool]:
    files: dict[str, WorkspaceFile] = {}
    scanned = 0
    skipped = 0
    total_bytes = 0
    truncated = False
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                # Bounded even for a single enormous directory. Do not retain
                # arbitrary numbers of file names in the indexing process.
                items = []
                for entry in entries:
                    items.append(entry)
                    if len(items) + scanned > MAX_SCANNED_ENTRIES:
                        truncated = True
                        break
        except OSError as exc:
            raise WorkspaceAccessError("scan_failed") from exc
        if truncated:
            break
        # Ordered, repeatable selection; a large repository is never indexed
        # based on whichever order the filesystem happened to return entries.
        for entry in sorted(items, key=lambda item: (item.name.casefold(), item.name)):
            scanned += 1
            try:
                # Windows/Python 3.11 DirEntry.stat() reports zero for st_dev
                # and st_ino, while fstat() on the opened handle has real file
                # identity. Use a fresh lstat() so the identity comparison
                # below has matching data and ordinary files are not skipped.
                info = Path(entry.path).lstat()
            except OSError:
                skipped += 1
                continue
            if _is_reparse(info):
                skipped += 1
                continue
            if stat.S_ISDIR(info.st_mode):
                if _permitted_name(entry.name, directory=True):
                    pending.append(Path(entry.path))
                else:
                    skipped += 1
                continue
            if not stat.S_ISREG(info.st_mode) or not _permitted_name(entry.name, directory=False):
                skipped += 1
                continue
            if len(files) >= MAX_WORKSPACE_FILES:
                truncated = True
                break
            data = _read_regular_source(Path(entry.path), info)
            if data is None:
                skipped += 1
                continue
            if total_bytes + len(data) > MAX_WORKSPACE_BYTES:
                truncated = True
                break
            try:
                content = data.decode("utf-8")
            except UnicodeError:
                skipped += 1
                continue
            relative = Path(entry.path).relative_to(root).as_posix()
            files[relative] = WorkspaceFile(
                path=relative,
                text=content,
                lines=tuple(content.splitlines()),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            total_bytes += len(data)
        if truncated:
            break
    return MappingProxyType(dict(sorted(files.items()))), total_bytes, scanned, skipped, truncated


def _relative_file_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise WorkspaceAccessError("invalid_file")
    if "\\" in value or "\x00" in value or ":" in value:
        raise WorkspaceAccessError("invalid_file")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise WorkspaceAccessError("invalid_file")
    return path.as_posix()


class WorkspaceSnapshotRegistry:
    """Process-local, owner-scoped snapshot leases; no durable grant record."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._snapshots: dict[str, WorkspaceSnapshot] = {}

    def capture(
        self, *, owner: str, root_path: str, confirmed: bool = False
    ) -> WorkspaceSnapshot:
        if confirmed is not True:
            raise WorkspaceAccessError("consent_required")
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise WorkspaceAccessError("invalid_owner")
        root = _safe_root(root_path)
        files, total, scanned, skipped, truncated = _capture_files(root)
        now = self._clock()
        with self._lock:
            self._expire(now)
            if sum(item.owner == owner for item in self._snapshots.values()) >= MAX_SNAPSHOTS_PER_OWNER:
                raise WorkspaceAccessError("owner_limit")
            if len(self._snapshots) >= MAX_TOTAL_SNAPSHOTS:
                raise WorkspaceAccessError("registry_limit")
            key = secrets.token_urlsafe(32)
            snapshot = WorkspaceSnapshot(
                snapshot_id=key,
                owner=owner,
                files=files,
                total_bytes=total,
                scanned_entries=scanned,
                skipped_entries=skipped,
                truncated=truncated,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=WORKSPACE_TTL_SECONDS),
                expiry_deadline=now + WORKSPACE_TTL_SECONDS,
            )
            self._snapshots[key] = snapshot
            return snapshot

    def _expire(self, now: float) -> None:
        for key, snapshot in list(self._snapshots.items()):
            if snapshot.expiry_deadline <= now:
                del self._snapshots[key]

    def get(self, *, owner: str, snapshot_id: str) -> WorkspaceSnapshot:
        with self._lock:
            self._expire(self._clock())
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot is None or snapshot.owner != owner:
                # Unknown, foreign, revoked and expired ids are indistinguishable.
                raise WorkspaceAccessError("unavailable")
            return snapshot

    def revoke(self, *, owner: str, snapshot_id: str) -> None:
        with self._lock:
            self.get(owner=owner, snapshot_id=snapshot_id)
            self._snapshots.pop(snapshot_id, None)

    def close(self) -> None:
        with self._lock:
            self._snapshots.clear()

    def list_paths(self, *, owner: str, snapshot_id: str, limit: int = 200) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise WorkspaceAccessError("invalid_limit")
        snapshot = self.get(owner=owner, snapshot_id=snapshot_id)
        return tuple(sorted(snapshot.files))[:limit]

    def search(
        self, *, owner: str, snapshot_id: str, query: str, limit: int = 12
    ) -> tuple[WorkspaceSearchHit, ...]:
        if (
            not isinstance(query, str) or not 2 <= len(query.strip()) <= 120
            or any(ord(char) < 32 for char in query)
        ):
            raise WorkspaceAccessError("invalid_query")
        if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise WorkspaceAccessError("invalid_limit")
        snapshot = self.get(owner=owner, snapshot_id=snapshot_id)
        needle = query.casefold().strip()
        hits: list[WorkspaceSearchHit] = []
        for path, file in snapshot.files.items():
            for index, line in enumerate(file.lines, start=1):
                if needle in line.casefold():
                    snippet = line.strip()
                    if len(snippet) > 280:
                        occurrence = snippet.casefold().find(needle)
                        start = max(0, occurrence - 80)
                        snippet = snippet[start:start + 280]
                    hits.append(WorkspaceSearchHit(path=path, line=index, text=snippet))
                    if len(hits) >= limit:
                        return tuple(hits)
        return tuple(hits)

    def read(
        self,
        *,
        owner: str,
        snapshot_id: str,
        path: str,
        start_line: int = 1,
        max_lines: int = 80,
    ) -> WorkspaceExcerpt:
        if type(start_line) is not int or start_line < 1:
            raise WorkspaceAccessError("invalid_range")
        if type(max_lines) is not int or not 1 <= max_lines <= MAX_READ_LINES:
            raise WorkspaceAccessError("invalid_range")
        key = _relative_file_name(path)
        snapshot = self.get(owner=owner, snapshot_id=snapshot_id)
        file = snapshot.files.get(key)
        if file is None:
            raise WorkspaceAccessError("file_unavailable")
        if start_line > max(1, file.line_count):
            raise WorkspaceAccessError("invalid_range")
        selected = file.lines[start_line - 1 : start_line - 1 + max_lines]
        rendered: list[str] = []
        total_chars = 0
        for index, line in enumerate(selected, start=start_line):
            value = f"{index}: {line}"
            if total_chars + len(value) + 1 > MAX_READ_CHARS:
                break
            rendered.append(value)
            total_chars += len(value) + 1
        truncated = len(rendered) < len(selected) or start_line - 1 + len(selected) < file.line_count
        return WorkspaceExcerpt(
            path=file.path,
            start_line=start_line,
            end_line=start_line + len(rendered) - 1,
            total_lines=file.line_count,
            sha256=file.sha256,
            text="\n".join(rendered),
            truncated=truncated,
        )
