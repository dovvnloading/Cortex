"""In-memory source patch previews against an approved workspace snapshot.

No method writes to a checkout, touches a host path, launches a process, or
grants execution. Edits must target existing, explicitly indexed UTF-8 files
and carry the exact baseline SHA-256 the model saw in a read observation.
Replacement text must match exactly once. All changes are staged atomically
and rendered as a bounded unified diff for the user to review. This is the
first *proposal* step toward an eventual approval-gated edit/test workflow,
not an authorisation to apply changes on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
from threading import RLock

from .workspace_context import (
    MAX_WORKSPACE_FILE_BYTES,
    WorkspaceAccessError,
    WorkspaceSnapshot,
    WorkspaceSnapshotRegistry,
    _relative_file_name,
)


MAX_PREVIEW_FILES = 8
MAX_PREVIEW_REPLACEMENTS = 16
MAX_REPLACEMENT_TEXT_CHARS = 8000
MAX_PATCH_DIFF_CHARS = 60000


class WorkspacePatchError(RuntimeError):
    """Stable redacted error without content or host paths."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The proposed workspace patch could not be staged.")


@dataclass(frozen=True, slots=True)
class WorkspacePatchPreview:
    """A preview only; the original repository is never modified."""

    diff: str
    paths: tuple[str, ...]
    replacements: int
    original_sha256: tuple[tuple[str, str], ...]
    staged_sha256: tuple[tuple[str, str], ...]
    applied_to_checkout: bool = False


class WorkspacePatchStager:
    """Stage text-only, optimistic replacements in an immutable source copy."""

    def __init__(
        self,
        *,
        registry: WorkspaceSnapshotRegistry,
        owner: str,
        snapshot_id: str,
        allow_preview: bool = False,
    ) -> None:
        if allow_preview is not True:
            raise WorkspacePatchError("preview_not_enabled")
        # Check the grant before allocating a transaction, and on every
        # operation thereafter: revoked and expired grants never keep authority.
        registry.get(owner=owner, snapshot_id=snapshot_id)
        self._registry = registry
        self._owner = owner
        self._snapshot_id = snapshot_id
        self._lock = RLock()
        self._staged: dict[str, str] = {}
        self._replacements = 0

    def _snapshot(self) -> WorkspaceSnapshot:
        return self._registry.get(
            owner=self._owner,
            snapshot_id=self._snapshot_id,
        )

    @staticmethod
    def _render_diff(snapshot: WorkspaceSnapshot, staged: dict[str, str]) -> str:
        parts: list[str] = []
        size = 0
        for path in sorted(staged):
            original = snapshot.files[path].text
            modified = staged[path]
            if original == modified:
                continue
            lines = difflib.unified_diff(
                original.splitlines(keepends=True),
                modified.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
            # difflib leaves a changed final line without its newline. A raw
            # join would run the next +/- line into it and produce a malformed
            # patch. Emit the standard no-newline marker explicitly.
            chunk = "".join(
                line if line.endswith(("\n", "\r"))
                else line + "\n\\ No newline at end of file\n"
                for line in lines
            )
            size += len(chunk)
            if size > MAX_PATCH_DIFF_CHARS:
                raise WorkspacePatchError("diff_too_large")
            parts.append(chunk)
        return "".join(parts)

    def replace(
        self,
        *,
        path: str,
        expected_sha256: str,
        old_text: str,
        new_text: str,
    ) -> WorkspacePatchPreview:
        """Apply one exact match to the *in-memory* stage and review the diff."""
        with self._lock:
            snapshot = self._snapshot()
            try:
                relative = _relative_file_name(path)
            except WorkspaceAccessError as exc:
                raise WorkspacePatchError(exc.code) from None
            file = snapshot.files.get(relative)
            if file is None:
                raise WorkspacePatchError("file_unavailable")
            if (
                not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
                or expected_sha256 != file.sha256
            ):
                raise WorkspacePatchError("stale_source")
            if (
                not isinstance(old_text, str)
                or not isinstance(new_text, str)
                or not old_text
                or old_text == new_text
                or len(old_text) > MAX_REPLACEMENT_TEXT_CHARS
                or len(new_text) > MAX_REPLACEMENT_TEXT_CHARS
                or "\x00" in old_text
                or "\x00" in new_text
            ):
                raise WorkspacePatchError("invalid_replacement")
            if self._replacements >= MAX_PREVIEW_REPLACEMENTS:
                raise WorkspacePatchError("replacement_limit")
            if relative not in self._staged and len(self._staged) >= MAX_PREVIEW_FILES:
                raise WorkspacePatchError("file_limit")

            before = self._staged.get(relative, file.text)
            if before.count(old_text) != 1:
                # Reject zero matches and ambiguous repeats alike: guessing
                # which occurrence to edit is not acceptable for a coding agent.
                raise WorkspacePatchError("match_not_unique")
            after = before.replace(old_text, new_text, 1)
            try:
                encoded = after.encode("utf-8")
            except UnicodeError:
                # A lone surrogate from malformed model JSON is not UTF-8.
                # Keep it out of the stage and report a stable rejection.
                raise WorkspacePatchError("invalid_replacement") from None
            if len(encoded) > MAX_WORKSPACE_FILE_BYTES:
                raise WorkspacePatchError("file_too_large")

            candidate = {**self._staged, relative: after}
            # Validate the entire transaction before committing this step.
            # A failed operation never leaves half of a proposed patch staged.
            self._render_diff(snapshot, candidate)
            self._staged = candidate
            self._replacements += 1
            return self.preview()

    def preview(self) -> WorkspacePatchPreview:
        with self._lock:
            snapshot = self._snapshot()
            paths = tuple(sorted(self._staged))
            return WorkspacePatchPreview(
                diff=self._render_diff(snapshot, self._staged),
                paths=paths,
                replacements=self._replacements,
                original_sha256=tuple(
                    (path, snapshot.files[path].sha256) for path in paths
                ),
                staged_sha256=tuple(
                    (path, hashlib.sha256(self._staged[path].encode("utf-8")).hexdigest())
                    for path in paths
                ),
            )

    def files_for_application(self) -> tuple[WorkspaceSnapshot, dict[str, str]]:
        """Verified snapshot and independent staged content for a trusted host.

        The model never receives this method or a host path. The application
        layer must separately seek explicit write approval and revalidate
        original file bytes immediately before any filesystem operation.
        """
        with self._lock:
            return self._snapshot(), dict(self._staged)

    def discard(self) -> None:
        """Forget all proposed changes, including after revoke or expiry."""
        with self._lock:
            self._staged.clear()
            self._replacements = 0
