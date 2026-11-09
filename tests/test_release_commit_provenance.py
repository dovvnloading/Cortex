"""Guard release provenance against tag-only version checks and mutable ref builds."""

from pathlib import Path


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
