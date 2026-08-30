"""Pinned llama.cpp release: exact build tag and per-asset SHA-256 hashes.

ggml-org/llama.cpp does not publish official per-asset checksums, so these
values are computed once by a maintainer (``tools/pin_llamacpp_release.py``)
when bumping ``CURRENT_RELEASE``, and pinned here as the sole trust anchor
for a binary Cortex downloads and executes on the user's behalf. Both the
CPU and Vulkan builds are MIT-licensed, matching the rest of llama.cpp --
CUDA/HIP/SYCL builds are deliberately not offered (see the GPU-backend
selection in ``server_manager.py``), since they pull in vendor-licensed
runtime bits that shouldn't be silently auto-downloaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from collections.abc import Mapping

GpuBackend = Literal["cpu", "vulkan"]

RELEASE_DOWNLOAD_BASE = "https://github.com/ggml-org/llama.cpp/releases/download"


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """One downloadable release asset and its two verification hashes.

    Recent llama.cpp Windows release archives ship each ``.exe`` as a tiny
    (~9KB) stub that dynamically loads a same-directory ``-impl.dll`` (e.g.
    ``llama-server.exe`` loads ``llama-server-impl.dll``, ~10MB) plus shared
    ``ggml-*.dll`` backend libraries -- hashing only the stub exe would leave
    the actual executable code (the impl/backend DLLs) unverified. So the
    trust anchor is a whole-directory manifest hash (``directory_sha256``,
    see ``binary_fetcher._hash_directory``) over every extracted file, not
    just the entry-point stub.
    """

    filename: str
    archive_sha256: str
    directory_sha256: str
    executable_relpath: str = "llama-server.exe"


@dataclass(frozen=True, slots=True)
class PinnedRelease:
    tag: str
    assets: Mapping[GpuBackend, AssetSpec]

    def download_url(self, backend: GpuBackend) -> str:
        asset = self.assets[backend]
        return f"{RELEASE_DOWNLOAD_BASE}/{self.tag}/{asset.filename}"


# Computed by running `python tools/pin_llamacpp_release.py <tag>` against
# ggml-org/llama.cpp release b10311 (the latest tag as of 2026-08-07) and
# pasting the printed literal here. Re-run that tool and replace this value
# to bump the pinned llama.cpp version.
CURRENT_RELEASE: PinnedRelease | None = PinnedRelease(
    tag="b10311",
    assets={
        "cpu": AssetSpec(
            filename="llama-b10311-bin-win-cpu-x64.zip",
            archive_sha256="e44896adc1f42134c394ff7ae92b6ec5a1c1b5631d539226780e2d7619bc6ff5",
            directory_sha256="c48264bd4596274fcef1a26e92c6ed3639336642b20cd046d84a13e0bef21e37",
            executable_relpath="llama-server.exe",
        ),
        "vulkan": AssetSpec(
            filename="llama-b10311-bin-win-vulkan-x64.zip",
            archive_sha256="28d43ef430024df797259f63636544db47182b1ec49d7cd1bf664d58b0ce4f53",
            directory_sha256="bf995039c856b45bc53d3faf899602a8c4de2499a6b93d25c640493ca1f974cd",
            executable_relpath="llama-server.exe",
        ),
    },
)
