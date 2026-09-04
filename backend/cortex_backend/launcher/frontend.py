"""Deterministic source-mode frontend preparation with atomic replacement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any

from cortex_backend import __version__


logger = logging.getLogger(__name__)

MANIFEST_NAME = ".cortex-build.json"
BUILD_LOCK_NAME = ".cortex-frontend-build.lock"
INSTALL_MANIFEST_NAME = ".cortex-install.json"
INSTALL_CACHE_DIRNAME = ".cortex-install-cache"
TRACKED_CONFIG = (
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
)


class FrontendBuildError(RuntimeError):
    """Raised when the source frontend cannot be prepared safely."""


@dataclass(frozen=True, slots=True)
class FrontendManifest:
    lock_digest: str
    source_digest: str
    node_major: int
    npm_major: int
    built_at: str
    cortex_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "lock_digest": self.lock_digest,
            "source_digest": self.source_digest,
            "node_major": self.node_major,
            "npm_major": self.npm_major,
            "built_at": self.built_at,
            "cortex_version": self.cortex_version,
        }


def _digest_files(frontend_root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    def relative_path(path: Path) -> str:
        try:
            return path.relative_to(frontend_root).as_posix()
        except ValueError:
            # The generated API contract lives beside (not under) the
            # frontend tree.  Include a stable relative label without ever
            # resolving arbitrary paths outside the repository layout.
            return Path(os.path.relpath(path, frontend_root)).as_posix()

    for path in sorted(paths, key=relative_path):
        relative = relative_path(path).encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _tracked_files(frontend_root: Path) -> list[Path]:
    files = [frontend_root / name for name in TRACKED_CONFIG]
    files.extend(
        path
        for extension in ("*.ts", "*.tsx", "*.css")
        for path in (frontend_root / "src").rglob(extension)
    )
    public_dir = frontend_root / "public"
    if public_dir.is_dir():
        files.extend(path for path in public_dir.rglob("*") if path.is_file())
    files.extend(path for path in frontend_root.glob(".env*") if path.is_file())
    contract = frontend_root.parent / "contracts" / "cortex-api.ts"
    if contract.is_file():
        files.append(contract)
    return [path for path in files if path.is_file()]


def lock_digest(frontend_root: Path) -> str:
    lockfile = frontend_root / "package-lock.json"
    if not lockfile.is_file():
        raise FrontendBuildError("frontend/package-lock.json is required for source builds.")
    return _digest_files(frontend_root, [lockfile])


def source_digest(frontend_root: Path) -> str:
    digest = hashlib.sha256(_digest_files(frontend_root, _tracked_files(frontend_root)).encode("ascii"))
    # Vite embeds VITE_* process variables in the bundle even when there is no
    # corresponding .env file.  Hash names and values, never persist values in
    # the manifest itself.
    for name, value in sorted(os.environ.items()):
        if name.startswith("VITE_"):
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def _tool_name(name: str) -> str:
    if os.name == "nt" and name == "npm":
        return "npm.cmd"
    return name


def _major_version(command: str) -> int:
    try:
        result = subprocess.run(
            [_tool_name(command), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FrontendBuildError(f"{command} is required to build the frontend.") from exc
    value = result.stdout.strip().lstrip("v").split(".", 1)[0]
    try:
        return int(value)
    except ValueError as exc:
        raise FrontendBuildError(f"Could not determine the {command} major version.") from exc


def read_manifest(dist: Path) -> FrontendManifest | None:
    path = dist / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return FrontendManifest(
            lock_digest=str(payload["lock_digest"]),
            source_digest=str(payload["source_digest"]),
            node_major=int(payload["node_major"]),
            npm_major=int(payload["npm_major"]),
            built_at=str(payload["built_at"]),
            cortex_version=str(payload["cortex_version"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def needs_build(frontend_root: Path, *, force: bool = False) -> bool:
    dist = frontend_root / "dist"
    if force or not (dist / "index.html").is_file():
        return True
    manifest = read_manifest(dist)
    if manifest is None:
        return True
    node_major = _major_version("node")
    npm_major = _major_version("npm")
    return (
        manifest.lock_digest != lock_digest(frontend_root)
        or manifest.source_digest != source_digest(frontend_root)
        or manifest.node_major != node_major
        or manifest.npm_major != npm_major
    )


def _run(command: list[str], *, cwd: Path) -> None:
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except OSError as exc:
        raise FrontendBuildError(f"Could not start {command[0]}.") from exc
    except subprocess.CalledProcessError as exc:
        raise FrontendBuildError(
            f"Frontend command failed with exit code {exc.returncode}."
        ) from exc


@contextmanager
def _frontend_build_lock(frontend_root: Path):
    """Serialize source builds and shared install-cache mutation.

    The lock is an OS-level byte-range lock on a persistent, ignored file. A
    persistent inode avoids the unlink/recreate race that would let a second
    process miss the first process's lock while it is cleaning up.
    """

    lock_path = frontend_root / BUILD_LOCK_NAME
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise FrontendBuildError("Could not open the frontend build lock.") from exc
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                # POSIX-only; this is the non-nt branch.
                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
            locked = True
        except (OSError, ImportError) as exc:
            raise FrontendBuildError("Another frontend build is already running.") from exc
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(),
                        fcntl.LOCK_UN,  # type: ignore[attr-defined]
                    )
            except OSError:
                logger.warning("Could not release the frontend build lock: %s", lock_path)
        handle.close()


def _reclaim_stale_staging_directories(parent: Path, keep: Path) -> None:
    """Remove staging directories orphaned by a crashed or killed build."""
    try:
        candidates = list(parent.glob(".cortex-frontend-build-*"))
    except OSError as exc:
        logger.warning("Could not scan %s for stale frontend build directories: %s", parent, exc)
        return
    for candidate in candidates:
        if candidate == keep:
            continue
        try:
            shutil.rmtree(candidate)
            logger.info("Reclaimed stale frontend build staging directory: %s", candidate)
        except OSError as exc:
            logger.warning(
                "Could not remove stale frontend build directory %s: %s", candidate, exc
            )


def _stage_frontend_source(frontend_root: Path) -> Path:
    """Copy build inputs beside the source tree so live installs stay untouched."""
    staging = frontend_root.parent / f".cortex-frontend-build-{uuid.uuid4().hex}"
    _reclaim_stale_staging_directories(frontend_root.parent, staging)
    try:
        shutil.copytree(
            frontend_root,
            staging,
            ignore=shutil.ignore_patterns(
                "node_modules",
                "dist",
                ".cortex-*",
                "coverage",
                "test-results",
                "playwright-report",
            ),
        )
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise FrontendBuildError(
            "Could not stage frontend sources for an isolated build."
        ) from exc
    return staging


def _install_cache_root(frontend_root: Path) -> Path:
    """Stable cache directory that survives per-build staging churn."""
    return frontend_root / INSTALL_CACHE_DIRNAME


def _install_if_needed(build_root: Path, expected_lock_digest: str, cache_root: Path) -> None:
    """Install dependencies into ``build_root``, reusing a stable cache when possible."""
    node_modules = build_root / "node_modules"
    cached_node_modules = cache_root / "node_modules"
    marker = cache_root / INSTALL_MANIFEST_NAME
    cached_digest = None
    if marker.is_file():
        try:
            cached_digest = json.loads(marker.read_text(encoding="utf-8"))["lock_digest"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            cached_digest = None
    if cached_digest == expected_lock_digest and cached_node_modules.is_dir():
        shutil.copytree(cached_node_modules, node_modules)
        return
    _run([_tool_name("npm"), "ci"], cwd=build_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    if cached_node_modules.exists():
        shutil.rmtree(cached_node_modules)
    shutil.copytree(node_modules, cached_node_modules)
    marker.write_text(
        json.dumps({"lock_digest": expected_lock_digest}, indent=2),
        encoding="utf-8",
    )


def build_frontend(
    frontend_root: Path,
    *,
    cortex_version: str = __version__,
) -> Path:
    frontend_root = frontend_root.resolve()
    if not (frontend_root / "package.json").is_file():
        raise FrontendBuildError("frontend/package.json is missing from the source checkout.")
    with _frontend_build_lock(frontend_root):
        build_root = _stage_frontend_source(frontend_root)
        staging = build_root / f".cortex-dist-staging-{uuid.uuid4().hex}"
        dist = frontend_root / "dist"
        backup = frontend_root / f".cortex-dist-backup-{uuid.uuid4().hex}"
        try:
            lock = lock_digest(build_root)
            source = source_digest(build_root)
            node_major = _major_version("node")
            npm_major = _major_version("npm")
            _install_if_needed(build_root, lock, _install_cache_root(frontend_root))
            _run(
                [_tool_name("npm"), "run", "build", "--", "--outDir", str(staging)],
                cwd=build_root,
            )
            if not (staging / "index.html").is_file():
                raise FrontendBuildError("Frontend build completed without index.html.")
            manifest = FrontendManifest(
                lock_digest=lock,
                source_digest=source,
                node_major=node_major,
                npm_major=npm_major,
                built_at=datetime.now(timezone.utc).isoformat(),
                cortex_version=cortex_version,
            )
            (staging / MANIFEST_NAME).write_text(
                json.dumps(manifest.as_dict(), indent=2),
                encoding="utf-8",
            )
            if dist.exists():
                os.replace(dist, backup)
            try:
                os.replace(staging, dist)
            except OSError:
                if backup.exists() and not dist.exists():
                    os.replace(backup, dist)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return dist
        except FrontendBuildError:
            raise
        except OSError as exc:
            raise FrontendBuildError("Could not atomically install the frontend bundle.") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if build_root.exists():
                shutil.rmtree(build_root, ignore_errors=True)
            if backup.exists() and not dist.exists():
                os.replace(backup, dist)


def ensure_frontend(
    frontend_root: Path,
    *,
    force: bool = False,
    skip_check: bool = False,
    packaged: bool = False,
    cortex_version: str = __version__,
) -> Path:
    """Return a verified bundle, building only in an identifiable source tree."""
    frontend_root = frontend_root.resolve()
    dist = frontend_root / "dist"
    if packaged:
        if not (dist / "index.html").is_file():
            raise FrontendBuildError("Packaged Cortex is missing its frontend bundle.")
        return dist
    if skip_check:
        if not (dist / "index.html").is_file():
            raise FrontendBuildError("--skip-build-check requested but frontend/dist is missing.")
        return dist
    if needs_build(frontend_root, force=force):
        return build_frontend(frontend_root, cortex_version=cortex_version)
    return dist
