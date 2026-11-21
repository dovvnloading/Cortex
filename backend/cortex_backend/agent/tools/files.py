"""Bounded, work-copy-only file tools for explicitly opted-in agent runs.

This is not a filesystem sandbox against a concurrently hostile local process.
It uses the same validated work-copy paths and root identity checks as WorkCopy,
and never intentionally traverses a link or a Windows reparse point.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from functools import cache
from hashlib import sha256
import fnmatch
import json
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import re
import stat
import tempfile
from time import monotonic
from typing import Any, Final

from cortex_backend.services.workspace_context import (
    WorkspaceAccessError,
    _is_reparse,
    _permitted_name,
    _relative_file_name,
)

from ..protocol import ToolCall, ToolSpec
from ..tool_registry import NeedsApproval, ToolContext, ToolInterrupted, ToolRegistry, ToolResult
from ..workcopy import WorkCopyError, _check_root, _local_root, _valid_path

MAX_FILE_BYTES: Final = 1024 * 1024
MAX_READ_LINES: Final = 400
MAX_GREP_HITS: Final = 200
MAX_RESULTS: Final = 200
MAX_SCANNED_ENTRIES: Final = 12_000
GREP_FILE_TIMEOUT_SECONDS: Final = 2.0
_MAX_REGEX_LENGTH: Final = 256
_GREP_LINE_CHARS: Final = 240

# Refuse Windows device names and probable 8.3 aliases on every platform.
# Resolve existing components too, so an alias cannot disguise a protected path.
_SHORT_NAME = re.compile(r"[^./\\]{1,6}~[0-9]+(?:\.[^./\\]{0,3})?\Z", re.IGNORECASE)
_DEVICE_NAME = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.IGNORECASE)


class FileToolError(RuntimeError):
    """A stable error category, without a path or file content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The work-copy file operation was refused.")


def _schema(required: dict[str, Any], optional: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**required, **(optional if optional is not None else {})},
        "required": list(required),
        "additionalProperties": False,
    }


_PATH: Final = {"type": "string", "minLength": 1, "maxLength": 1024}
_TEXT: Final = {"type": "string"}
_POSITIVE: Final = {"type": "integer", "minimum": 1}
_DIRECTORY: Final = {"type": "string", "maxLength": 1024}
_PATTERN: Final = {"type": "string", "minLength": 1, "maxLength": 1024}

FILE_TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec("list_dir", "List permitted entries in a work-copy directory (default: root).",
             _schema({}, {"path": _DIRECTORY})),
    ToolSpec("glob", "Find permitted work-copy files by a relative glob, with ** for directories.",
             _schema({"pattern": _PATTERN}, {"path": _DIRECTORY})),
    ToolSpec("grep", "Search UTF-8 work-copy files with Python re; max 200 hits and a timeout per file.",
             _schema({"pattern": {"type": "string", "minLength": 1, "maxLength": _MAX_REGEX_LENGTH}},
                     {"path": _DIRECTORY, "glob": _PATTERN})),
    ToolSpec("read_file", "Read at most 400 numbered lines and the full-file SHA-256.",
             _schema({"path": _PATH}, {"start_line": _POSITIVE, "end_line": _POSITIVE})),
    ToolSpec("write_file", "Create or replace one UTF-8 work-copy file, at most 1 MiB.",
             _schema({"path": _PATH, "content": _TEXT})),
    ToolSpec("edit_file", "Replace exact text in a UTF-8 file, once unless replace_all is true.",
             _schema({"path": _PATH, "old": _TEXT, "new": _TEXT},
                     {"replace_all": {"type": "boolean"}})),
    ToolSpec("delete_file", "Delete one regular work-copy file.",
             _schema({"path": _PATH})),
    ToolSpec("move_file", "Move a work-copy file to an unoccupied destination.",
             _schema({"source": _PATH, "destination": _PATH})),
)


def _string(args: Mapping[str, Any], key: str, *, default: str | None = None) -> str:
    value = args.get(key, default)
    if not isinstance(value, str):
        raise FileToolError("invalid_arguments")
    return value


def _integer(args: Mapping[str, Any], key: str, *, default: int) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FileToolError("invalid_arguments")
    return value


def _checked_parts(relative: str) -> None:
    for name in relative.split("/"):
        if name.endswith(".") or _SHORT_NAME.fullmatch(name) or _DEVICE_NAME.fullmatch(name):
            raise FileToolError("invalid_path")


def _file_name(value: str) -> str:
    try:
        relative = _valid_path(value)
    except WorkCopyError:
        raise FileToolError("invalid_path") from None
    _checked_parts(relative)
    return relative


def _directory_name(value: str) -> str:
    if value in {"", "."}:
        return ""
    # Use WorkCopy's validator on every directory component, not an alternate
    # filename policy. The appended name is only a validator sentinel.
    validated = _file_name(value + "/__cortex_probe__.py")
    return validated.rsplit("/", 1)[0]


def _glob_pattern(value: str) -> str:
    try:
        relative = _relative_file_name(value)
    except WorkspaceAccessError:
        raise FileToolError("invalid_path") from None
    _checked_parts(relative)
    if not all(_permitted_name(part, directory=True) for part in relative.split("/")):
        raise FileToolError("invalid_path")
    return relative


def _matches_glob(path: str, pattern: str) -> bool:
    parts = path.split("/")
    patterns = pattern.split("/")

    @cache
    def match(pattern_index: int, path_index: int) -> bool:
        if pattern_index == len(patterns):
            return path_index == len(parts)
        segment = patterns[pattern_index]
        if segment == "**":
            return match(pattern_index + 1, path_index) or (
                path_index < len(parts) and match(pattern_index, path_index + 1)
            )
        return (
            path_index < len(parts)
            and fnmatch.fnmatchcase(parts[path_index], segment)
            and match(pattern_index + 1, path_index + 1)
        )

    return match(0, 0)


def _children(folder: Path) -> list[os.DirEntry[str]]:
    # Do not materialize an unbounded directory before checking scan limits.
    entries: list[os.DirEntry[str]] = []
    with os.scandir(folder) as iterator:
        for item in iterator:
            if len(entries) >= MAX_SCANNED_ENTRIES:
                raise FileToolError("scan_limit")
            entries.append(item)
    return sorted(entries, key=lambda item: item.name)


def _grep_worker(text: str, expression: str, limit: int, connection: Connection) -> None:
    """Only this disposable process runs potentially pathological re.search."""
    try:
        compiled = re.compile(expression)
        hits: list[tuple[int, str]] = []
        truncated = False
        for number, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line) is None:
                continue
            if len(hits) >= limit:
                truncated = True
                break
            hits.append((number, line[:_GREP_LINE_CHARS]))
        connection.send((hits, truncated))
    finally:
        connection.close()


class _Files:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = _local_root(root)
        info = self.root.lstat()
        self.identity = (info.st_dev, info.st_ino)

    def _check(self, context: ToolContext) -> None:
        if context.stopped():
            raise ToolInterrupted("file operation interrupted")
        _check_root(self.root, self.identity)

    def _canonical(self, path: Path) -> None:
        # In particular, refuse links and Windows 8.3 aliases even if a path
        # appears lexically beneath the root.
        real = path.resolve(strict=True)
        if not real.is_relative_to(self.root) or real != path:
            raise FileToolError("unsafe_path")

    def _directory(self, path: str, *, create: bool = False) -> Path:
        _check_root(self.root, self.identity)
        relative = _directory_name(path)
        cursor = self.root
        for component in relative.split("/") if relative else ():
            cursor /= component
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                if not create:
                    raise FileToolError("not_found") from None
                cursor.mkdir(mode=0o700)
                info = cursor.lstat()
            if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise FileToolError("unsafe_path")
            self._canonical(cursor)
        return cursor

    def _file(self, path: str, *, create_parents: bool = False, required: bool = True
              ) -> tuple[str, Path, os.stat_result | None]:
        relative = _file_name(path)
        parent_name = relative.rpartition("/")[0]
        parent = self._directory(parent_name, create=create_parents)
        candidate = parent / relative.rsplit("/", 1)[-1]
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if required:
                raise FileToolError("not_found") from None
            return relative, candidate, None
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise FileToolError("unsafe_path")
        self._canonical(candidate)
        return relative, candidate, info

    def _read(self, path: str) -> tuple[str, bytes]:
        relative, target, info = self._file(path)
        if info is None:
            raise FileToolError("not_found")
        if info.st_size > MAX_FILE_BYTES:
            raise FileToolError("file_too_large")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            before = os.fstat(descriptor)
            if (
                _is_reparse(before) or not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                or before.st_size > MAX_FILE_BYTES
            ):
                raise FileToolError("unsafe_file")
            chunks: list[bytes] = []
            count = 0
            while count <= MAX_FILE_BYTES:
                block = os.read(descriptor, min(65536, MAX_FILE_BYTES + 1 - count))
                if not block:
                    break
                chunks.append(block)
                count += len(block)
            contents = b"".join(chunks)
            after = os.fstat(descriptor)
            if (
                count > MAX_FILE_BYTES or count != before.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            ):
                raise FileToolError("unsafe_file")
        finally:
            os.close(descriptor)
        return relative, contents

    def _text(self, path: str) -> tuple[str, bytes, str]:
        relative, contents = self._read(path)
        if b"\x00" in contents:
            raise FileToolError("binary_file")
        try:
            decoded = contents.decode("utf-8")
        except UnicodeError:
            raise FileToolError("binary_file") from None
        return relative, contents, decoded

    def _write(self, path: str, contents: bytes) -> str:
        if len(contents) > MAX_FILE_BYTES:
            raise FileToolError("file_too_large")
        relative, target, original = self._file(path, create_parents=True, required=False)
        staged: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".cortex-agent-", dir=target.parent)
            staged = temporary
            with os.fdopen(descriptor, "wb") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            if original is not None:
                os.chmod(temporary, stat.S_IMODE(original.st_mode))
            # Revalidate every existing component immediately before replacing.
            self._file(path, required=False)
            os.replace(temporary, target)
        finally:
            if staged is not None:
                try:
                    os.unlink(staged)
                except FileNotFoundError:
                    pass
        return relative

    def _files(self, directory: str, context: ToolContext) -> Iterator[str]:
        pending = [directory]
        scanned = 0
        while pending:
            self._check(context)
            location = pending.pop()
            folder = self._directory(location)
            children = _children(folder)
            for item in children:
                self._check(context)
                scanned += 1
                if scanned > MAX_SCANNED_ENTRIES:
                    raise FileToolError("scan_limit")
                candidate = Path(item.path)
                info = candidate.lstat()
                if _is_reparse(info):
                    continue
                is_directory = stat.S_ISDIR(info.st_mode)
                if not (is_directory or stat.S_ISREG(info.st_mode)):
                    continue
                if not _permitted_name(item.name, directory=is_directory):
                    continue
                relative = f"{location}/{item.name}" if location else item.name
                try:
                    _checked_parts(relative)
                    self._canonical(candidate)
                except FileToolError:
                    continue
                if is_directory:
                    pending.append(relative)
                else:
                    _file_name(relative)
                    yield relative

    def _list_dir(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        name = _string(args, "path", default=".")
        self._check(context)
        folder = self._directory(name)
        relative = _directory_name(name)
        entries: list[dict[str, str]] = []
        children = _children(folder)
        for item in children:
            self._check(context)
            candidate = Path(item.path)
            info = candidate.lstat()
            is_directory = stat.S_ISDIR(info.st_mode)
            if _is_reparse(info) or not (is_directory or stat.S_ISREG(info.st_mode)):
                continue
            if not _permitted_name(item.name, directory=is_directory):
                continue
            path = f"{relative}/{item.name}" if relative else item.name
            try:
                _checked_parts(path)
                self._canonical(candidate)
                if not is_directory:
                    _file_name(path)
            except FileToolError:
                continue
            if len(entries) >= MAX_RESULTS:
                return {"path": relative, "entries": entries, "truncated": True}
            entries.append({"name": item.name, "path": path, "type": "directory" if is_directory else "file"})
        return {"path": relative, "entries": entries, "truncated": False}

    def _glob(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        pattern = _glob_pattern(_string(args, "pattern"))
        directory = _directory_name(_string(args, "path", default="."))
        self._check(context)
        self._directory(directory)
        results: list[str] = []
        for file in self._files(directory, context):
            scoped = file[len(directory) + 1:] if directory else file
            if _matches_glob(scoped, pattern):
                if len(results) >= MAX_RESULTS:
                    return {"paths": results, "truncated": True}
                results.append(file)
        return {"paths": results, "truncated": False}

    def _grep_file(self, contents: str, expression: str, limit: int,
                   context: ToolContext) -> tuple[list[tuple[int, str]], bool]:
        parent, child = multiprocessing.get_context("spawn").Pipe(duplex=False)
        process = multiprocessing.get_context("spawn").Process(
            target=_grep_worker, args=(contents, expression, limit, child), daemon=True,
        )
        try:
            process.start()
            child.close()
            deadline = monotonic() + GREP_FILE_TIMEOUT_SECONDS
            while not parent.poll(0.05):
                if context.stopped():
                    raise ToolInterrupted("grep interrupted")
                if monotonic() >= deadline:
                    raise FileToolError("regex_timeout")
                if not process.is_alive():
                    raise FileToolError("regex_worker_failed")
            result: tuple[list[tuple[int, str]], bool] = parent.recv()
            return result
        finally:
            parent.close()
            child.close()
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=0.5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.5)

    def _grep(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        expression = _string(args, "pattern")
        if not expression or len(expression) > _MAX_REGEX_LENGTH:
            raise FileToolError("invalid_arguments")
        try:
            re.compile(expression)
        except re.error:
            raise FileToolError("invalid_regex") from None
        location = _string(args, "path", default=".")
        selector = _glob_pattern(_string(args, "glob", default="**"))
        self._check(context)
        if location not in {"", "."}:
            try:
                _file_name(location)
            except FileToolError:
                directory = _directory_name(location)
                self._directory(directory)
                paths: Iterator[str] = self._files(directory, context)
            else:
                name, _, _ = self._file(location)
                directory = name.rpartition("/")[0]
                paths = iter((name,))
        else:
            directory = ""
            paths = self._files(directory, context)
        hits: list[dict[str, object]] = []
        for path in paths:
            self._check(context)
            scoped = path[len(directory) + 1:] if directory else path
            if not _matches_glob(scoped, selector):
                continue
            try:
                _, _, text = self._text(path)
            except FileToolError as error:
                if error.code in {"binary_file", "file_too_large"}:
                    continue
                raise
            matches, excess = self._grep_file(text, expression, MAX_GREP_HITS - len(hits), context)
            for number, excerpt in matches:
                hits.append({"path": path, "line": number, "text": excerpt})
            if excess:
                return {"hits": hits, "truncated": True}
        return {"hits": hits, "truncated": False}

    def _read_file(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        path = _string(args, "path")
        start = _integer(args, "start_line", default=1)
        end = _integer(args, "end_line", default=start + MAX_READ_LINES - 1)
        if end < start or end - start + 1 > MAX_READ_LINES:
            raise FileToolError("line_limit")
        self._check(context)
        relative, contents, text = self._text(path)
        lines = text.splitlines(keepends=True)
        actual_end = min(end, len(lines))
        selected = "".join(lines[start - 1:actual_end])
        return {
            "path": relative, "start_line": start, "end_line": actual_end,
            "total_lines": len(lines), "sha256": sha256(contents).hexdigest(),
            "content": selected,
        }

    def _write_file(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        path = _string(args, "path")
        contents = _string(args, "content")
        if "\x00" in contents:
            raise FileToolError("binary_file")
        data = contents.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise FileToolError("file_too_large")
        self._check(context)
        relative = self._write(path, data)
        return {"path": relative, "bytes": len(data), "sha256": sha256(data).hexdigest()}

    def _edit_file(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        path = _string(args, "path")
        old = _string(args, "old")
        new = _string(args, "new")
        replace_all = args.get("replace_all", False)
        if not isinstance(replace_all, bool) or not old or "\x00" in new:
            raise FileToolError("invalid_arguments")
        self._check(context)
        relative, _, source = self._text(path)
        first = source.find(old)
        if first < 0:
            raise FileToolError("no_match")
        second = source.find(old, first + 1)
        if not replace_all and second >= 0:
            raise FileToolError("ambiguous_edit")
        updated = source.replace(old, new) if replace_all else source[:first] + new + source[first + len(old):]
        encoded = updated.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise FileToolError("file_too_large")
        self._write(relative, encoded)
        return {"path": relative, "replacements": source.count(old) if replace_all else 1,
                "sha256": sha256(encoded).hexdigest()}

    def _delete_file(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        path = _string(args, "path")
        self._check(context)
        relative, target, _ = self._file(path)
        target.unlink()
        return {"path": relative, "deleted": True}

    def _move_file(self, args: Mapping[str, Any], context: ToolContext) -> dict[str, object]:
        source = _string(args, "source")
        destination = _string(args, "destination")
        self._check(context)
        src, source_path, _ = self._file(source)
        dst, dest_path, info = self._file(destination, create_parents=True, required=False)
        if src == dst or info is not None:
            raise FileToolError("destination_exists")
        self._file(src)
        self._file(dst, required=False)
        os.rename(source_path, dest_path)
        return {"source": src, "destination": dst}

    def handle(self, call: ToolCall, context: ToolContext) -> ToolResult:
        try:
            self._check(context)
            args = call.arguments
            if call.name == "list_dir":
                result = self._list_dir(args, context)
            elif call.name == "glob":
                result = self._glob(args, context)
            elif call.name == "grep":
                result = self._grep(args, context)
            elif call.name == "read_file":
                result = self._read_file(args, context)
            elif call.name == "write_file":
                result = self._write_file(args, context)
            elif call.name == "edit_file":
                result = self._edit_file(args, context)
            elif call.name == "delete_file":
                result = self._delete_file(args, context)
            elif call.name == "move_file":
                result = self._move_file(args, context)
            else:
                raise FileToolError("unknown_tool")
            self._check(context)
            return ToolResult(json.dumps(result, ensure_ascii=False), summary="Work-copy file tool completed")
        except FileToolError as error:
            code = error.code
        except WorkCopyError as error:
            code = error.code
        except (OSError, ValueError, UnicodeError, EOFError):
            code = "file_operation_failed"
        # No exception messages, filenames or source bytes enter error output.
        return ToolResult(json.dumps({"error": code}), summary="Work-copy file tool refused", success=False)


_READ_ONLY_TOOLS: Final = frozenset({"list_dir", "glob", "grep", "read_file"})


def register_file_tools(
    registry: ToolRegistry, workcopy_root: str | os.PathLike[str], *,
    read_only: bool = False, require_approval: bool = False,
) -> None:
    """Offer work-copy tools, optionally withholding writes and gating each mutation.

    Live runs set require_approval even in auto_sandboxed mode: a disposable
    work copy is not proof of a verified process sandbox. The only way through
    the gate is ToolContext.approved, supplied after a one-shot ledger decision.
    """
    files = _Files(workcopy_root)

    def handle(call: ToolCall, context: ToolContext) -> ToolResult | NeedsApproval:
        if require_approval and call.name not in _READ_ONLY_TOOLS and not context.approved:
            request: dict[str, object] = {
                "tool": call.name,
                "summary": "Stage this change in the disposable work copy.",
            }
            if call.name == "move_file":
                request["source"] = str(call.arguments.get("source", ""))
                request["destination"] = str(call.arguments.get("destination", ""))
            else:
                request["path"] = str(call.arguments.get("path", ""))
            return NeedsApproval(request)
        return files.handle(call, context)

    for spec in FILE_TOOLS:
        if read_only and spec.name not in _READ_ONLY_TOOLS:
            continue
        registry.register(spec, handle)
