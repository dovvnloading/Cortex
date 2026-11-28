"""Guard release provenance against tag-only version checks and mutable ref builds."""

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def test_release_tag_must_be_ancestor_of_main_and_have_completed_exact_sha_gates() -> None:
    workflow = RELEASE.read_text(encoding="utf-8")
    verify, package = workflow.split("\n  package:\n", maxsplit=1)

    assert "fetch-depth: 0" in verify
    assert "git fetch --no-tags origin main" in verify
    assert "git merge-base --is-ancestor $sha origin/main" in verify
    assert "throw \"Release tags must point at a commit reachable from main.\"" in verify
    assert '@("quality.yml", "python-compatibility.yml")' in verify
    assert "head_sha=$sha" in verify
    assert '$_.head_sha -eq $sha' in verify
    assert '$_.event -eq "push"' in verify
    assert '$_.status -eq "completed"' in verify
    assert '$_.conclusion -eq "success"' in verify
    assert '$_.name -eq "CodeQL"' in verify
    assert "Cannot inspect code scanning" in verify

    # The package MUST use the proven SHA, never re-resolve a mutable tag
    # that could be moved between the check and artifact build jobs.
    assert 'sha: ${{ steps.proven.outputs.sha }}' in verify
    assert '"sha=$sha" >> $env:GITHUB_OUTPUT' in verify
    assert 'ref: ${{ needs.verify-version.outputs.sha }}' in package
    assert 'ref: ${{ github.event.inputs.tag || github.ref }}' not in package


def test_release_security_verification_is_read_only_and_fails_closed() -> None:
    workflow = RELEASE.read_text(encoding="utf-8")
    verify, package = workflow.split("\n  package:\n", maxsplit=1)
    assert "  actions: read" in verify
    assert "  checks: read" in verify
    assert "GH_TOKEN: ${{ github.token }}" in verify
    assert "no successful completed main push" in verify
    assert "must pass CodeQL before it may be packaged" in verify
    assert "contents: write" in package  # only the artifact-upload job may write


def test_release_checks_out_and_verifies_the_tag_itself() -> None:
    workflow = RELEASE.read_text(encoding="utf-8")
    verify, _package = workflow.split("\n  package:\n", maxsplit=1)

    # A dispatch input is resolved as a tag ref, never as a same-named branch.
    assert "format('refs/tags/{0}', github.event.inputs.tag)" in verify
    assert 'git rev-parse --verify --quiet "refs/tags/$($env:RELEASE_TAG)^{commit}"' in verify
    assert verify.index("^{commit}") < verify.index('"sha=$sha" >> $env:GITHUB_OUTPUT')


# --- the draft-release step, run for real against a fake gh (issue #715) ------


def _draft_step_script() -> str:
    lines = RELEASE.read_text(encoding="utf-8").splitlines()
    start = lines.index("      - name: Create a draft release")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


# Answers `gh api` from FAKE_GH_OBJECTS (path -> git object) and records
# `gh release create` arguments, so the step can be run without GitHub.
FAKE_GH = r"""
$objects = $env:FAKE_GH_OBJECTS | ConvertFrom-Json -AsHashtable
function gh {
    if ($args[0] -eq 'api') {
        $path = $args[1]
        if ($objects.ContainsKey($path)) {
            $global:LASTEXITCODE = 0
            return (@{ object = $objects[$path] } | ConvertTo-Json -Compress)
        }
        $global:LASTEXITCODE = 1
        return '{"message":"Not Found"}'
    }
    if ($args[0] -eq 'release' -and $args[1] -eq 'create') {
        Set-Content -LiteralPath $env:FAKE_GH_CREATED -Value ($args -join ' ')
        $global:LASTEXITCODE = 0
        return
    }
    throw "unexpected gh call: $args"
}
"""

VERIFIED = "a" * 40


def _run_draft_step(tmp_path: Path, objects: dict[str, dict[str, str]]) -> tuple[subprocess.CompletedProcess[str], str | None]:
    created = tmp_path / "created.txt"
    env = {
        **os.environ,
        "FAKE_GH_OBJECTS": json.dumps(objects),
        "FAKE_GH_CREATED": str(created),
        "RELEASE_TAG": "v9.9.9",
        "VERIFIED_SHA": VERIFIED,
        "GITHUB_REPOSITORY": "owner/repo",
        "ARCHIVE": "Cortex-9.9.9-windows-x64.zip",
    }
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", FAKE_GH + _draft_step_script()],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    return result, created.read_text(encoding="utf-8") if created.exists() else None


needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="requires PowerShell 7")
REF = "repos/owner/repo/git/ref/tags/v9.9.9"


@needs_pwsh
def test_draft_is_created_only_for_the_verified_tag(tmp_path: Path) -> None:
    result, created = _run_draft_step(tmp_path, {REF: {"type": "commit", "sha": VERIFIED}})

    assert result.returncode == 0, result.stderr
    assert created is not None
    assert created.startswith("release create v9.9.9 ")
    assert "--verify-tag" in created and "--draft" in created


@needs_pwsh
def test_an_annotated_tag_is_peeled_to_its_commit(tmp_path: Path) -> None:
    objects = {
        REF: {"type": "tag", "sha": "t" * 40},
        "repos/owner/repo/git/tags/" + "t" * 40: {"type": "commit", "sha": VERIFIED},
    }
    result, created = _run_draft_step(tmp_path, objects)

    assert result.returncode == 0, result.stderr
    assert created is not None


@needs_pwsh
def test_a_moved_tag_creates_no_release(tmp_path: Path) -> None:
    result, created = _run_draft_step(tmp_path, {REF: {"type": "commit", "sha": "b" * 40}})

    assert result.returncode != 0
    assert "no longer points at the verified commit" in result.stderr + result.stdout
    assert created is None


@needs_pwsh
def test_a_deleted_tag_creates_no_release(tmp_path: Path) -> None:
    result, created = _run_draft_step(tmp_path, {})

    assert result.returncode != 0
    assert created is None
