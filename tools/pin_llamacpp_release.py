"""Compute the pinned SHA-256 hashes for a ggml-org/llama.cpp release.

ggml-org's GitHub releases do not publish official per-asset checksums, so
Cortex pins its own: this tool downloads the Windows CPU and Vulkan release
assets for one build tag, hashes the archive and the extracted
``llama-server.exe`` separately, and prints a ``PinnedRelease`` literal ready
to paste into ``backend/cortex_backend/llamacpp/binary_release.py`` as the
new ``CURRENT_RELEASE`` value.

This performs real network downloads (two zip archives, each roughly
50-150MB) -- run it deliberately when bumping the pinned llama.cpp version,
not as part of any automated build or test step.

Usage:
    python tools/pin_llamacpp_release.py b6142
"""

from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from cortex_backend.llamacpp.binary_fetcher import hash_directory  # noqa: E402

RELEASE_DOWNLOAD_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"
ASSET_FILENAME_PATTERNS = {
    "cpu": "llama-{tag}-bin-win-cpu-x64.zip",
    "vulkan": "llama-{tag}-bin-win-vulkan-x64.zip",
}
# llama.cpp's release .exe files are tiny stubs that dynamically load a
# same-directory "-impl.dll" -- the pinned hash covers the whole extracted
# directory (see hash_directory), but the launched entry point is still
# just this one file, relative to that directory.
EXECUTABLE_RELPATH = "llama-server.exe"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_flat(archive_bytes: bytes, destination: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        archive.extractall(destination)
    # Release zips extract into a single top-level folder; flatten so the
    # executable lands directly at <destination>/llama-server.exe, matching
    # what BinaryFetcher._extract does at runtime.
    entries = list(destination.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for item in inner.iterdir():
            item.rename(destination / item.name)
        inner.rmdir()


def pin_release(tag: str) -> str:
    lines = ["PinnedRelease(", f'    tag="{tag}",', "    assets={"]
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)) as client:
        for backend, pattern in ASSET_FILENAME_PATTERNS.items():
            filename = pattern.format(tag=tag)
            url = f"{RELEASE_DOWNLOAD_BASE}/{tag}/{filename}"
            print(f"Downloading {url} ...", file=sys.stderr)
            response = client.get(url)
            response.raise_for_status()
            archive_bytes = response.content
            with tempfile.TemporaryDirectory() as tmp:
                extract_dir = Path(tmp) / backend
                _extract_flat(archive_bytes, extract_dir)
                if not (extract_dir / EXECUTABLE_RELPATH).is_file():
                    raise SystemExit(
                        f"{EXECUTABLE_RELPATH} not found in {filename} -- release layout may have changed."
                    )
                directory_hash = hash_directory(extract_dir)
            lines.append(
                f'        "{backend}": AssetSpec(\n'
                f'            filename="{filename}",\n'
                f'            archive_sha256="{_sha256(archive_bytes)}",\n'
                f'            directory_sha256="{directory_hash}",\n'
                f'            executable_relpath="{EXECUTABLE_RELPATH}",\n'
                f"        ),"
            )
    lines.append("    },")
    lines.append(")")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python tools/pin_llamacpp_release.py <release-tag>", file=sys.stderr)
        raise SystemExit(2)
    print(pin_release(sys.argv[1]))


if __name__ == "__main__":
    main()
