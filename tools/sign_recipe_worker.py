"""Sign a built one-folder recipe worker without persisting private key material.

The caller supplies an external raw Ed25519 private key file. The tool emits only
the signed manifest and bounded release metadata; installation still belongs to
``SignedBundleInstaller`` with an independently pinned public-key trust root.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from cortex_backend.execution.worker_release import (  # noqa: E402
    WorkerReleaseError,
    _path_has_reparse_component,
    build_signed_worker_manifest,
)


def _read_private_key(path: Path) -> bytes:
    if _path_has_reparse_component(path):
        raise WorkerReleaseError("release_signing_key_invalid")
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size != 32 or int(getattr(stat, "st_nlink", 1)) != 1:
            raise WorkerReleaseError("release_signing_key_invalid")
        identity = (
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
            int(getattr(stat, "st_ino", 0)),
            int(getattr(stat, "st_dev", 0)),
        )
        key = path.read_bytes()
        after = path.stat()
    except WorkerReleaseError:
        raise
    except OSError:
        raise WorkerReleaseError("release_signing_key_invalid") from None
    if (
        len(key) != 32
        or identity
        != (
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
            int(getattr(after, "st_ino", 0)),
            int(getattr(after, "st_dev", 0)),
        )
    ):
        raise WorkerReleaseError("release_signing_key_changed")
    return key


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    if _path_has_reparse_component(path):
        raise WorkerReleaseError("release_manifest_write_failed")
    if path.exists() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise WorkerReleaseError("release_manifest_exists")
    temporary: Path | None = None
    created = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if _path_has_reparse_component(path):
            raise WorkerReleaseError("release_manifest_write_failed")
        temporary = path.with_name(f".{path.name}.staging-{os.getpid()}")
        if temporary.exists() or temporary.is_symlink():
            raise WorkerReleaseError("release_manifest_exists")
        created = True
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            encoding="ascii",
        )
        os.replace(temporary, path)
    except WorkerReleaseError:
        raise
    except OSError:
        raise WorkerReleaseError("release_manifest_write_failed") from None
    finally:
        if created and temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--bundle-version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="Emit bounded metadata as JSON.")
    args = parser.parse_args(argv)
    try:
        source_root = args.source_root
        if _path_has_reparse_component(args.output_manifest):
            raise WorkerReleaseError("release_manifest_write_failed")
        try:
            source_resolved = source_root.resolve(strict=True)
            output_manifest = args.output_manifest.resolve()
        except (OSError, RuntimeError):
            raise WorkerReleaseError("release_root_unavailable") from None
        if output_manifest == source_resolved or output_manifest.is_relative_to(source_resolved):
            raise WorkerReleaseError("release_manifest_inside_source")
        release = build_signed_worker_manifest(
            source_root,
            private_key_bytes=_read_private_key(args.private_key),
            key_id=args.key_id,
            bundle_version=args.bundle_version,
            sequence=args.sequence,
        )
        _write_manifest(output_manifest, release.manifest)
    except WorkerReleaseError as error:
        if args.json:
            print(json.dumps({"status": "blocked", "code": error.code}, separators=(",", ":")))
        else:
            print(f"release blocked: {error.code}", file=sys.stderr)
        return 2
    result = {
        "status": "signed",
        "entry_count": release.entry_count,
        "manifest_digest": release.manifest_digest,
        "worker_size": release.worker_size,
        "worker_sha256": release.worker_sha256,
        "manifest": str(output_manifest),
    }
    print(json.dumps(result, separators=(",", ":") if args.json else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
