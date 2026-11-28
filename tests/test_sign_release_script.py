"""Behaviour of the maintainer's release-signing script (issues #704, #705).

The script needs real signing tools, so these tests load only its function
definitions through the PowerShell parser and drive them with a fake ``gh``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "sign_release.ps1"

pytestmark = pytest.mark.skipif(
    shutil.which("pwsh") is None, reason="requires PowerShell 7"
)

# Defines every top-level function of the script in the current scope, without
# running the script body (which would resolve signing tools and sign in).
LOAD_FUNCTIONS = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SIGN_SCRIPT, [ref]$null, [ref]$null)
$definitions = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
"""

# A stand-in for the GitHub CLI: answers `gh release view` with the next JSON
# document from SIGN_GH_RESPONSES, so a test can change state between calls.
FAKE_GH = r"""
$script:ghResponses = [System.Collections.Generic.Queue[string]]::new()
foreach ($item in ($env:SIGN_GH_RESPONSES | ConvertFrom-Json)) { $script:ghResponses.Enqueue(($item | ConvertTo-Json -Compress)) }
function gh {
    if ($args[0] -ne 'release' -or $args[1] -ne 'view') { throw "unexpected gh call: $args" }
    $global:LASTEXITCODE = 0
    return $script:ghResponses.Dequeue()
}
"""


def _run(body: str, *, responses: list[dict[str, object]] | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SIGN_SCRIPT": str(SCRIPT),
        "SIGN_GH_RESPONSES": json.dumps(responses or []),
    }
    command = LOAD_FUNCTIONS + FAKE_GH + body
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_existing_work_directory_with_files_is_refused_and_left_intact(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    sentinel = work / "keep-me.txt"
    sentinel.write_text("caller data", encoding="utf-8")

    result = _run(f"New-SigningWorkDirectory '{work}' | Out-Null")

    assert result.returncode != 0
    assert "is not empty" in result.stderr + result.stdout
    assert sentinel.read_text(encoding="utf-8") == "caller data"


def test_new_or_empty_work_directory_is_accepted(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    fresh = tmp_path / "fresh"

    result = _run(
        f"(New-SigningWorkDirectory '{empty}').FullName; "
        f"(New-SigningWorkDirectory '{fresh}').FullName"
    )

    assert result.returncode == 0, result.stderr
    assert fresh.is_dir()
    assert empty.is_dir()


def test_work_directory_that_is_a_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    path.write_text("x", encoding="utf-8")

    result = _run(f"New-SigningWorkDirectory '{path}' | Out-Null")

    assert result.returncode != 0
    assert path.read_text(encoding="utf-8") == "x"


def test_published_release_is_refused_before_signing() -> None:
    result = _run(
        "Get-DraftRelease 'v9.9.9' 'owner/repo' | Out-Null",
        responses=[{"databaseId": 7, "isDraft": False, "tagName": "v9.9.9"}],
    )

    assert result.returncode != 0
    assert "is not a draft" in result.stderr + result.stdout


def test_draft_release_is_returned_with_its_identity() -> None:
    result = _run(
        "(Get-DraftRelease 'v9.9.9' 'owner/repo').databaseId",
        responses=[{"databaseId": 7, "isDraft": True, "tagName": "v9.9.9"}],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"


def test_release_published_during_signing_blocks_upload() -> None:
    result = _run(
        "$release = Get-DraftRelease 'v9.9.9' 'owner/repo'; "
        "Assert-SameDraftRelease $release 'v9.9.9' 'owner/repo'",
        responses=[
            {"databaseId": 7, "isDraft": True, "tagName": "v9.9.9"},
            {"databaseId": 7, "isDraft": False, "tagName": "v9.9.9"},
        ],
    )

    assert result.returncode != 0
    assert "is not a draft" in result.stderr + result.stdout


def test_tag_moved_to_another_release_blocks_upload() -> None:
    result = _run(
        "$release = Get-DraftRelease 'v9.9.9' 'owner/repo'; "
        "Assert-SameDraftRelease $release 'v9.9.9' 'owner/repo'",
        responses=[
            {"databaseId": 7, "isDraft": True, "tagName": "v9.9.9"},
            {"databaseId": 8, "isDraft": True, "tagName": "v9.9.9"},
        ],
    )

    assert result.returncode != 0
    assert "different release" in result.stderr + result.stdout


def test_script_never_clears_a_directory_and_checks_draft_before_download() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "Remove-Item" not in text
    assert text.index("$release = Get-DraftRelease") < text.index("gh release download")
    assert text.index("Assert-SameDraftRelease $release") < text.index("gh release upload")
