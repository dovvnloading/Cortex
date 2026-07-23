"""Fail-closed native launcher boundary tests."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import cortex_backend.execution.native_launcher as launcher_module
from cortex_backend.execution.bundle_installer import SignedBundleInstaller
from cortex_backend.execution.native_launcher import (
    BrokerWorkerBinding,
    NativeBrokerIdentityBinder,
    NativeLauncherError,
    NativeWorkerLaunchPlan,
    NativeWorkerLauncher,
    NativeWorkerPolicy,
)
from cortex_backend.execution.manifest import TrustedRecipeKeys


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


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
    payload = {
        **unsigned,
        "signature": base64.urlsafe_b64encode(signer.sign(_canonical(unsigned)))
        .decode("ascii")
        .rstrip("="),
    }
    installer = SignedBundleInstaller(tmp_path / "store", TrustedRecipeKeys({"release-1": public}))
    installer.install(payload, source)
    return installer


def _binding() -> BrokerWorkerBinding:
    return BrokerWorkerBinding(
        pipe_name=r"\\.\pipe\cortex-worker-test",
        broker_process_id=321,
        installation_principal_id="a" * 64,
        job_id="job-1",
    )


def test_policy_is_bounded_and_has_no_breakaway_flags():
    policy = NativeWorkerPolicy()
    assert policy.required_limit_flags == 0x230E
    assert not policy.required_limit_flags & 0x1800  # BREAKAWAY_OK and SILENT_BREAKAWAY_OK.

    with pytest.raises(ValueError):
        NativeWorkerPolicy(process_memory_limit_bytes=0)
    with pytest.raises(ValueError):
        NativeWorkerPolicy(job_memory_limit_bytes=1, process_memory_limit_bytes=2)
    with pytest.raises(ValueError):
        NativeWorkerPolicy(watchdog_timeout_ms=600_001)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pipe_name": r"\\.\pipe\other-worker"},
        {"broker_process_id": 0},
        {"installation_principal_id": "not-a-principal"},
        {"job_id": "../job"},
    ],
)
def test_broker_binding_rejects_untrusted_identity_values(kwargs):
    values = {
        "pipe_name": r"\\.\pipe\cortex-worker-test",
        "broker_process_id": 321,
        "installation_principal_id": "a" * 64,
        "job_id": "job-1",
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        BrokerWorkerBinding(**values)


def test_prepare_reverifies_active_worker_and_builds_fixed_command_line(tmp_path: Path):
    installer = _installer(tmp_path)
    plan = NativeWorkerLauncher(installer).prepare(_binding())

    assert plan.executable.name == "recipe_worker.exe"
    assert plan.worker.worker_path == "recipe_worker.exe"
    assert plan.broker.broker_process_id == 321
    assert subprocess.list2cmdline(
        [
            str(plan.executable),
            "--native-broker",
            "--broker-pipe",
            _binding().pipe_name,
            "--broker-pid",
            "321",
        ]
    ) == plan.command_line
    assert "shell" not in plan.command_line.casefold()


def test_launch_refuses_before_process_creation_without_live_broker_binder(tmp_path: Path):
    installer = _installer(tmp_path)
    created = []

    class _Factory:
        def create_suspended(self, plan: NativeWorkerLaunchPlan):
            created.append(plan)
            raise AssertionError("factory must not run before broker binding exists")

    launcher = NativeWorkerLauncher(installer, process_factory=_Factory())
    with pytest.raises(NativeLauncherError) as error:
        launcher.launch(_binding())
    assert error.value.code == "native_broker_binding_required"
    assert created == []


@pytest.mark.skipif(os.name != "nt", reason="native launch orchestration is Windows-only")
def test_launch_orders_policy_binding_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    installer = _installer(tmp_path)
    events: list[str] = []

    class _Worker:
        process_id = 444
        app_container_sid = "S-1-15-2-123-456"

        def apply_job_policy(self, policy: NativeWorkerPolicy) -> None:
            assert policy.required_limit_flags == 0x230E
            events.append("policy")

        def resume(self) -> None:
            events.append("resume")

        def close(self) -> None:
            events.append("close")

    class _Factory:
        def create_suspended(self, plan: NativeWorkerLaunchPlan):
            assert plan.worker.worker_path == "recipe_worker.exe"
            events.append("create")
            return _Worker()

    class _Binder:
        def bind_worker(self, *, process_id: int, app_container_sid: str, binding: BrokerWorkerBinding):
            assert process_id == 444
            assert app_container_sid == "S-1-15-2-123-456"
            assert binding.job_id == "job-1"
            events.append("bind")

    result = NativeWorkerLauncher(
        installer,
        process_factory=_Factory(),
        broker_binder=_Binder(),
    ).launch(_binding())

    assert result.process_id == 444
    assert events == ["create", "policy", "bind", "resume"]


@pytest.mark.skipif(os.name != "nt", reason="native launch orchestration is Windows-only")
def test_broker_binding_failure_closes_worker_without_resume(tmp_path: Path):
    installer = _installer(tmp_path)
    events: list[str] = []

    class _Worker:
        process_id = 445
        app_container_sid = "S-1-15-2-123-456"

        def apply_job_policy(self, policy: NativeWorkerPolicy) -> None:
            events.append("policy")

        def resume(self) -> None:
            events.append("resume")

        def close(self) -> None:
            events.append("close")

    class _Factory:
        def create_suspended(self, plan: NativeWorkerLaunchPlan):
            events.append("create")
            return _Worker()

    class _Binder:
        def bind_worker(self, **kwargs):
            del kwargs
            events.append("bind")
            raise NativeLauncherError("native_peer_identity_mismatch")

    with pytest.raises(NativeLauncherError) as error:
        NativeWorkerLauncher(
            installer,
            process_factory=_Factory(),
            broker_binder=_Binder(),
        ).launch(_binding())
    assert error.value.code == "native_peer_identity_mismatch"
    assert events == ["create", "policy", "bind", "close"]


def test_launcher_closes_live_broker_binding_if_resume_fails(tmp_path: Path):
    installer = _installer(tmp_path)
    events: list[str] = []

    class _Worker:
        process_id = 446
        app_container_sid = "S-1-15-2-123-456"

        def apply_job_policy(self, policy: NativeWorkerPolicy) -> None:
            events.append("policy")

        def resume(self) -> None:
            events.append("resume")
            raise NativeLauncherError("native_resume_failed")

        def close(self) -> None:
            events.append("worker-close")

    class _Factory:
        def create_suspended(self, plan: NativeWorkerLaunchPlan):
            events.append("create")
            return _Worker()

    class _Binder:
        def bind_worker(self, **kwargs):
            del kwargs
            events.append("bind")

        def close_binding(self):
            events.append("broker-close")

    with pytest.raises(NativeLauncherError) as error:
        NativeWorkerLauncher(
            installer,
            process_factory=_Factory(),
            broker_binder=_Binder(),
        ).launch(_binding())
    assert error.value.code == "native_resume_failed"
    assert events == ["create", "policy", "bind", "resume", "broker-close", "worker-close"]


def test_tampered_worker_is_rejected_at_launch_plan_boundary(tmp_path: Path):
    installer = _installer(tmp_path)
    installed = installer.status()
    assert installed is not None
    (installed.bundle_root / "recipe_worker.exe").write_bytes(b"tampered")

    with pytest.raises(NativeLauncherError) as error:
        NativeWorkerLauncher(installer).prepare(_binding())
    assert error.value.code == "worker_bundle_integrity_failed"


def test_native_broker_identity_binder_pins_worker_pid_and_appcontainer_sid(
    monkeypatch: pytest.MonkeyPatch,
):
    servers = []
    accepted = object()

    class _Server:
        def __init__(self, config):
            self.config = config
            self.opened = False
            self.closed = False

        def open(self):
            self.opened = True

        def accept(self, *, expected_principal_id, owner_for_job):
            assert expected_principal_id == "a" * 64
            assert owner_for_job("job-1") == "owner"
            return accepted

        def close(self):
            self.closed = True

    def factory(config):
        server = _Server(config)
        servers.append(server)
        return server

    monkeypatch.setattr(launcher_module.os, "getpid", lambda: 321)
    binder = NativeBrokerIdentityBinder(
        allowed_user_sids=frozenset({"S-1-5-21-1-2-3-4"}),
        server_factory=factory,
    )
    binding = _binding()
    binder.bind_worker(
        process_id=446,
        app_container_sid="S-1-15-2-123-456",
        binding=binding,
    )

    assert binder.bound
    assert binder.binding == binding
    assert servers[0].config.peer_policy.expected_process_id == 446
    assert servers[0].config.peer_policy.acl.allowed_app_container_sids == frozenset(
        {"S-1-15-2-123-456"}
    )
    assert binder.accept(owner_for_job=lambda job_id: "owner" if job_id == "job-1" else None) is accepted
    binder.close_binding()
    assert not binder.bound
    assert servers[0].closed


def test_native_broker_identity_binder_rejects_pid_or_token_mismatch(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(launcher_module.os, "getpid", lambda: 321)
    binder = NativeBrokerIdentityBinder(
        allowed_user_sids=frozenset({"S-1-5-21-1-2-3-4"}),
        server_factory=lambda config: object(),
    )
    with pytest.raises(NativeLauncherError) as pid_error:
        binder.bind_worker(
            process_id=446,
            app_container_sid="S-1-15-2-123-456",
            binding=BrokerWorkerBinding(
                pipe_name=_binding().pipe_name,
                broker_process_id=322,
                installation_principal_id="a" * 64,
                job_id="job-1",
            ),
        )
    assert pid_error.value.code == "native_broker_process_mismatch"
    with pytest.raises(NativeLauncherError) as sid_error:
        binder.bind_worker(
            process_id=446,
            app_container_sid="S-1-5-21-1-2-3-4",
            binding=_binding(),
        )
    assert sid_error.value.code == "native_appcontainer_sid_invalid"


@pytest.mark.skipif(os.name != "nt", reason="native broker server requires Windows")
def test_native_broker_identity_binder_opens_real_protected_server():
    binding = BrokerWorkerBinding(
        pipe_name=rf"\\.\pipe\cortex-binder-{uuid4().hex}",
        broker_process_id=os.getpid(),
        installation_principal_id="a" * 64,
        job_id="job-real-binder",
    )
    binder = NativeBrokerIdentityBinder(
        allowed_user_sids=frozenset({"S-1-5-21-100-200-300-400"}),
    )
    binder.bind_worker(
        process_id=os.getpid(),
        app_container_sid="S-1-15-2-100-200-300-400",
        binding=binding,
    )
    try:
        assert binder.bound
        assert binder.binding == binding
    finally:
        binder.close_binding()
    assert not binder.bound


def test_non_windows_launch_is_blocked_before_adapters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    installer = _installer(tmp_path)
    monkeypatch.setattr(launcher_module.os, "name", "posix")
    with pytest.raises(NativeLauncherError) as error:
        NativeWorkerLauncher(installer, process_factory=object(), broker_binder=object()).launch(
            _binding()
        )
    assert error.value.code == "native_windows_required"
