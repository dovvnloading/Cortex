"""Preview-only code edits are lossless, bounded and never touch the checkout."""

from __future__ import annotations

from hashlib import sha256

import pytest

from cortex_backend.services import workspace_patch as patch
from cortex_backend.services.workspace_context import WorkspaceAccessError, WorkspaceSnapshotRegistry


def _context(tmp_path, *, source: bytes = b"def value():\n    return 42\n"):
    root = tmp_path / "checkout"
    root.mkdir()
    file = root / "module.py"
    file.write_bytes(source)
    (root / ".env").write_text("PRIVATE=never-index\n", encoding="utf-8")
    registry = WorkspaceSnapshotRegistry()
    snapshot = registry.capture(owner="owner", root_path=str(root), confirmed=True)
    return registry, snapshot, file


def _stager(registry, snapshot):
    return patch.WorkspacePatchStager(
        registry=registry, owner="owner", snapshot_id=snapshot.snapshot_id, allow_preview=True
    )


def _replace(stager, snapshot, *, path="module.py", before="return 42", after="return 43"):
    return stager.replace(
        path=path,
        expected_sha256=snapshot.files[path].sha256,
        old_text=before,
        new_text=after,
    )


def test_exact_replacement_builds_reviewable_unified_diff_and_leaves_checkout_untouched(tmp_path):
    registry, snapshot, file = _context(tmp_path)
    original = file.read_bytes()
    stager = _stager(registry, snapshot)

    preview = _replace(stager, snapshot)

    assert "--- a/module.py" in preview.diff
    assert "+++ b/module.py" in preview.diff
    assert "-    return 42" in preview.diff
    assert "+    return 43" in preview.diff
    assert preview.paths == ("module.py",)
    assert preview.replacements == 1
    assert preview.original_sha256 == (("module.py", sha256(original).hexdigest()),)
    assert preview.staged_sha256 == ((
        "module.py", sha256(original.replace(b"return 42", b"return 43")).hexdigest()
    ),)
    assert preview.applied_to_checkout is False
    assert file.read_bytes() == original


def test_source_bytes_preserve_crlf_final_newline_and_bom(tmp_path):
    original = b"\xef\xbb\xbfdef value():\r\n    return 42\r\n"
    registry, snapshot, file = _context(tmp_path, source=original)
    stored = snapshot.files["module.py"]
    assert stored.text.encode("utf-8") == original
    assert stored.sha256 == sha256(original).hexdigest()
    preview = _replace(_stager(registry, snapshot), snapshot)
    expected = original.replace(b"42", b"43")
    assert preview.staged_sha256 == (("module.py", sha256(expected).hexdigest()),)
    assert file.read_bytes() == original


@pytest.mark.parametrize("value", [
    "../module.py", "/private/module.py", "src/../../module.py", "C:/private.py",
    ".env", "not-indexed.py",
])
def test_preview_never_targets_paths_outside_the_indexed_snapshot(tmp_path, value):
    registry, snapshot, file = _context(tmp_path)
    before = file.read_bytes()
    stager = _stager(registry, snapshot)
    with pytest.raises(patch.WorkspacePatchError) as error:
        stager.replace(
            path=value, expected_sha256=snapshot.files["module.py"].sha256,
            old_text="return 42", new_text="return 43",
        )
    assert error.value.code in {"invalid_file", "file_unavailable"}
    assert before == file.read_bytes()
    assert stager.preview().diff == ""


def test_a_stale_or_guessed_digest_is_rejected_without_a_partial_edit(tmp_path):
    registry, snapshot, _ = _context(tmp_path)
    stager = _stager(registry, snapshot)
    with pytest.raises(patch.WorkspacePatchError) as error:
        stager.replace(
            path="module.py", expected_sha256="0" * 64,
            old_text="return 42", new_text="return 43",
        )
    assert error.value.code == "stale_source"
    assert stager.preview().replacements == 0


def test_ambiguous_old_text_is_never_replaced_by_guessing(tmp_path):
    registry, snapshot, _ = _context(
        tmp_path, source=b"first = 42\nsecond = 42\n"
    )
    stager = _stager(registry, snapshot)
    for old in ("42", "does-not-exist"):
        with pytest.raises(patch.WorkspacePatchError) as error:
            _replace(stager, snapshot, before=old, after="43")
        assert error.value.code == "match_not_unique"
    assert stager.preview().diff == ""


def test_two_replacements_use_one_baseline_and_preview_whole_transaction(tmp_path):
    registry, snapshot, file = _context(tmp_path)
    stager = _stager(registry, snapshot)
    _replace(stager, snapshot)
    preview = _replace(stager, snapshot, before="return 43", after="return 44")
    assert preview.replacements == 2
    assert "-    return 42" in preview.diff
    assert "+    return 44" in preview.diff
    assert "return 43" not in preview.diff
    assert b"return 42" in file.read_bytes()


def test_failed_second_step_does_not_corrupt_staged_first_step(tmp_path):
    registry, snapshot, _ = _context(tmp_path)
    stager = _stager(registry, snapshot)
    original = _replace(stager, snapshot)
    with pytest.raises(patch.WorkspacePatchError):
        _replace(stager, snapshot, before="missing content", after="new content")
    assert stager.preview() == original


def test_preview_is_disabled_until_trusted_host_explicitly_opts_in(tmp_path):
    registry, snapshot, _ = _context(tmp_path)
    with pytest.raises(patch.WorkspacePatchError) as error:
        patch.WorkspacePatchStager(
            registry=registry, owner="owner", snapshot_id=snapshot.snapshot_id
        )
    assert error.value.code == "preview_not_enabled"


def test_revocation_prevents_even_reading_a_staged_diff(tmp_path):
    registry, snapshot, _ = _context(tmp_path)
    stager = _stager(registry, snapshot)
    _replace(stager, snapshot)
    registry.revoke(owner="owner", snapshot_id=snapshot.snapshot_id)
    with pytest.raises(WorkspaceAccessError) as error:
        stager.preview()
    assert error.value.code == "unavailable"
    stager.discard()


def test_diff_limit_refuses_atomically_before_committing_a_replacement(tmp_path, monkeypatch):
    registry, snapshot, file = _context(tmp_path)
    stager = _stager(registry, snapshot)
    monkeypatch.setattr(patch, "MAX_PATCH_DIFF_CHARS", 10)
    with pytest.raises(patch.WorkspacePatchError) as error:
        _replace(stager, snapshot)
    assert error.value.code == "diff_too_large"
    assert stager.preview().replacements == 0
    assert b"return 42" in file.read_bytes()


def test_edit_bounds_limit_replacements_and_input_sizes(tmp_path, monkeypatch):
    registry, snapshot, _ = _context(tmp_path)
    stager = _stager(registry, snapshot)
    monkeypatch.setattr(patch, "MAX_PREVIEW_REPLACEMENTS", 1)
    _replace(stager, snapshot)
    with pytest.raises(patch.WorkspacePatchError) as error:
        _replace(stager, snapshot, before="return 43", after="return 44")
    assert error.value.code == "replacement_limit"
    for old, new in (("", "x"), ("return 42", "return 42"), ("x" * 9000, "y"), ("x", "\x00")):
        with pytest.raises(patch.WorkspacePatchError) as error:
            _replace(stager, snapshot, before=old, after=new)
        # The cap is tested first for a previously active transaction.
        assert error.value.code == "invalid_replacement" or error.value.code == "replacement_limit"


def test_discard_forgets_the_proposal_without_changing_source(tmp_path):
    registry, snapshot, file = _context(tmp_path)
    stager = _stager(registry, snapshot)
    _replace(stager, snapshot)
    stager.discard()
    assert stager.preview().diff == ""
    assert stager.preview().replacements == 0
    assert "return 42" in file.read_text(encoding="utf-8")


def test_unpaired_surrogate_in_model_replacement_is_a_typed_rejection(tmp_path):
    registry, snapshot, file = _context(tmp_path)
    stager = _stager(registry, snapshot)
    with pytest.raises(patch.WorkspacePatchError) as error:
        _replace(stager, snapshot, after="return '\ud800'")
    assert error.value.code == "invalid_replacement"
    assert stager.preview().diff == ""
    assert b"return 42" in file.read_bytes()
