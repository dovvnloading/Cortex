"""Per-job composition of the signed native recipe worker attempt.

This module is the missing runtime binding between the durable recipe
coordinator and the already-reviewed Windows launcher/broker adapters.  Every
attempt owns a fresh broker binder, pipe identity, suspended worker, and
authenticated protocol client.  Failure at any step tears down the native
handles and is reduced to a stable coordinator error.  There is no host
process, subprocess, or alternate transport fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from queue import Empty, Queue
import os
import re
from threading import Thread
from typing import Any
from uuid import uuid4

from .bundle_installer import SignedBundleInstaller
from .models import ExecutionJob
from .native_launcher import (
    BrokerWorkerBinding,
    NativeBrokerIdentityBinder,
    NativeLauncherError,
    NativeProcessFactory,
    NativeSuspendedWorker,
    NativeWorkerLauncher,
    NativeWorkerPolicy,
)
from .recipe_coordinator import (
    DEFAULT_CANCEL_GRACE_SECONDS,
    DEFAULT_WORKER_TIMEOUT_SECONDS,
    RecipeExecutionError,
    RecipeWorkerAttempt,
    RecipeWorkerAttemptFactory,
    RecipeWorkerClient,
    RecipeWorkerOutput,
)


_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SID = re.compile(r"^S-1-[0-9]+(?:-[0-9]+)+$")
_DEFAULT_ACCEPT_TIMEOUT_SECONDS = 15.0


def _bounded_seconds(value: float, *, minimum: float, maximum: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} is invalid")
    converted = float(value)
    if not minimum <= converted <= maximum:
        raise ValueError(f"{field} is invalid")
    return converted


class NativeRecipeWorkerAttempt:
    """Recipe worker client plus the native resources that keep it trusted."""

    def __init__(
        self,
        client: RecipeWorkerClient,
        *,
        binder: NativeBrokerIdentityBinder,
        worker: NativeSuspendedWorker,
        accept_thread: Thread,
    ) -> None:
        if not isinstance(client, RecipeWorkerClient):
            raise TypeError("client must be a RecipeWorkerClient")
        self._client = client
        self._binder = binder
        self._worker = worker
        self._accept_thread = accept_thread
        self._closed = False

    def transform(self, *args: Any, **kwargs: Any) -> RecipeWorkerOutput:
        return self._client.transform(*args, **kwargs)

    def cancel(self, reason: str = "user") -> None:
        self._client.cancel(reason)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.close()
        finally:
            try:
                self._binder.close_binding()
            finally:
                try:
                    self._worker.close()
                finally:
                    self._accept_thread.join(timeout=0.5)


class NativeRecipeWorkerAttemptFactory:
    """Build one fresh signed/native attempt for each durable recipe job."""

    def __init__(
        self,
        installer: SignedBundleInstaller,
        *,
        allowed_user_sids: frozenset[str],
        process_factory_factory: Callable[[], NativeProcessFactory],
        policy: NativeWorkerPolicy | None = None,
        accept_timeout_seconds: float = _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
        worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        binder_factory: Callable[..., NativeBrokerIdentityBinder] = NativeBrokerIdentityBinder,
        launcher_factory: Callable[..., NativeWorkerLauncher] = NativeWorkerLauncher,
    ) -> None:
        if not isinstance(installer, SignedBundleInstaller):
            raise TypeError("installer must be a SignedBundleInstaller")
        if not isinstance(allowed_user_sids, frozenset) or not allowed_user_sids:
            raise ValueError("allowed_user_sids must be a non-empty frozenset")
        if any(not isinstance(sid, str) or _SID.fullmatch(sid) is None for sid in allowed_user_sids):
            raise ValueError("allowed_user_sids are invalid")
        if not callable(process_factory_factory):
            raise TypeError("process_factory_factory must be callable")
        if not callable(binder_factory) or not callable(launcher_factory):
            raise TypeError("native adapter factories must be callable")
        if policy is not None and not isinstance(policy, NativeWorkerPolicy):
            raise TypeError("policy must be a NativeWorkerPolicy")
        self._installer = installer
        self._allowed_user_sids = allowed_user_sids
        self._process_factory_factory = process_factory_factory
        self._policy = policy or NativeWorkerPolicy()
        self._accept_timeout = _bounded_seconds(
            accept_timeout_seconds,
            minimum=0.1,
            maximum=120.0,
            field="accept_timeout_seconds",
        )
        self._worker_timeout = _bounded_seconds(
            worker_timeout_seconds,
            minimum=0.1,
            maximum=600.0,
            field="worker_timeout_seconds",
        )
        self._cancel_grace = _bounded_seconds(
            cancel_grace_seconds,
            minimum=0.1,
            maximum=30.0,
            field="cancel_grace_seconds",
        )
        self._binder_factory = binder_factory
        self._launcher_factory = launcher_factory

    def __call__(self, job: ExecutionJob) -> RecipeWorkerAttempt:
        if not isinstance(job, ExecutionJob):
            raise RecipeExecutionError("worker_job_invalid")
        if _PRINCIPAL.fullmatch(job.owner) is None:
            raise RecipeExecutionError("worker_principal_invalid")
        if _SAFE_ID.fullmatch(job.job_id) is None:
            raise RecipeExecutionError("worker_job_invalid")

        binder: NativeBrokerIdentityBinder | None = None
        worker: NativeSuspendedWorker | None = None
        accept_thread: Thread | None = None
        connection: Any | None = None
        accepted: Queue[object] | None = None
        try:
            binder = self._binder_factory(allowed_user_sids=self._allowed_user_sids)
            if not all(
                callable(getattr(binder, name, None))
                for name in ("accept", "close_binding")
            ):
                raise RecipeExecutionError("worker_broker_invalid")
            process_factory = self._process_factory_factory()
            if not callable(getattr(process_factory, "create_suspended", None)):
                raise RecipeExecutionError("worker_process_factory_invalid")
            launcher = self._launcher_factory(
                self._installer,
                process_factory=process_factory,
                broker_binder=binder,
            )
            if not callable(getattr(launcher, "launch", None)):
                raise RecipeExecutionError("worker_launcher_invalid")
            binding = BrokerWorkerBinding(
                pipe_name=rf"\\.\pipe\cortex-recipe-{uuid4().hex}",
                broker_process_id=os.getpid(),
                installation_principal_id=job.owner,
                job_id=job.job_id,
            )
            worker = launcher.launch(binding, self._policy)
            if not callable(getattr(worker, "close", None)):
                raise RecipeExecutionError("worker_handle_invalid")

            accepted = Queue(maxsize=1)

            def accept() -> None:
                try:
                    connection = binder.accept(
                        owner_for_job=lambda requested: (
                            job.owner if requested == job.job_id else None
                        )
                    )
                    accepted.put(connection)
                except Exception as error:  # pragma: no cover - native failure path.
                    accepted.put(error)

            accept_thread = Thread(
                target=accept,
                name=f"cortex-native-broker-{job.job_id}",
                daemon=True,
            )
            accept_thread.start()
            try:
                value = accepted.get(timeout=self._accept_timeout)
            except Empty:
                raise RecipeExecutionError("worker_broker_timeout") from None
            if isinstance(value, Exception):
                raise RecipeExecutionError("worker_broker_failed") from None
            if not all(callable(getattr(value, name, None)) for name in ("send_message", "receive_message", "close")):
                raise RecipeExecutionError("worker_connection_invalid")
            connection = value
            client = RecipeWorkerClient(
                connection,
                installation_principal_id=job.owner,
                timeout_seconds=self._worker_timeout,
                cancel_grace_seconds=self._cancel_grace,
            )
            return NativeRecipeWorkerAttempt(
                client,
                binder=binder,
                worker=worker,
                accept_thread=accept_thread,
            )
        except RecipeExecutionError:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._cleanup(binder, worker, accept_thread=accept_thread, accepted=accepted)
            raise
        except NativeLauncherError:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._cleanup(binder, worker, accept_thread=accept_thread, accepted=accepted)
            raise RecipeExecutionError("worker_launch_failed") from None
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self._cleanup(binder, worker, accept_thread=accept_thread, accepted=accepted)
            raise RecipeExecutionError("worker_launch_failed") from None

    @staticmethod
    def _cleanup(
        binder: NativeBrokerIdentityBinder | None,
        worker: NativeSuspendedWorker | None,
        *,
        accept_thread: Thread | None = None,
        accepted: Queue[object] | None = None,
    ) -> None:
        if binder is not None:
            try:
                binder.close_binding()
            except Exception:
                pass
        if worker is not None:
            try:
                worker.close()
            except Exception:
                pass
        if accept_thread is not None:
            accept_thread.join(timeout=0.5)
        if accepted is not None:
            try:
                value = accepted.get_nowait()
            except Empty:
                return
            if callable(getattr(value, "close", None)):
                try:
                    value.close()
                except Exception:
                    pass


def build_native_recipe_worker_attempt_factory(
    installer: SignedBundleInstaller,
    *,
    allowed_user_sids: frozenset[str],
    process_factory_factory: Callable[[], NativeProcessFactory],
    policy: NativeWorkerPolicy | None = None,
    accept_timeout_seconds: float = _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
    worker_timeout_seconds: float = DEFAULT_WORKER_TIMEOUT_SECONDS,
    cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
) -> RecipeWorkerAttemptFactory:
    """Return the explicit signed/native worker factory for qualification wiring."""

    return NativeRecipeWorkerAttemptFactory(
        installer,
        allowed_user_sids=allowed_user_sids,
        process_factory_factory=process_factory_factory,
        policy=policy,
        accept_timeout_seconds=accept_timeout_seconds,
        worker_timeout_seconds=worker_timeout_seconds,
        cancel_grace_seconds=cancel_grace_seconds,
    )


__all__ = [
    "NativeRecipeWorkerAttempt",
    "NativeRecipeWorkerAttemptFactory",
    "build_native_recipe_worker_attempt_factory",
]
