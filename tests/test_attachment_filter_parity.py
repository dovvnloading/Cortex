"""The composer's attachment filter must not disagree with the backend's.

The frontend keeps its own list of attachable file types (it turns away a
dropped ``.exe`` before uploading it, and feeds the file picker's ``accept``
attribute). The backend is the authority: it classifies by content and rejects
what it cannot read. A type accepted by one and refused by the other is either
a file the user cannot attach for no reason or a wasted upload, so the two lists
are kept identical. This reads the TypeScript source, so it needs no build.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from cortex_backend.services import attachments

FRONTEND_LIST = Path(__file__).resolve().parents[1] / "frontend" / "src" / "lib" / "attachments.ts"


def _quoted(block_name: str, source: str) -> set[str]:
    match = re.search(rf"{block_name}[^=]*=\s*(?:new Set\()?\[(.*?)\]", source, re.DOTALL)
    assert match, f"{block_name} not found in {FRONTEND_LIST.name}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


@pytest.fixture(scope="module")
def frontend_source() -> str:
    return FRONTEND_LIST.read_text(encoding="utf-8")


def test_frontend_extension_list_matches_the_backend_text_extensions(frontend_source: str) -> None:
    frontend = _quoted("ATTACHMENT_EXTENSIONS", frontend_source)

    assert frontend, "the frontend extension list parsed as empty"
    assert frontend == set(attachments._TEXT_EXTENSIONS), (
        f"only in frontend: {sorted(frontend - attachments._TEXT_EXTENSIONS)}; "
        f"only in backend: {sorted(attachments._TEXT_EXTENSIONS - frontend)}"
    )


def test_frontend_extensionless_names_match_the_backend(frontend_source: str) -> None:
    frontend = _quoted("ATTACHMENT_FILENAMES", frontend_source)

    assert frontend, "the frontend file-name list parsed as empty"
    assert frontend == set(attachments._TEXT_FILENAMES), (
        f"only in frontend: {sorted(frontend - attachments._TEXT_FILENAMES)}; "
        f"only in backend: {sorted(attachments._TEXT_FILENAMES - frontend)}"
    )


@pytest.mark.parametrize("extension", [".exe", ".zip", ".pdf", ".mp4", ".docx"])
def test_backend_refuses_what_the_frontend_filter_turns_away(extension: str) -> None:
    with pytest.raises(attachments.ChatAttachmentError) as refused:
        attachments._classify(f"file{extension}", b"MZ\x00\x01binary")

    assert refused.value.code == "attachment_type_unsupported"
