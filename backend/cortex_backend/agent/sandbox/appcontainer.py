"""Per-run Windows low-box launcher and crash-recoverable AppContainer ACL ledger.

No token is ever granted modify access outside the work copy. Individual profile SIDs receive inheritable RX only on toolchain directories
whose ACLs this user can change. Directories already readable/executable by
ALL APPLICATION PACKAGES are left untouched. Stale journals are replayed on
the next launcher startup after an unclean exit.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any

from cortex_backend.agent.sandbox import win_acl
from cortex_backend.agent.sandbox.base import (
    ProcessHandle,
    RunningProcess,
    SandboxLevel,
    canonical_workcopy,
    child_environment,
    command_argv,
    job_limits,
    toolchain,
    within,
    working_directory,
)
from cortex_backend.core import win_appcontainer
from cortex_backend.core.win_jobs import JobLimits, KillOnCloseJob
from cortex_backend.core.win_paths import system32, system_directory

_LOG = logging.getLogger(__name__)
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_STARTUP_LOCK = threading.Lock()
_SWEEPED = False


def _journal_directory() -> Path:
    return Path.home() / "AppData" / "Local" / "Cortex" / "agent-sandbox"


def _on_windows() -> bool:
    return sys.platform == "win32"


@contextmanager
def _creation_gate() -> Iterator[None]:
    """Serialize journal creation and sweeps across Cortex processes."""
    import msvcrt

    directory = _journal_directory()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".sweep.lock").open("a+b") as file:
        if file.seek(0, 2) == 0:
            file.write(b" ")
            file.flush()
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)


def _acl(path: Path, sid: str, *, access: str | None) -> None:
    """Apply/revoke one explicit inheritable ACE for this run's unique SID."""
    if not path.exists():
        if access is None:
            return  # the deleted directory no longer has its ACE
        raise FileNotFoundError(path)
    if access is None:
        try:
            if not win_acl.has_explicit_sid_grant(path, sid):
                return  # /grant failed or this directory was already shared
        except OSError:
            # When the ACL cannot be inspected, still attempt revocation.
            _LOG.warning("Could not inspect AppContainer ACE on %s; attempting removal", path)
        args = [system32("icacls.exe"), str(path), "/remove:g", f"*{sid}"]
    else:
        args = [system32("icacls.exe"), str(path), "/grant", f"*{sid}:(OI)(CI){access}"]
    root = system_directory()
    if root is None:
        raise OSError("System32 unavailable")
    subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True,
        check=True, env={"SystemRoot": str(root.parent)}, creationflags=win_appcontainer.CREATE_NO_WINDOW,
    )


class _Journal:
    """An OS-released byte lock identifies a run still alive after a crash.

    The complete set of *planned* grants is fsynced before the first ACL write.
    A sweep thus knows exactly what to undo even if Cortex was killed halfway
    through an icacls operation. No content from this ledger reaches the child.
    """

    def __init__(self, name: str, sid: str, paths: Sequence[Path]):
        import msvcrt

        self.name = name
        self.sid = sid
        self.paths = tuple(paths)
        directory = _journal_directory()
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{name}.json"
        self._file = self.path.open("x+b")
        try:
            contents = json.dumps({"name": name, "sid": sid, "paths": [str(p) for p in paths]}).encode("utf-8")
            self._file.write(b" " + contents)
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
        except BaseException:
            self._file.close()
            self.path.unlink(missing_ok=True)
            raise

    def finish(self, *, delete_profile: bool, revoke: bool = True) -> None:
        """Try every revocation and profile deletion, retaining a failed journal."""
        try:
            _cleanup_profile(self.name, self.sid, self.paths, revoke=revoke, delete_profile=delete_profile)
        except Exception:
            raise  # preserve the journal for the next startup sweep
        else:
            self._file.close()
            self.path.unlink(missing_ok=True)
        finally:
            if not self._file.closed:
                self._file.close()


def _revoke(sid: str, paths: Sequence[Path]) -> None:
    errors: list[Exception] = []
    for path in reversed(paths):
        try:
            _acl(path, sid, access=None)
        except Exception as exc:
            # CalledProcessError is NOT an OSError; never stop at the first ACE.
            _LOG.error("Could not remove AppContainer ACE for %s on %s: %s", sid, path, exc)
            errors.append(exc)
    if errors:
        raise OSError(f"could not remove {len(errors)} of {len(paths)} agent AppContainer ACEs") from errors[0]


def _cleanup_profile(
    name: str, sid: str, paths: Sequence[Path], *, revoke: bool, delete_profile: bool,
) -> None:
    errors: list[tuple[str, Exception]] = []
    if revoke:
        try:
            _revoke(sid, paths)
        except Exception as exc:
            errors.append(("ACE removal", exc))
    if delete_profile:
        try:
            win_appcontainer.delete_profile(name)
        except Exception as exc:
            errors.append(("profile deletion", exc))
    if errors:
        details = "; ".join(f"{action}: {exc}" for action, exc in errors)
        _LOG.error("AppContainer cleanup failed for %s: %s", name, details)
        raise OSError(f"AppContainer cleanup failed for {name}: {details}") from errors[0][1]


def _read_stale(path: Path, raw: bytes | None = None) -> tuple[str, str, tuple[Path, ...]]:
    if path.is_symlink() or not path.name.startswith(win_appcontainer.PROFILE_PREFIX):
        raise ValueError("invalid AppContainer journal")
    if raw is None:
        with path.open("rb") as file:
            file.seek(1)
            raw = file.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid AppContainer journal")
    name, sid, paths = value.get("name"), value.get("sid"), value.get("paths")
    if (not isinstance(name, str) or not isinstance(sid, str) or
            not isinstance(paths, list) or name + ".json" != path.name or
            not _RUN_ID.fullmatch(name.removeprefix(win_appcontainer.PROFILE_PREFIX)) or
            not sid.startswith("S-1-15-2-") or not all(isinstance(p, str) for p in paths)):
        raise ValueError("invalid AppContainer journal")
    # No untrusted journal may ask to revoke the grants of another profile.
    if win_appcontainer.derive_profile_sid(name) != sid:
        raise ValueError("AppContainer journal SID does not match its profile")
    return name, sid, tuple(Path(p) for p in paths)


def sweep_stale_profiles() -> None:
    """Remove orphan ACEs before deleting orphan profiles, skipping live runs.

    An open journal byte is locked for the entire child lifetime. Windows
    releases it on an abrupt Cortex exit, unlike an application-level PID.
    """
    if not _on_windows():
        return
    with _creation_gate():
        _sweep_locked()


def _sweep_locked() -> None:
    import msvcrt

    directory = _journal_directory()
    directory.mkdir(parents=True, exist_ok=True)
    live: set[str] = set()
    for path in directory.glob(f"{win_appcontainer.PROFILE_PREFIX}*.json"):
        try:
            with path.open("r+b") as file:
                file.seek(0)
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    live.add(path.stem)
                    continue
                try:
                    file.seek(1)
                    name, sid, paths = _read_stale(path, file.read())
                    _cleanup_profile(name, sid, paths, revoke=True, delete_profile=True)
                finally:
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            _LOG.warning("Could not recover an AppContainer journal; preserving it for repair: %s", path.name)
            raise  # do not start a new run with known leaked permissions
    # An orphan created after profile creation but before the journal is
    # persisted has no ACLs yet; its profile may be deleted on its own.
    for name in win_appcontainer.list_profiles():
        if name not in live:
            win_appcontainer.delete_profile(name)


def _sweep_once() -> None:
    global _SWEEPED
    with _STARTUP_LOCK:
        if not _SWEEPED:
            sweep_stale_profiles()
            _SWEEPED = True


class AppContainerLauncher:
    """One-shot launcher; a unique profile and journal belong to one command."""

    level: SandboxLevel = "appcontainer"

    def __init__(self, workcopy: str | Path, run_id: str):
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must contain 1-64 ASCII letters, digits, hyphens or underscores")
        self.workcopy = canonical_workcopy(workcopy)
        self.name = win_appcontainer.PROFILE_PREFIX + run_id
        self._started = False

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: JobLimits | None = None,
    ) -> ProcessHandle:
        if self._started:
            raise RuntimeError("a per-run AppContainer launcher cannot be reused")
        if not _on_windows():
            raise OSError("AppContainer launching requires Windows")
        _sweep_once()
        directory = working_directory(cwd, self.workcopy)
        tools, folders = toolchain()
        args = command_argv(argv, self.workcopy, tools)  # reject escapes before creating a profile
        # env is deliberately ignored: the child receives *only* this allowlist.
        _ = env
        system = system_directory()
        grant_paths = [self.workcopy]
        unavailable: set[Path] = set()
        for folder in folders:
            if folder == system or within(folder, self.workcopy) or folder in grant_paths:
                continue
            try:
                policy = win_acl.tool_folder_policy(folder)
            except OSError as exc:
                _LOG.warning("Toolchain %s unavailable (cannot read ACL): %s", folder, exc)
                unavailable.add(folder)
                continue
            if policy == "grantable":
                grant_paths.append(folder)
            elif policy == "unavailable":
                _LOG.warning("Toolchain %s unavailable (cannot change its ACL)", folder)
                unavailable.add(folder)
            # "shared": ALL APPLICATION PACKAGES already has inheritable RX.
        sid = win_appcontainer.derive_profile_sid(self.name)
        with _creation_gate():
            journal = _Journal(self.name, sid, grant_paths)
        profile_created = False
        grants_attempted = False
        child: Any = None
        job: KillOnCloseJob | None = None
        self._started = True
        try:
            created_sid = win_appcontainer.create_profile(self.name)
            profile_created = True
            if created_sid != sid:
                raise OSError("created profile SID differs from the recorded SID")
            for i, path in enumerate(grant_paths):
                grants_attempted = True
                if i == 0:
                    _acl(path, sid, access="M")  # never run without a writable work copy
                else:
                    try:
                        _acl(path, sid, access="RX")
                    except (OSError, subprocess.CalledProcessError) as exc:
                        # An optional host tool must not require elevation to launch other tools.
                        _LOG.warning("Toolchain %s unavailable (RX grant refused): %s", path, exc)
                        unavailable.add(path)
            available_tools = {name: tool for name, tool in tools.items() if tool.parent not in unavailable}
            available_folders = tuple(folder for folder in folders if folder not in unavailable)
            try:
                args = command_argv(argv, self.workcopy, available_tools)
            except PermissionError as exc:
                requested = tools.get(argv[0].casefold().removesuffix(".exe").removesuffix(".cmd"))
                if requested is None and Path(argv[0]).is_absolute():
                    requested = Path(argv[0]).resolve(strict=True)
                if requested is not None and requested.parent in unavailable:
                    raise PermissionError(f"toolchain unavailable to AppContainer: {requested.parent}") from exc
                raise
            environment = child_environment(self.workcopy, available_folders)
            child = win_appcontainer.spawn_suspended(args, directory, environment, sid, network=network)
            job = KillOnCloseJob(job_limits(limits))
            job.assign(child.pid)
            child.resume()  # never resume before containment succeeds
            return RunningProcess(child, job, self.level, lambda: journal.finish(delete_profile=True))
        except BaseException:
            if job is not None:
                job.close()
            if child is not None:
                child.kill()
                try:
                    child.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                child.close()
            journal.finish(delete_profile=profile_created, revoke=grants_attempted)
            raise
