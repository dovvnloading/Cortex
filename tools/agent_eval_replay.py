"""Offline replay launchers and tiny real Git-format repository for synthetic agent tasks.

No subprocesses or profiles are created here. Only the fixture's pytest command
and a small, known Git commit operation are emulated by the scripted benchmark.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from hashlib import sha1
from pathlib import Path
import re
import struct
from typing import Literal
import zlib

from cortex_backend.agent.sandbox.base import Launcher, ProcessHandle, SandboxLevel
from cortex_backend.core.win_jobs import JobLimits

_GIT_FILE = "release.txt"
_OBJECT_LIMIT = 1024 * 1024


def _git_object(git_dir: Path, kind: str, content: bytes) -> str:
    payload = f"{kind} {len(content)}\0".encode("ascii") + content
    digest = sha1(payload).hexdigest()
    target = git_dir / "objects" / digest[:2] / digest[2:]
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(zlib.compress(payload))
    return digest


def _git_write_index(root: Path, blob_sha: str) -> None:
    """Write a legitimate single-file v2 Git index without invoking Git."""
    path = root / _GIT_FILE
    info = path.stat()
    header = struct.pack(
        ">10I20sH", int(info.st_ctime), info.st_ctime_ns % 1_000_000_000,
        int(info.st_mtime), info.st_mtime_ns % 1_000_000_000,
        info.st_dev & 0xFFFFFFFF, info.st_ino & 0xFFFFFFFF, 0o100644,
        info.st_uid & 0xFFFFFFFF, info.st_gid & 0xFFFFFFFF, info.st_size,
        bytes.fromhex(blob_sha), len(_GIT_FILE),
    )
    entry = header + _GIT_FILE.encode("ascii") + b"\0"
    entry += b"\0" * (-len(entry) % 8)
    data = b"DIRC" + struct.pack(">II", 2, 1) + entry
    (root / ".git" / "index").write_bytes(data + bytes.fromhex(sha1(data).hexdigest()))


def _git_commit_tree(root: Path, parent: str | None, message: str) -> str:
    git_dir = root / ".git"
    blob_sha = _git_object(git_dir, "blob", (root / _GIT_FILE).read_bytes())
    tree_sha = _git_object(git_dir, "tree", b"100644 release.txt\0" + bytes.fromhex(blob_sha))
    lines = [f"tree {tree_sha}"]
    if parent is not None:
        lines.append(f"parent {parent}")
    lines.extend((
        "author Cortex Eval <eval@cortex.invalid> 0 +0000",
        "committer Cortex Eval <eval@cortex.invalid> 0 +0000",
    ))
    commit_sha = _git_object(git_dir, "commit", ("\n".join(lines) + "\n\n" + message + "\n").encode())
    (git_dir / "refs" / "heads" / "main").write_text(commit_sha + "\n", encoding="ascii")
    _git_write_index(root, blob_sha)
    return commit_sha


def init_git_fixture(root: Path) -> str:
    """Build a genuine, isolated Git repository in an already-copied temp tree."""
    git_dir = root / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (git_dir / "info").mkdir()
    (git_dir / "info" / "exclude").write_text(
        ".cortex-agent-git-home/\n.cortex-agent-tmp/\n.cortex-agent-localappdata/\n"
        ".pytest_cache/\n__pycache__/\n",
        encoding="utf-8",
    )
    (git_dir / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\tfilemode = false\n\tbare = false\n\tlogallrefupdates = true\n",
        encoding="utf-8",
    )
    return _git_commit_tree(root, None, "Fixture baseline")


def _git_read_object(git_dir: Path, digest: str, kind: str) -> bytes:
    if re.fullmatch(r"[0-9a-f]{40}", digest) is None:
        raise ValueError("invalid_git_object")
    path = git_dir / "objects" / digest[:2] / digest[2:]
    if not path.is_file() or path.stat().st_size > _OBJECT_LIMIT:
        raise ValueError("invalid_git_object")
    compressed = path.read_bytes()
    decompressor = zlib.decompressobj()
    inflated = decompressor.decompress(compressed, _OBJECT_LIMIT + 1)
    if not decompressor.eof or decompressor.unconsumed_tail or len(inflated) > _OBJECT_LIMIT:
        raise ValueError("invalid_git_object")
    marker, separator, content = inflated.partition(b"\0")
    if (not separator or marker != f"{kind} {len(content)}".encode("ascii")
            or sha1(inflated).hexdigest() != digest):
        raise ValueError("invalid_git_object")
    return content


def verify_git_fixture(root: Path, baseline: str, expected: Mapping[str, bytes]) -> tuple[bool, bool]:
    """Check the actual work-copy commit graph and HEAD tree, not a fake status."""
    try:
        git_dir = root / ".git"
        if (git_dir / "HEAD").read_text(encoding="ascii") != "ref: refs/heads/main\n":
            return False, False
        head = (git_dir / "refs" / "heads" / "main").read_text(encoding="ascii").strip()
        commit = _git_read_object(git_dir, head, "commit")
        metadata, separator, _message = commit.partition(b"\n\n")
        if not separator:
            return False, False
        lines = metadata.decode("ascii").splitlines()
        parents = [line.removeprefix("parent ") for line in lines if line.startswith("parent ")]
        exactly_one = head != baseline and parents == [baseline]
        baseline_commit = _git_read_object(git_dir, baseline, "commit")
        exactly_one = exactly_one and b"\nparent " not in baseline_commit
        tree_lines = [line.removeprefix("tree ") for line in lines if line.startswith("tree ")]
        if len(tree_lines) != 1:
            return exactly_one, False
        tree = _git_read_object(git_dir, tree_lines[0], "tree")
        prefix = b"100644 release.txt\0"
        if not tree.startswith(prefix) or len(tree) != len(prefix) + 20:
            return exactly_one, False
        contents = _git_read_object(git_dir, tree[len(prefix):].hex(), "blob")
        return exactly_one, expected == {_GIT_FILE: contents}
    except (OSError, UnicodeError, ValueError, zlib.error):
        return False, False


class ReplayProcess:
    level: SandboxLevel = "appcontainer"
    pid = 0

    def __init__(self, exit_code: int, output: bytes) -> None:
        self.exit_code = exit_code
        self.output = output

    def stdout_chunks(self) -> Iterator[bytes]:
        if self.output:
            yield self.output

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code

    def kill_tree(self) -> None:
        self.exit_code = 1


class ReplayCheckLauncher(Launcher):
    """Only report pytest success when the copied one-line bug is fixed."""
    level: Literal["appcontainer"] = "appcontainer"

    def __init__(self, root: Path, corrected: bytes) -> None:
        self.root = root.resolve()
        self.corrected = corrected
        self.commands: list[str] = []

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: JobLimits | None = None,
    ) -> ProcessHandle:
        if env is not None or network or limits is not None or Path(cwd).resolve() != self.root:
            raise RuntimeError("unsafe_eval_launch")
        command = argv[-1] if argv else ""
        self.commands.append(command)
        if command != "python -m pytest":
            return ReplayProcess(1, b"Only fixture pytest is supported\n")
        passed = (self.root / "calculator.py").read_bytes() == self.corrected
        return ReplayProcess(0 if passed else 1, b"1 passed (offline replay)\n" if passed else b"1 failed\n")


class ReplayGitLauncher:
    """The real Git tool dispatches into this offline, single-repo replay."""
    level: Literal["appcontainer"] = "appcontainer"

    def __init__(self, workcopy: str | Path, _profile: str) -> None:
        self.root = Path(workcopy).resolve()

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: object = None,
    ) -> ReplayProcess:
        if (env is not None or network or limits is not None or Path(cwd).resolve() != self.root
                or not argv or argv[0] != "git" or "--git-dir=.git" not in argv):
            raise RuntimeError("unsafe_eval_git_launch")
        if "add" in argv:
            return ReplayProcess(0, b"")
        if "commit" in argv and "-m" in argv:
            message = argv[argv.index("-m") + 1]
            parent = (self.root / ".git" / "refs" / "heads" / "main").read_text(encoding="ascii").strip()
            self.commit(parent, message)
            return ReplayProcess(0, b"[main] committed\n")
        if "status" in argv:
            return ReplayProcess(0, b"")
        return ReplayProcess(1, b"unsupported offline git operation\n")

    def commit(self, parent: str, message: str) -> None:
        _git_commit_tree(self.root, parent, message)
