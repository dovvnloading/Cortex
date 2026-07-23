"""Fail-closed native worker launch boundary.

This module is the seam between storage-only worker provenance and the future
Windows process factory.  It deliberately does not execute through
``subprocess`` or provide a host-process fallback.  A reviewed native factory and
a live broker binder must be injected before a process can be created.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
from typing import Final, Protocol

from .broker import BrokerAclPolicy, BrokerPeerPolicy
from .bundle_installer import SignedBundleInstaller
from .native_broker import (
    NativeBrokerConnection,
    NativeBrokerServer,
    NativeBrokerServerConfig,
)
from .worker_provenance import (
    EXPECTED_WORKER_PATH,
    VerifiedRecipeWorker,
    WorkerProvenanceError,
    verify_active_worker,
)


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SID = re.compile(r"^S-1-[0-9]+(?:-[0-9]+)+$")
_APPCONTAINER_SID = re.compile(r"^S-1-15-2-(?:[0-9]+-)+[0-9]+$")
_PIPE = re.compile(r"^\\\\\.\\pipe\\cortex-[A-Za-z0-9._-]{1,200}$")
_MAX_WORKER_BYTES = 128 * 1024 * 1024
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_REQUIRED_LIMIT_FLAGS: Final[int] = (
    _JOB_OBJECT_LIMIT_PROCESS_TIME
    | _JOB_OBJECT_LIMIT_JOB_TIME
    | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
    | _JOB_OBJECT_LIMIT_JOB_MEMORY
    | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
)


class NativeLauncherError(ValueError):
    """Stable, non-sensitive native launch failure category."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid native launcher code")
        self.code = code
        super().__init__("The recipe worker could not be launched safely.")


def _positive_bounded(value: int, *, maximum: int, code: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(code)
    return value


@dataclass(frozen=True, slots=True)
class NativeWorkerPolicy:
    """Resource policy that must be applied before a suspended worker resumes."""

    active_process_limit: int = 1
    process_memory_limit_bytes: int = 64 * 1024 * 1024
    job_memory_limit_bytes: int = 128 * 1024 * 1024
    process_user_time_100ns: int = 20_000_000
    job_user_time_100ns: int = 40_000_000
    watchdog_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        _positive_bounded(self.active_process_limit, maximum=64, code="active_process_limit_invalid")
        _positive_bounded(
            self.process_memory_limit_bytes,
            maximum=1024 * 1024 * 1024,
            code="process_memory_limit_invalid",
        )
        _positive_bounded(
            self.job_memory_limit_bytes,
            maximum=1024 * 1024 * 1024,
            code="job_memory_limit_invalid",
        )
        _positive_bounded(
            self.process_user_time_100ns,
            maximum=600 * 10_000_000,
            code="process_cpu_limit_invalid",
        )
        _positive_bounded(
            self.job_user_time_100ns,
            maximum=600 * 10_000_000,
            code="job_cpu_limit_invalid",
        )
        _positive_bounded(self.watchdog_timeout_ms, maximum=600_000, code="watchdog_timeout_invalid")
        if self.job_memory_limit_bytes < self.process_memory_limit_bytes:
            raise ValueError("job memory limit must cover the process limit")
        if self.job_user_time_100ns < self.process_user_time_100ns:
            raise ValueError("job CPU limit must cover the process limit")

    @property
    def required_limit_flags(self) -> int:
        """Return the exact flags; breakaway flags are intentionally absent."""

        return _REQUIRED_LIMIT_FLAGS


@dataclass(frozen=True, slots=True)
class BrokerWorkerBinding:
    """Trusted broker identity values used to bind one suspended worker."""

    pipe_name: str
    broker_process_id: int
    installation_principal_id: str
    job_id: str

    def __post_init__(self) -> None:
        if _PIPE.fullmatch(self.pipe_name) is None:
            raise ValueError("broker pipe name is invalid")
        if type(self.broker_process_id) is not int or self.broker_process_id <= 0:
            raise ValueError("broker process ID is invalid")
        if _PRINCIPAL.fullmatch(self.installation_principal_id) is None:
            raise ValueError("installation principal is invalid")
        if _SAFE_ID.fullmatch(self.job_id) is None:
            raise ValueError("job ID is invalid")


@dataclass(frozen=True, slots=True)
class NativeWorkerLaunchPlan:
    """Immutable launch inputs after provenance and command-line validation."""

    worker: VerifiedRecipeWorker
    executable: Path
    command_line: str
    broker: BrokerWorkerBinding
    policy: NativeWorkerPolicy


class NativeSuspendedWorker(Protocol):
    """Handle returned by a reviewed Windows native process factory."""

    process_id: int
    app_container_sid: str

    def apply_job_policy(self, policy: NativeWorkerPolicy) -> None:
        """Assign the suspended process and verify all Job Object limits."""

    def resume(self) -> None:
        """Resume only after policy and broker binding have succeeded."""

    def close(self) -> None:
        """Kill-on-close and release every native handle."""


class NativeProcessFactory(Protocol):
    """Factory implemented only by the reviewed Windows ctypes adapter."""

    def create_suspended(self, plan: NativeWorkerLaunchPlan) -> NativeSuspendedWorker:
        """Create the verified worker with its AppContainer token suspended."""


class BrokerWorkerBinder(Protocol):
    """Live native broker binding performed before the worker resumes."""

    def bind_worker(
        self,
        *,
        process_id: int,
        app_container_sid: str,
        binding: BrokerWorkerBinding,
    ) -> None:
        """Bind expected PID, token identity, principal, and job ownership."""

    def close_binding(self) -> None:
        """Close the per-worker broker endpoint during cancellation or failure."""


class NativeBrokerIdentityBinder:
    """Bind one live named-pipe server to a suspended worker identity."""

    def __init__(
        self,
        *,
        allowed_user_sids: frozenset[str],
        server_factory: Callable[[NativeBrokerServerConfig], NativeBrokerServer] = NativeBrokerServer,
    ) -> None:
        if not allowed_user_sids or any(_SID.fullmatch(sid) is None for sid in allowed_user_sids):
            raise ValueError("allowed user SIDs are invalid")
        self._allowed_user_sids = frozenset(allowed_user_sids)
        self._server_factory = server_factory
        self._server: NativeBrokerServer | None = None
        self._binding: BrokerWorkerBinding | None = None

    @property
    def bound(self) -> bool:
        return self._server is not None and self._binding is not None

    @property
    def binding(self) -> BrokerWorkerBinding | None:
        return self._binding

    def bind_worker(
        self,
        *,
        process_id: int,
        app_container_sid: str,
        binding: BrokerWorkerBinding,
    ) -> None:
        if self.bound:
            raise NativeLauncherError("native_broker_already_bound")
        if type(process_id) is not int or process_id <= 0:
            raise NativeLauncherError("native_worker_identity_invalid")
        if _APPCONTAINER_SID.fullmatch(app_container_sid) is None:
            raise NativeLauncherError("native_appcontainer_sid_invalid")
        if binding.broker_process_id != os.getpid():
            raise NativeLauncherError("native_broker_process_mismatch")
        acl = BrokerAclPolicy(
            allowed_user_sids=self._allowed_user_sids,
            allowed_app_container_sids=frozenset({app_container_sid}),
        )
        peer_policy = BrokerPeerPolicy(
            acl=acl,
            expected_process_id=process_id,
            maximum_integrity="low",
        )
        config = NativeBrokerServerConfig(pipe_name=binding.pipe_name, peer_policy=peer_policy)
        try:
            server = self._server_factory(config)
            server.open()
        except NativeLauncherError:
            raise
        except Exception:
            try:
                if "server" in locals():
                    server.close()
            except Exception:
                pass
            raise NativeLauncherError("native_broker_open_failed") from None
        self._server = server
        self._binding = binding

    def accept(
        self,
        *,
        owner_for_job: Callable[[str], str | None],
    ) -> NativeBrokerConnection:
        if self._server is None or self._binding is None:
            raise NativeLauncherError("native_broker_not_bound")
        try:
            return self._server.accept(
                expected_principal_id=self._binding.installation_principal_id,
                owner_for_job=owner_for_job,
            )
        except NativeLauncherError:
            raise
        except Exception:
            raise NativeLauncherError("native_broker_accept_failed") from None

    def close_binding(self) -> None:
        server, self._server = self._server, None
        self._binding = None
        if server is not None:
            server.close()


def _revalidate_worker(worker: VerifiedRecipeWorker) -> Path:
    root = worker.bundle_root
    try:
        root = root.resolve(strict=True)
        raw_candidate = root / worker.worker_path
        if raw_candidate.is_symlink() or getattr(raw_candidate, "is_junction", lambda: False)():
            raise NativeLauncherError("worker_path_invalid")
        candidate = raw_candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise NativeLauncherError("worker_path_unavailable") from None
    if worker.worker_path != EXPECTED_WORKER_PATH or not candidate.is_relative_to(root):
        raise NativeLauncherError("worker_path_invalid")
    try:
        stat = candidate.stat()
        if not candidate.is_file() or candidate.is_symlink() or stat.st_nlink != 1:
            raise NativeLauncherError("worker_path_invalid")
        if stat.st_size != worker.worker_size or stat.st_size > _MAX_WORKER_BYTES:
            raise NativeLauncherError("worker_size_mismatch")
        before_identity = (
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
            int(getattr(stat, "st_ino", 0)),
        )
        digest = sha256()
        with candidate.open("rb") as stream:
            remaining = worker.worker_size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise NativeLauncherError("worker_size_mismatch")
                digest.update(chunk)
                remaining -= len(chunk)
        after = candidate.stat()
    except NativeLauncherError:
        raise
    except OSError:
        raise NativeLauncherError("worker_path_unavailable") from None
    after_identity = (
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
        int(getattr(after, "st_ino", 0)),
    )
    if before_identity != after_identity:
        raise NativeLauncherError("worker_path_changed")
    if digest.hexdigest() != worker.worker_sha256:
        raise NativeLauncherError("worker_hash_mismatch")
    return candidate


class NativeWorkerLauncher:
    """Construct and execute only a verified, broker-bound native worker."""

    def __init__(
        self,
        installer: SignedBundleInstaller,
        *,
        process_factory: NativeProcessFactory | None = None,
        broker_binder: BrokerWorkerBinder | None = None,
    ) -> None:
        if not isinstance(installer, SignedBundleInstaller):
            raise TypeError("installer must be a SignedBundleInstaller")
        self._installer = installer
        self._process_factory = process_factory
        self._broker_binder = broker_binder

    def prepare(
        self,
        binding: BrokerWorkerBinding,
        policy: NativeWorkerPolicy | None = None,
    ) -> NativeWorkerLaunchPlan:
        """Reverify the active bundle and build a fixed command line only."""

        if not isinstance(binding, BrokerWorkerBinding):
            raise TypeError("binding must be a BrokerWorkerBinding")
        if policy is not None and not isinstance(policy, NativeWorkerPolicy):
            raise TypeError("policy must be a NativeWorkerPolicy")
        try:
            worker = verify_active_worker(self._installer)
        except WorkerProvenanceError as error:
            raise NativeLauncherError(error.code) from None
        executable = _revalidate_worker(worker)
        command_line = subprocess.list2cmdline(
            [
                str(executable),
                "--native-broker",
                "--broker-pipe",
                binding.pipe_name,
                "--broker-pid",
                str(binding.broker_process_id),
            ]
        )
        return NativeWorkerLaunchPlan(
            worker=worker,
            executable=executable,
            command_line=command_line,
            broker=binding,
            policy=policy or NativeWorkerPolicy(),
        )

    def launch(
        self,
        binding: BrokerWorkerBinding,
        policy: NativeWorkerPolicy | None = None,
    ) -> NativeSuspendedWorker:
        """Launch only through injected native and broker-reviewed adapters."""

        if os.name != "nt":
            raise NativeLauncherError("native_windows_required")
        if self._broker_binder is None:
            raise NativeLauncherError("native_broker_binding_required")
        if self._process_factory is None:
            raise NativeLauncherError("native_process_factory_required")
        plan = self.prepare(binding, policy)
        worker: NativeSuspendedWorker | None = None
        broker_bound = False
        try:
            worker = self._process_factory.create_suspended(plan)
            if type(worker.process_id) is not int or worker.process_id <= 0:
                raise NativeLauncherError("native_worker_identity_invalid")
            if not isinstance(worker.app_container_sid, str) or not worker.app_container_sid:
                raise NativeLauncherError("native_worker_identity_invalid")
            worker.apply_job_policy(plan.policy)
            self._broker_binder.bind_worker(
                process_id=worker.process_id,
                app_container_sid=worker.app_container_sid,
                binding=plan.broker,
            )
            broker_bound = True
            worker.resume()
            return worker
        except NativeLauncherError:
            if broker_bound:
                close_binding = getattr(self._broker_binder, "close_binding", None)
                if close_binding is not None:
                    close_binding()
            if worker is not None:
                worker.close()
            raise
        except Exception:
            if broker_bound:
                close_binding = getattr(self._broker_binder, "close_binding", None)
                if close_binding is not None:
                    close_binding()
            if worker is not None:
                worker.close()
            raise NativeLauncherError("native_launch_failed") from None


__all__ = [
    "BrokerWorkerBinding",
    "BrokerWorkerBinder",
    "NativeBrokerIdentityBinder",
    "NativeLauncherError",
    "NativeProcessFactory",
    "NativeSuspendedWorker",
    "NativeWorkerLaunchPlan",
    "NativeWorkerLauncher",
    "NativeWorkerPolicy",
]
