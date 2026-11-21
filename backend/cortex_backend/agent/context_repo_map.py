"""Read-only, bounded source tree and declaration outline for an agent prompt.

Project files are parsed as text or Python AST; they are never imported or run.
The existing workspace-context rules own file and directory eligibility.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import stat

from cortex_backend.services.workspace_context import (
    MAX_SCANNED_ENTRIES,
    MAX_WORKSPACE_FILE_BYTES,
    _is_reparse,
    _permitted_name,
    _read_regular_source,
    _safe_root,
)

MAX_TREE_DEPTH = 4
MAX_MAP_FILES = 1024
MAX_MAP_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SYMBOLS_PER_FILE = 32
_EXTRA_PRIVATE_DIRECTORIES = frozenset({"keys", "credentials", "secrets", ".ssh", ".aws"})
_EXTRA_PRIVATE_FILES = frozenset({"credentials.py", "secrets.py", "private_key.py", "api_key.py"})
_TRUNCATED = "[truncated]"

# One declaration per line is intentional: these are outlines, not parsers for
# languages whose complete syntax would require loading the project toolchain.
_JS_PATTERNS = (
    ("class", re.compile(r"^\s*(?:(?:export|default|declare|abstract)\s+)*class\s+([\w$]+)\b")),
    ("interface", re.compile(r"^\s*(?:(?:export|default|declare)\s+)*interface\s+(\w+)\b")),
    ("function", re.compile(r"^\s*(?:(?:export|default|declare|async)\s+)*function\s*\*?\s*([\w$]+)\s*\(")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::[^=\n]+)?=\s*"
        r"(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*=>"
    )),
    ("method", re.compile(
        r"^\s*(?:(?:public|private|protected|static|async|override|get|set)\s+)*"
        r"([\w$]+)\s*\([^;\n]*\)\s*(?::[^\{\n]+)?\s*\{"
    )),
)
_GO_PATTERNS = (
    ("method", re.compile(r"^\s*func\s+\([^)]*\)\s*(\w+)\s*\(")),
    ("function", re.compile(r"^\s*func\s+(\w+)\s*\(")),
    ("type", re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)\b")),
)
_RUST_PATTERNS = (
    ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(\w+)\b")),
    ("impl", re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:]+\s+for\s+)?([\w:]+)\b")),
    ("function", re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const)\s+)*fn\s+(\w+)\b"
    )),
)
_CS_PATTERNS = (
    ("type", re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|abstract|sealed|partial)\s+)*"
        r"(?:class|struct|interface|record)\s+(\w+)\b"
    )),
    ("method", re.compile(
        r"^\s*(?:(?:public|private|protected|internal|static|async|virtual|override|sealed|"
        r"abstract|partial|new)\s+)+[\w.<>?,\[\]]+\s+(\w+)\s*\([^;\n]*\)"
    )),
)
_REGEX_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    ".js": _JS_PATTERNS,
    ".jsx": _JS_PATTERNS,
    ".mjs": _JS_PATTERNS,
    ".cjs": _JS_PATTERNS,
    ".ts": _JS_PATTERNS,
    ".tsx": _JS_PATTERNS,
    ".go": _GO_PATTERNS,
    ".rs": _RUST_PATTERNS,
    ".cs": _CS_PATTERNS,
}
_LANGUAGE_SUFFIXES = frozenset({".py", ".pyi", *_REGEX_PATTERNS})
_CONTROL_WORDS = frozenset({"if", "for", "while", "switch", "catch", "with"})


def _python_symbols(source: str) -> tuple[list[str], bool]:
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return [], False
    symbols: list[str] = []
    for node in module.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}:{node.lineno}")
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(f"method {node.name}.{member.name}:{member.lineno}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"function {node.name}:{node.lineno}")
        if len(symbols) > MAX_SYMBOLS_PER_FILE:
            break
    return symbols[:MAX_SYMBOLS_PER_FILE], len(symbols) > MAX_SYMBOLS_PER_FILE


def _regex_symbols(source: str, suffix: str) -> tuple[list[str], bool]:
    patterns = _REGEX_PATTERNS[suffix]
    symbols: list[str] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if line.lstrip().startswith(("//", "/*", "*", "#")):
            continue
        for kind, pattern in patterns:
            match = pattern.match(line)
            if match is not None and not (kind == "method" and match.group(1) in _CONTROL_WORDS):
                symbols.append(f"{kind} {match.group(1)}:{number}")
                break
        if len(symbols) > MAX_SYMBOLS_PER_FILE:
            break
    return symbols[:MAX_SYMBOLS_PER_FILE], len(symbols) > MAX_SYMBOLS_PER_FILE


def _bounded_render(lines: list[str], budget: int, incomplete: bool) -> str:
    full = "\n".join(lines)
    if not incomplete and len(full.encode("utf-8")) <= budget:
        return full
    # Byte count is a conservative tokenizer-independent upper bound: unlike
    # character/3.5 estimates, it also covers code punctuation and Unicode.
    if budget < len(_TRUNCATED):
        return ""
    selected: list[str] = []
    used = len(_TRUNCATED.encode("utf-8"))
    for line in lines:
        addition = len(line.encode("utf-8")) + 1  # newline before this line / marker
        if used + addition > budget:
            break
        selected.append(line)
        used += addition
    return "\n".join([*selected, _TRUNCATED])


def build_repo_map(root: str | os.PathLike[str], token_budget: int) -> str:
    """Return a deterministic file tree and symbols within a conservative token ceiling.

    The caller must supply an absolute, non-linked local project directory.
    A budget too small for the truncation marker yields an empty string.
    """
    if type(token_budget) is not int or token_budget < 0:
        raise ValueError("token_budget must be a nonnegative integer")
    if token_budget == 0:
        return ""
    project_root = _safe_root(os.fspath(root))
    pending: list[tuple[Path, int]] = [(project_root, 0)]
    directories: list[str] = []
    files: list[tuple[str, Path, os.stat_result]] = []
    scanned = 0
    incomplete = False

    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                children: list[os.DirEntry[str]] = []
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_SCANNED_ENTRIES:
                        incomplete = True
                        break
                    children.append(entry)
        except OSError:
            incomplete = True
            continue
        if scanned > MAX_SCANNED_ENTRIES:
            break
        for entry in sorted(children, key=lambda item: (item.name.casefold(), item.name)):
            path = Path(entry.path)
            try:
                info = path.lstat()
            except OSError:
                continue
            if _is_reparse(info):
                continue
            relative = path.relative_to(project_root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                if (
                    not _permitted_name(entry.name, directory=True)
                    or entry.name.casefold() in _EXTRA_PRIVATE_DIRECTORIES
                ):
                    continue
                directories.append(relative + "/")
                if depth + 1 < MAX_TREE_DEPTH:
                    pending.append((path, depth + 1))
                else:
                    incomplete = True
            elif (
                stat.S_ISREG(info.st_mode)
                and _permitted_name(entry.name, directory=False)
                and entry.name.casefold() not in _EXTRA_PRIVATE_FILES
            ):
                if len(files) >= MAX_MAP_FILES:
                    incomplete = True
                    break
                files.append((relative, path, info))
        if len(files) >= MAX_MAP_FILES:
            incomplete = True
            break

    verified_files: list[str] = []
    symbol_lines: list[str] = []
    total_bytes = 0
    for relative, path, info in sorted(files, key=lambda item: (item[0].casefold(), item[0])):
        if info.st_size > MAX_WORKSPACE_FILE_BYTES or total_bytes + info.st_size > MAX_MAP_SOURCE_BYTES:
            incomplete = True
            continue
        data = _read_regular_source(path, info)
        if data is None:
            continue
        try:
            source = data.decode("utf-8-sig")
        except UnicodeError:
            continue
        total_bytes += len(data)
        verified_files.append(relative)
        suffix = path.suffix.casefold()
        if suffix not in _LANGUAGE_SUFFIXES:
            continue
        if suffix in {".py", ".pyi"}:
            symbols, clipped = _python_symbols(source)
        else:
            symbols, clipped = _regex_symbols(source, suffix)
        incomplete = incomplete or clipped
        symbol_lines.append(f"{relative}: " + ("; ".join(symbols) if symbols else "(none)"))

    display_paths = [*directories, *verified_files]
    lines = ["Files:"]
    for relative in sorted(display_paths, key=lambda path: (path.casefold(), path)):
        parts = relative.rstrip("/").split("/")
        lines.append("  " * (len(parts) - 1) + parts[-1] + ("/" if relative.endswith("/") else ""))
    lines.append("Symbols:")
    lines.extend(symbol_lines)
    return _bounded_render(lines, token_budget, incomplete)
