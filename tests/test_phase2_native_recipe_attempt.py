"""Adversarial tests for per-job signed/native recipe attempt composition."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cortex_backend.execution.bundle_installer import SignedBundleInstaller
from cortex_backend.execution.manifest import TrustedRecipeKeys
from cortex_backend.execution.native_launcher import (
    BrokerWorkerBinding,
    NativeWorkerLaunchPlan,
    NativeWorkerPolicy,
)
from cortex_backend.execution.native_recipe_attempt import NativeRecipeWorkerAttemptFactory
from cortex_backend.execution.models import ExecutionJob


OWNER = "a" * 64


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _installer(tmp_path: Path) -> SignedBundleInstaller:
    signer = Ed25519PrivateKey.generate()
    public = signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    content = b"verified recipe worker fixture"
    source = tmp_path / "incoming"
    source.mkdir()
    (source / "recipe_worker.exe").write_bytes(content)
    unsigned = {
        "schema_version": "recipe.manifest.v1",
        "key_id": "release-1",
        "sequence": 1,
        "bundle_version": "1.0.0",
        "rollback_of": None,
        "entries": [
            {
                "recipe_id": "image-transform",
                "bundle_path": "recipe_worker.exe",
                "entrypoint": "image_transform",
                "version": "1.0.0",
                "size": len(content),
                "sha256": sha256(content).hexdigest(),
            }
        ],
    }
    signed = {
        **unsigned,
        "signature": base64.urlsafe_b64encode(signer.sign(_canonical(unsigned)))
        .decode("ascii")
        .rstrip("="),
    }
    installer = SignedBundleInstaller(
        tmp_path / "store",
        TrustedRecipeKeys({"release-1": public}),
    )
    installer.install(signed, source)
    return installer


def _job(job_id: str = "job-1") -> ExecutionJob:
    return ExecutionJob(
        job_id=job_id,
        owner=OWNER,
        request_id=f"request-{job_id}",
        profile="recipe.image.v1",
        status="running",
        sequence=1,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def send_message(self, _message: Any) -> None:
        return None

    def receive_message(self) -> Any:
        raise AssertionError("transform is not part of this composition test")

    def close(self) -> None:
        self.closed = True


class _Binder:
    def __init__(self, connection: _Connection, record: dict[str, Any]) -> None:
        self.connection = connection
        self.record = record
        self.closed = False

    def accept(self, *, owner_for_job):
        assert owner_for_job(self.record["binding"].job_id) == OWNER
        assert owner_for_job("foreign-job") is None
        return self.connection

    def close_binding(self) -> None:
        self.closed = True


class _Worker:
    process_id = 442
    app_container_sid = "S-1-15-2-123-456"

    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ProcessFactory:
    def create_suspended(self, _plan: NativeWorkerLaunchPlan):
        raise AssertionError("launcher fake owns process creation in this test")


class _Launcher:
    def __init__(self, _installer, *, process_factory, broker_binder):
        assert callable(getattr(process_factory, "create_suspended", None))
        self.binder = broker_binder

    def launch(self, binding: BrokerWorkerBinding, _policy: NativeWorkerPolicy):
        self.binder.record["binding"] = binding
        worker = _Worker(self.binder.record)
        self.binder.record["worker"] = worker
        return worker


def test_factory_creates_fresh_pipe_binder_and_worker_scope_per_job(tmp_path: Path):
    installer = _installer(tmp_path)
    records: list[dict[str, Any]] = []

    def binder_factory(**_kwargs):
        record: dict[str, Any] = {"connection": _Connection()}
        binder = _Binder(record["connection"], record)
        record["binder"] = binder
        records.append(record)
        return binder

    factory = NativeRecipeWorkerAttemptFactory(
        installer,
        allowed_user_sids=frozenset({"S-1-5-21-1-2-3-4"}),
        process_factory_factory=_ProcessFactory,
        binder_factory=binder_factory,
        launcher_factory=_Launcher,
    )

    first = factory(_job("job-1"))
    second = factory(_job("job-2"))
    assert len(records) == 2
    assert records[0]["binding"].pipe_name != records[1]["binding"].pipe_name
    assert records[0]["binding"].installation_principal_id == OWNER
    assert records[0]["binding"].broker_process_id > 0
    assert records[0]["binding"].job_id == "job-1"
    assert records[1]["binding"].job_id == "job-2"

    first.close()
    second.close()
    for record in records:
        assert record["connection"].closed
        assert record["binder"].closed
        assert record["worker"].closed


def test_factory_rejects_non_principal_job_before_native_adapters(tmp_path: Path):
    installer = _installer(tmp_path)
    called = []
    factory = NativeRecipeWorkerAttemptFactory(
        installer,
        allowed_user_sids=frozenset({"S-1-5-21-1-2-3-4"}),
        process_factory_factory=lambda: called.append(True),
    )
    invalid = _job()
    invalid = ExecutionJob(
        job_id=invalid.job_id,
        owner="not-a-principal",
        request_id=invalid.request_id,
        profile=invalid.profile,
        status=invalid.status,
        sequence=invalid.sequence,
        created_at=invalid.created_at,
        updated_at=invalid.updated_at,
    )
    with pytest.raises(Exception) as error:
        factory(invalid)
    assert getattr(error.value, "code", None) == "worker_principal_invalid"
    assert called == []


def test_factory_requires_explicit_process_factory_and_bounded_options(tmp_path: Path):
    installer = _installer(tmp_path)
    with pytest.raises(TypeError):
        NativeRecipeWorkerAttemptFactory(
            installer,
            allowed_user_sids=frozenset({"S-1-5-21-1-2-3-4"}),
            process_factory_factory=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        NativeRecipeWorkerAttemptFactory(
            installer,
            allowed_user_sids=frozenset({"S-1-5-21-1-2-3-4"}),
            process_factory_factory=_ProcessFactory,
            accept_timeout_seconds=120.1,
        )
