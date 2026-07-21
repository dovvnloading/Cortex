# ADR-0001 Phase 2 trusted artifact boundary

- **Status:** Implemented and verified
- **Phase:** 2 - trusted user-artifact copy-in, output validation, and publication
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Related:** [Phase 2 typed recipe contract](0001-phase2-recipe-contract.md), [Phase 2 evidence log](0001-phase2-evidence.md)
- **Scope:** Boundary code only. No image codec, provider, sandbox, or automatic execution is enabled by this ADR.

## Context

The signed bundle installer protects trusted runtime bytes, but it must not be used
as the user-artifact transfer path. User-selected inputs and provider outputs have a
different trust model: paths are attacker-controlled, files can change during a read,
and a provider can return files that were not declared by the plan. A copy or
publication helper that trusts a filename, caller MIME type, or a resolved path can
create path traversal, link/reparse-point, time-of-check/time-of-use, active-content,
partial-publication, or source-overwrite failures.

Windows reparse points deliberately change ordinary file-operation behavior, so this
boundary treats symbolic links and junctions as untrusted path hops. The implementation
also uses exclusive temporary files and an atomic replacement for the repository commit;
the operating-system behavior and limitations are documented by Microsoft in
[Reparse Points and File Operations](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points-and-file-operations),
[Reparse Point Operations](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-point-operations),
and [MoveFileEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexa).

## Decision

`ArtifactBoundary` is the only Phase 2 API allowed to copy a user source into the
artifact store or publish provider output. It is owner-scoped, fail-closed, and
provider-independent.

### 1. Explicit source grants and immutable snapshots

Copy-in requires an `ArtifactSourceGrant(owner, job_id, source_turn_id, source_path)`.
The repository job owner is checked before the path is inspected; an owner mismatch
does not disclose the source path. The source path must be absolute and bounded, with
no NUL, alternate-data-stream component, link, junction, or other reparse component.

The source must be a regular, non-sparse file with exactly one directory entry
(hardlinks are rejected). Its device/inode, size, and timestamps are captured before
and after a bounded read. Any mutation, disappearance, oversize read, or non-regular
object aborts the transfer. The source is read-only from this boundary and is never
renamed, replaced, or deleted.

### 2. Byte-derived content policy

MIME is derived from bytes, never from an extension or model declaration. PNG, JPEG,
WebP, finite JSON, and ordinary UTF-8 text are recognized. Portable executables,
ELF/OLE files, shortcuts, shebang scripts, archives, active HTML/SVG/JavaScript,
and common shell/PowerShell launchers are rejected. Unknown non-active bytes may be
stored as `application/octet-stream`; this is metadata safety, not permission to decode
or execute them. Image decoding remains a later provider qualification gate.

### 3. Private output staging and exact claims

The provider receives a private staging directory and returns a finite list of
`OutputClaim(relative_path, mime_type?)`. Relative paths use a narrow forward-slash
grammar and cannot contain dot segments, backslashes, drive/ADS syntax, or reparse
components. The staged file set must equal the claim set exactly. Extra files are
moved into a randomized `.quarantine` directory and no output is published. Missing
claims, invalid paths, links, mutation, active/archive content, MIME mismatches,
per-file limits, output-count limits, and aggregate-size limits fail closed.

Every declared output is fully read, identity-checked, MIME-sniffed, and hashed before
the first output is published. This prevents a late invalid file from leaving a
partially visible result set. On validation failure, the offending staged file is
quarantined. On publication failure, already-published records are deleted and all
remaining staged files are quarantined; inability to complete cleanup reports the
stable `artifact_cleanup_pending` category for supervisor recovery.

### 4. Safe repository publication

The repository generates an opaque digest-derived artifact name. It writes bytes to
an exclusive temporary file, flushes and fsyncs it, atomically replaces the final
path, and inserts the SQLite record only after the file commit succeeds. Artifact
read, delete, and expiry purge re-check artifact-root confinement, regular-file/link
state, and the stored SHA-256. A database row can never authorize deletion or reading
of a path outside the configured artifact root.

### 5. Stable failure categories

The boundary exposes only categories such as `artifact_owner_mismatch`,
`artifact_path_invalid`, `artifact_reparse_point`, `artifact_hardlink_rejected`,
`artifact_source_changed`, `artifact_too_large`, `invalid_artifact`,
`artifact_unclaimed_output`, `artifact_mime_mismatch`, `artifact_output_limit`,
`artifact_publish_failed`, and `artifact_cleanup_pending`. Raw paths and operating
system details do not cross the application boundary.

## Verification and evidence

`tests/test_phase2_artifact_boundary.py` covers owner binding, source preservation,
ADS/link/reparse rejection, source mutation, exact claims, quarantine, active/archive
rejection, executable rejection, non-finite JSON rejection, MIME mismatch, aggregate
limits, and all-or-nothing publication rollback. The repository tests continue to
cover hash/size/retention behavior. The complete matrix is recorded in the
[Phase 2 evidence log](0001-phase2-evidence.md).

## Explicit non-goals

This ADR does not authorize image decoding, thumbnail generation, archive extraction,
provider loading, code execution, model tool exposure, network access, or automatic
execution. The next gate is fixed-function provider qualification inside the already
planned OS sandbox, followed by a lifecycle health check and external security review.
