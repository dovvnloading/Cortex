"""Approval-gated, ephemeral coding workspace on top of immutable source snapshots.

A model may only inspect a bounded snapshot and propose edits in memory. Actual
writes require a separate user confirmation, exact baseline-hash rechecks, and
only target files originally indexed. Commands are fixed choices and require
another explicit confirmation. Commands execute trusted project code with the
user's OS permissions; Windows Job Objects limit lifetime/resources, NOT file
or network access. Treat the workspace as trusted.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
from threading import Event, RLock, Timer
import time
from typing import Any

from cortex_backend.core.win_jobs import JobLimits, JobObjectError, KillOnCloseJob

from .workspace_agent import WorkspaceReadAgent
from .workspace_context import (
    WorkspaceAccessError,
    WorkspaceSnapshotRegistry,
    _is_reparse,
    _read_regular_source,
    _relative_file_name,
    _safe_root,
)
from .workspace_patch import WorkspacePatchStager

MAX_CHECK_SECONDS = 120
MAX_CHECK_OUTPUT_BYTES = 32768
MAX_CHECK_TAIL_BYTES = 8192
CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "pytest": ("python", "-m", "pytest", "-q"),
    "npm-test": ("npm", "test", "--", "--run"),
    "npm-build": ("npm", "run", "build"),
}


class WorkspaceWorkflowError(RuntimeError):
    """Path-free, safe-to-return failure category."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("The coding workspace operation could not be completed.")


@dataclass(slots=True)
class AppliedFile:
    original: bytes
    modified_sha256: str


@dataclass(slots=True)
class CodingSession:
    owner: str
    snapshot_id: str
    root: Path
    root_identity: tuple[int, int]
    state: str = "open"
    stager: WorkspacePatchStager | None = None
    applied: dict[str, AppliedFile] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)
    cancel_research: Event = field(default_factory=Event)
    cancel_check: Event = field(default_factory=Event)
    check_process: subprocess.Popen[bytes] | None = None
    check_job: KillOnCloseJob | None = None


class WorkspaceCodingSessions:
    """Owner-scoped leases and checked edit/test actions; nothing persists."""

    def __init__(self, *, snapshots: WorkspaceSnapshotRegistry | None = None) -> None:
        self.snapshots = snapshots or WorkspaceSnapshotRegistry()
        self._lock = RLock()
        self._write_lock = RLock()
        self._sessions: dict[str, CodingSession] = {}

    def open(self, *, owner: str, root_path: str, confirmed: bool) -> dict[str, Any]:
        snapshot = self.snapshots.capture(owner=owner, root_path=root_path, confirmed=confirmed)
        root = _safe_root(root_path)
        info = root.lstat()
        session = CodingSession(
            owner=owner, snapshot_id=snapshot.snapshot_id, root=root,
            root_identity=(info.st_dev, info.st_ino),
        )
        with self._lock:
            self._prune_locked()
            self._sessions[snapshot.snapshot_id] = session
        return {
            "session_id": snapshot.snapshot_id,
            "root": str(root),
            "file_count": snapshot.file_count,
            "total_bytes": snapshot.total_bytes,
            "truncated": snapshot.truncated,
            "skipped_entries": snapshot.skipped_entries,
            "expires_at": snapshot.expires_at.isoformat(),
            "state": session.state,
        }

    def refresh(self, *, owner: str, session_id: str, confirmed: bool) -> dict[str, Any]:
        """Reindex live source after an approved edit without dropping undo.

        The next model turn must see the *new* checkout, not the obsolete
        snapshot from before the last patch. An explicit reread renews the
        lease; every previously applied file must still match its last
        approved bytes, so undo remains trustworthy across rounds.
        """
        if confirmed is not True:
            raise WorkspaceWorkflowError("consent_required")
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        try:
            if session.state == "partial":
                raise WorkspaceWorkflowError("restore_required")
            if session.state not in {"open", "applied", "undone"}:
                raise WorkspaceWorkflowError("reopen_required")
            with self._write_lock:
                self._verified_root(session)
                for path, change in sorted(session.applied.items()):
                    self._verified_target(session, path, change.modified_sha256)
                snapshot = self.snapshots.refresh(
                    owner=owner, snapshot_id=session_id,
                    root_path=str(session.root), confirmed=True,
                )
                # A staged preview belongs to the previous immutable copy.
                # It never survives an explicitly requested refresh.
                if session.stager is not None:
                    session.stager.discard()
                session.stager = None
                session.state = "open"
                return {
                    "session_id": session_id,
                    "root": str(session.root),
                    "file_count": snapshot.file_count,
                    "total_bytes": snapshot.total_bytes,
                    "truncated": snapshot.truncated,
                    "skipped_entries": snapshot.skipped_entries,
                    "expires_at": snapshot.expires_at.isoformat(),
                    "state": session.state,
                    "undo_available": bool(session.applied),
                }
        finally:
            session.lock.release()

    def _prune_locked(self) -> None:
        for key, session in list(self._sessions.items()):
            try:
                self.snapshots.get(owner=session.owner, snapshot_id=key)
            except WorkspaceAccessError:
                session.cancel_research.set()
                session.cancel_check.set()
                self._stop_check(session)
                self._sessions.pop(key, None)

    def _get(self, *, owner: str, session_id: str) -> CodingSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or session.owner != owner:
            raise WorkspaceWorkflowError("unavailable")
        try:
            self.snapshots.get(owner=owner, snapshot_id=session_id)
        except WorkspaceAccessError as exc:
            raise WorkspaceWorkflowError("unavailable") from exc
        return session

    def paths(self, *, owner: str, session_id: str) -> tuple[str, ...]:
        self._get(owner=owner, session_id=session_id)
        return self.snapshots.list_paths(owner=owner, snapshot_id=session_id)

    def search(self, *, owner: str, session_id: str, query: str) -> list[dict[str, Any]]:
        self._get(owner=owner, session_id=session_id)
        return [
            asdict(hit)
            for hit in self.snapshots.search(owner=owner, snapshot_id=session_id, query=query)
        ]

    def read(
        self, *, owner: str, session_id: str, path: str, start_line: int = 1
    ) -> dict[str, Any]:
        self._get(owner=owner, session_id=session_id)
        return asdict(self.snapshots.read(
            owner=owner, snapshot_id=session_id, path=path, start_line=start_line
        ))

    def ask(
        self, *, owner: str, session_id: str, question: str, model: str,
        chat_client: Any, preview_edits: bool = False,
    ) -> dict[str, Any]:
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        try:
            if session.state != "open":
                raise WorkspaceWorkflowError("reopen_required")
            session.cancel_research.clear()
            # Each research turn replaces, rather than silently accumulates,
            # the previous proposed patch. An incomplete turn saves no stage.
            session.stager = None
            stager = (
                WorkspacePatchStager(
                    registry=self.snapshots, owner=owner, snapshot_id=session_id,
                    allow_preview=True,
                )
                if preview_edits else None
            )
            result = WorkspaceReadAgent(
                registry=self.snapshots, chat_client=chat_client, model=model,
                allow_patch_preview=preview_edits,
            ).run(
                owner=owner, snapshot_id=session_id, question=question,
                cancellation_event=session.cancel_research,
                patch_stager=stager,
            )
            if (
                result.status == "completed" and result.preview_diff
                and stager is not None
            ):
                session.stager = stager
            elif stager is not None:
                stager.discard()
            return asdict(result)
        finally:
            session.lock.release()

    def replace(
        self, *, owner: str, session_id: str, path: str,
        expected_sha256: str, old_text: str, new_text: str,
    ) -> dict[str, Any]:
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        try:
            if session.state != "open":
                raise WorkspaceWorkflowError("reopen_required")
            if session.stager is None:
                session.stager = WorkspacePatchStager(
                    registry=self.snapshots, owner=owner, snapshot_id=session_id,
                    allow_preview=True,
                )
            return asdict(session.stager.replace(
                path=path, expected_sha256=expected_sha256,
                old_text=old_text, new_text=new_text,
            ))
        finally:
            session.lock.release()

    def preview(self, *, owner: str, session_id: str) -> dict[str, Any]:
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        try:
            if session.stager is None:
                return {"diff": "", "paths": [], "replacements": 0, "applied_to_checkout": False}
            return asdict(session.stager.preview())
        finally:
            session.lock.release()

    @staticmethod
    def _verified_target(
        session: CodingSession, relative: str, digest: str,
    ) -> tuple[Path, os.stat_result, bytes]:
        """Reject linked ancestors, replaced roots and changed target bytes."""
        try:
            root = _safe_root(str(session.root))
            identity = root.lstat()
            if (identity.st_dev, identity.st_ino) != session.root_identity:
                raise WorkspaceWorkflowError("stale_root")
            name = _relative_file_name(relative)
            target = root
            parts = name.split("/")
            for part in parts[:-1]:
                target = target / part
                info = target.lstat()
                if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
                    raise WorkspaceWorkflowError("linked_path")
            target = target / parts[-1]
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                raise WorkspaceWorkflowError("linked_path")
            original = _read_regular_source(target, info)
            if original is None or sha256(original).hexdigest() != digest:
                raise WorkspaceWorkflowError("stale_source")
            return target, info, original
        except (OSError, ValueError, WorkspaceAccessError) as exc:
            raise WorkspaceWorkflowError("stale_source") from exc

    @staticmethod
    def _atomic_replace(target: Path, data: bytes, mode: int) -> None:
        """Write a sibling then replace; no partially-written source is exposed."""
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".cortex-edit-", dir=str(target.parent))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, stat.S_IMODE(mode))
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                with suppress(OSError):
                    os.unlink(temporary)

    def apply(self, *, owner: str, session_id: str, confirmed: bool) -> dict[str, Any]:
        if confirmed is not True:
            raise WorkspaceWorkflowError("consent_required")
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        try:
            if session.state != "open" or session.stager is None:
                raise WorkspaceWorkflowError("no_patch")
            snapshot, staged = session.stager.files_for_application()
            if not staged or not session.stager.preview().diff:
                raise WorkspaceWorkflowError("no_patch")
            with self._write_lock:
                # Keep the pre-first-edit bytes for undo, even when a later
                # approved patch edits the same file again.
                previous = dict(session.applied)
                for path, change in sorted(previous.items()):
                    self._verified_target(session, path, change.modified_sha256)
                baselines: dict[str, tuple[bytes, int]] = {}
                for path in sorted(staged):
                    source = snapshot.files[path]
                    _, info, raw = self._verified_target(session, path, source.sha256)
                    baselines[path] = (raw, info.st_mode)
                # This batch's rollback restores the state before this
                # particular approval, not the state before the first patch.
                batch: dict[str, AppliedFile] = {}

                def accumulated() -> dict[str, AppliedFile]:
                    result = dict(previous)
                    for path, change in batch.items():
                        first_original = (
                            previous[path].original if path in previous else change.original
                        )
                        result[path] = AppliedFile(
                            original=first_original, modified_sha256=change.modified_sha256,
                        )
                    return result

                try:
                    for path in sorted(staged):
                        source = snapshot.files[path]
                        target, info, _ = self._verified_target(session, path, source.sha256)
                        content = staged[path].encode("utf-8")
                        self._atomic_replace(target, content, info.st_mode)
                        batch[path] = AppliedFile(
                            original=baselines[path][0], modified_sha256=sha256(content).hexdigest(),
                        )
                except (OSError, WorkspaceWorkflowError):
                    # A failed multi-file batch is rolled back to the previous
                    # approved revision, retaining its earlier undo checkpoint.
                    # Refuse further edits if even rollback cannot finish.
                    for path, change in reversed(list(batch.items())):
                        try:
                            target, info, _ = self._verified_target(
                                session, path, change.modified_sha256
                            )
                            self._atomic_replace(target, change.original, info.st_mode)
                        except (OSError, WorkspaceWorkflowError):
                            continue
                        del batch[path]
                    if batch:
                        session.applied = accumulated()
                        session.state = "partial"
                        raise WorkspaceWorkflowError("partial_rollback") from None
                    raise WorkspaceWorkflowError("write_failed") from None
                session.applied = accumulated()
                session.state = "applied"
                session.stager.discard()
                session.stager = None
                return {"status": "applied", "paths": sorted(batch), "undo_available": True}
        finally:
            session.lock.release()

    def undo(self, *, owner: str, session_id: str, confirmed: bool) -> dict[str, Any]:
        if confirmed is not True:
            raise WorkspaceWorkflowError("consent_required")
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        try:
            if session.state not in {"open", "applied", "partial"} or not session.applied:
                raise WorkspaceWorkflowError("no_checkpoint")
            with self._write_lock:
                # Verify every target first, before restoring even one.
                for path, change in sorted(session.applied.items()):
                    self._verified_target(session, path, change.modified_sha256)
                restored: list[str] = []
                for path, change in sorted(session.applied.items()):
                    try:
                        target, info, _ = self._verified_target(
                            session, path, change.modified_sha256
                        )
                        self._atomic_replace(target, change.original, info.st_mode)
                    except (OSError, WorkspaceWorkflowError):
                        for done in restored:
                            session.applied.pop(done, None)
                        raise WorkspaceWorkflowError("restore_failed") from None
                    restored.append(path)
                session.applied.clear()
                if session.stager is not None:
                    session.stager.discard()
                session.stager = None
                session.state = "undone"
                return {"status": "undone", "paths": restored, "undo_available": False}
        finally:
            session.lock.release()

    @staticmethod
    def _stop_check(session: CodingSession) -> None:
        process = session.check_process
        if process is None or process.poll() is not None:
            return
        if session.check_job is not None:
            session.check_job.close()
        elif os.name != "nt":
            # Windows stubs do not export POSIX-only killpg/SIGKILL, even
            # though this branch only executes on a POSIX host.
            kill_group = getattr(os, "killpg", None)
            kill_signal = getattr(signal, "SIGKILL", None)
            if callable(kill_group) and kill_signal is not None:
                with suppress(OSError):
                    kill_group(process.pid, kill_signal)
        with suppress(OSError):
            process.kill()

    def cancel(self, *, owner: str, session_id: str) -> dict[str, bool]:
        session = self._get(owner=owner, session_id=session_id)
        session.cancel_research.set()
        session.cancel_check.set()
        active = session.check_process is not None and session.check_process.poll() is None
        if active:
            self._stop_check(session)
        return {"cancelled": active}

    def check(
        self, *, owner: str, session_id: str, command: str, confirmed: bool,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise WorkspaceWorkflowError("consent_required")
        if command not in CHECK_COMMANDS:
            raise WorkspaceWorkflowError("invalid_command")
        session = self._get(owner=owner, session_id=session_id)
        if not session.lock.acquire(blocking=False):
            raise WorkspaceWorkflowError("busy")
        timer: Timer | None = None
        timed_out = Event()
        started = time.monotonic()
        try:
            # A test or build can execute arbitrary checked-out project code
            # with the user's account: the choice is fixed but not sandboxed.
            self._verified_root(session)
            args = CHECK_COMMANDS[command]
            binary = "npm.cmd" if os.name == "nt" and args[0] == "npm" else args[0]
            executable = shutil.which(binary)
            if not executable:
                raise WorkspaceWorkflowError("runner_unavailable")
            env = {
                key: value for key, value in os.environ.items()
                if key in {"SystemRoot", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA"}
            }
            env.update({"CI": "1", "NO_COLOR": "1", "PYTHONUTF8": "1"})
            session.cancel_check.clear()
            try:
                process = subprocess.Popen(
                    (executable, *args[1:]), cwd=str(session.root),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, shell=False,
                    start_new_session=(os.name != "nt"),
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                )
            except OSError as exc:
                raise WorkspaceWorkflowError("runner_unavailable") from exc
            session.check_process = process
            if os.name == "nt":
                try:
                    session.check_job = KillOnCloseJob(JobLimits(
                        job_memory_bytes=2 * 1024**3, active_processes=32, restrict_ui=True,
                    ))
                    session.check_job.assign(process.pid)
                except (JobObjectError, OSError) as exc:
                    self._stop_check(session)
                    raise WorkspaceWorkflowError("containment_unavailable") from exc

            def expire() -> None:
                timed_out.set()
                self._stop_check(session)

            timer = Timer(MAX_CHECK_SECONDS, expire)
            timer.daemon = True
            timer.start()
            total = 0
            head = bytearray()
            tail = bytearray()
            assert process.stdout is not None
            with process.stdout as output:
                while True:
                    chunk = output.read(4096)
                    if not chunk:
                        break
                    total += len(chunk)
                    if len(head) < MAX_CHECK_OUTPUT_BYTES - MAX_CHECK_TAIL_BYTES:
                        head.extend(chunk[:MAX_CHECK_OUTPUT_BYTES - MAX_CHECK_TAIL_BYTES - len(head)])
                    tail.extend(chunk)
                    if len(tail) > MAX_CHECK_TAIL_BYTES:
                        del tail[:-MAX_CHECK_TAIL_BYTES]
            code = process.wait()
            clipped = total > MAX_CHECK_OUTPUT_BYTES
            blob = bytes(head) + (b"\n...[output truncated]...\n" if clipped else b"") + (
                bytes(tail) if clipped else bytes(tail)[max(0, len(head) + len(tail) - total):]
            )
            # Without truncation the head and tail may overlap. The last
            # expression above adds only bytes not already stored in head.
            result_status = (
                "cancelled" if session.cancel_check.is_set() else
                "timed_out" if timed_out.is_set() else
                "passed" if code == 0 else "failed"
            )
            return {
                "status": result_status,
                "command": command,
                "exit_code": code,
                "output": blob.decode("utf-8", errors="replace"),
                "output_truncated": clipped,
                "duration_seconds": round(time.monotonic() - started, 2),
            }
        finally:
            if timer is not None:
                timer.cancel()
            self._stop_check(session)
            if session.check_job is not None:
                session.check_job.close()
                session.check_job = None
            session.check_process = None
            session.lock.release()

    @staticmethod
    def _verified_root(session: CodingSession) -> None:
        try:
            root = _safe_root(str(session.root))
            info = root.lstat()
            if (info.st_dev, info.st_ino) != session.root_identity:
                raise WorkspaceWorkflowError("stale_root")
        except (OSError, WorkspaceAccessError) as exc:
            raise WorkspaceWorkflowError("stale_root") from exc

    def revoke(self, *, owner: str, session_id: str) -> None:
        session = self._get(owner=owner, session_id=session_id)
        # Interrupt a blocking research/check call before waiting for the
        # session lock. Once revoke returns, a concurrent apply/undo must
        # have finished: otherwise a closed workspace could keep writing.
        session.cancel_research.set()
        session.cancel_check.set()
        self._stop_check(session)
        with session.lock:
            with self._lock:
                if self._sessions.get(session_id) is not session:
                    raise WorkspaceWorkflowError("unavailable")
                self._sessions.pop(session_id, None)
            self.snapshots.revoke(owner=owner, snapshot_id=session_id)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.cancel_research.set()
            session.cancel_check.set()
            self._stop_check(session)
        # A shutdown may race an approved on-disk edit. Wait for its
        # session-level critical section to finish before clearing leases.
        for session in sessions:
            with session.lock:
                pass
        self.snapshots.close()
