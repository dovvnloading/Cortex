"""Deterministic source-mode frontend preparation with atomic replacement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any


MANIFEST_NAME = ".cortex-build.json"
INSTALL_MANIFEST_NAME = ".cortex-install.json"
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
    for path in sorted(paths, key=lambda item: item.relative_to(frontend_root).as_posix()):
        relative = path.relative_to(frontend_root).as_posix().encode("utf-8")
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
    return [path for path in files if path.is_file()]


def lock_digest(frontend_root: Path) -> str:
    lockfile = frontend_root / "package-lock.json"
    if not lockfile.is_file():
        raise FrontendBuildError("frontend/package-lock.json is required for source builds.")
    return _digest_files(frontend_root, [lockfile])


def source_digest(frontend_root: Path) -> str:
    return _digest_files(frontend_root, _tracked_files(frontend_root))


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
    return (
        manifest.lock_digest != lock_digest(frontend_root)
        or manifest.source_digest != source_digest(frontend_root)
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


def _install_if_needed(frontend_root: Path, expected_lock_digest: str) -> None:
    node_modules = frontend_root / "node_modules"
    marker = node_modules / INSTALL_MANIFEST_NAME
    installed_digest = None
    if marker.is_file():
        try:
            installed_digest = json.loads(marker.read_text(encoding="utf-8"))["lock_digest"]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            installed_digest = None
    if node_modules.is_dir() and installed_digest == expected_lock_digest:
        return
    _run([_tool_name("npm"), "ci"], cwd=frontend_root)
    node_modules.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"lock_digest": expected_lock_digest}, indent=2),
        encoding="utf-8",
    )


def build_frontend(
    frontend_root: Path,
    *,
    cortex_version: str = "0.1.0",
) -> Path:
    frontend_root = frontend_root.resolve()
    if not (frontend_root / "package.json").is_file():
        raise FrontendBuildError("frontend/package.json is missing from the source checkout.")
    lock = lock_digest(frontend_root)
    node_major = _major_version("node")
    npm_major = _major_version("npm")
    _install_if_needed(frontend_root, lock)

    staging = frontend_root / f".cortex-dist-staging-{uuid.uuid4().hex}"
    dist = frontend_root / "dist"
    backup = frontend_root / f".cortex-dist-backup-{uuid.uuid4().hex}"
    try:
        _run(
            [_tool_name("npm"), "run", "build", "--", "--outDir", str(staging)],
            cwd=frontend_root,
        )
        if not (staging / "index.html").is_file():
            raise FrontendBuildError("Frontend build completed without index.html.")
        manifest = FrontendManifest(
            lock_digest=lock,
            source_digest=source_digest(frontend_root),
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
        if backup.exists() and not dist.exists():
            os.replace(backup, dist)


def ensure_frontend(
    frontend_root: Path,
    *,
    force: bool = False,
    skip_check: bool = False,
    packaged: bool = False,
    cortex_version: str = "0.1.0",
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
