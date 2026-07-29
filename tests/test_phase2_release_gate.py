"""Phase 2 release/lifecycle preflight remains deterministic and fail-closed."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cortex_backend.execution.bundle_installer import SignedBundleInstaller
from cortex_backend.execution.lifecycle import RuntimeHealth
from cortex_backend.execution.manifest import TrustedRecipeKeys
from cortex_backend.execution.release_gate import RecipeRuntimeReleaseGate
from cortex_backend.execution.worker_release import build_signed_worker_manifest


class _ProcessFactory:
    def create_suspended(self, _plan):
        raise AssertionError("release preflight must not launch a process")


class _BrokerBinder:
    def bind_worker(self, **_kwargs):
        raise AssertionError("release preflight must not bind a broker")

    def close_binding(self):
        raise AssertionError("release preflight must not close a broker")


def _installer(tmp_path: Path) -> SignedBundleInstaller:
    signer = Ed25519PrivateKey.generate()
    source = tmp_path / "recipe-runtime"
    source.mkdir(parents=True)
    (source / "recipe_worker.exe").write_bytes(b"signed worker")
    private_key = signer.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_key = signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    release = build_signed_worker_manifest(
        source,
        private_key_bytes=private_key,
        key_id="release-1",
        bundle_version="1.0.0",
        sequence=1,
    )
    installer = SignedBundleInstaller(
        tmp_path / "store",
        TrustedRecipeKeys({"release-1": public_key}),
    )
    installer.install(release.manifest, source)
    return installer


def _gate(tmp_path: Path, **kwargs) -> RecipeRuntimeReleaseGate:
    return RecipeRuntimeReleaseGate(
        _installer(tmp_path),
        platform_name="nt",
        process_factory=_ProcessFactory(),
        broker_binder=_BrokerBinder(),
        **kwargs,
    )


def test_non_windows_is_blocked_before_reading_worker(tmp_path: Path):
    gate = RecipeRuntimeReleaseGate(
        _installer(tmp_path),
        platform_name="posix",
    )

    snapshot = gate.check()

    assert snapshot.available is False
    assert snapshot.health.code == "native_windows_required"
    assert [(check.name, check.code) for check in snapshot.checks] == [
        ("native_platform", "native_windows_required")
    ]


def test_missing_worker_is_blocked_without_adapter_or_review_access(tmp_path: Path):
    signer = Ed25519PrivateKey.generate()
    public_key = signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    installer = SignedBundleInstaller(
        tmp_path / "store",
        TrustedRecipeKeys({"release-1": public_key}),
    )
    review_calls: list[bool] = []

    snapshot = RecipeRuntimeReleaseGate(
        installer,
        platform_name="nt",
        process_factory=_ProcessFactory(),
        broker_binder=_BrokerBinder(),
        external_review_check=lambda: review_calls.append(True) or RuntimeHealth.ready(),
    ).check()

    assert snapshot.available is False
    assert snapshot.health.code == "worker_bundle_unavailable"
    assert review_calls == []
    assert snapshot.checks[-1].name == "signed_worker"


def test_process_factory_and_broker_binding_are_mandatory(tmp_path: Path):
    installer = _installer(tmp_path)
    missing_process = RecipeRuntimeReleaseGate(
        installer,
        platform_name="nt",
        broker_binder=_BrokerBinder(),
        external_review_check=RuntimeHealth.ready,
    ).check()
    assert missing_process.health.code == "native_process_factory_required"

    missing_broker = RecipeRuntimeReleaseGate(
        installer,
        platform_name="nt",
        process_factory=_ProcessFactory(),
        external_review_check=RuntimeHealth.ready,
    ).check()
    assert missing_broker.health.code == "native_broker_binding_required"


def test_external_review_is_explicit_and_fail_closed(tmp_path: Path):
    required = _gate(tmp_path).check()
    assert required.health.code == "external_review_required"
    assert [check.name for check in required.checks] == [
        "native_platform",
        "signed_worker",
        "native_process_factory",
        "native_broker_binding",
        "external_review",
    ]

    unavailable = _gate(
        tmp_path / "unavailable",
        external_review_check=lambda: RuntimeHealth.blocked(
            "review_pending", "review evidence is pending"
        ),
    ).check()
    assert unavailable.health.code == "external_review_unavailable"
    assert unavailable.health.message == (
        "External security review has not approved provider enablement."
    )


def test_invalid_or_failing_review_is_redacted(tmp_path: Path):
    invalid = _gate(
        tmp_path / "invalid",
        external_review_check=lambda: object(),
    ).check()
    assert invalid.health.code == "external_review_result_invalid"

    failed = _gate(
        tmp_path / "failed",
        external_review_check=lambda: (_ for _ in ()).throw(
            RuntimeError("secret review path")
        ),
    ).check()
    assert failed.health.code == "external_review_check_failed"
    assert "secret" not in failed.health.message.lower()


def test_all_controls_ready_returns_health_callback_result(tmp_path: Path):
    gate = _gate(
        tmp_path,
        external_review_check=lambda: RuntimeHealth.ready("review approved"),
    )
    snapshot = gate.check()

    assert snapshot.available is True
    assert snapshot.health.code == "ready"
    assert gate.health().code == "ready"
    assert [check.code for check in snapshot.checks] == [
        "ok",
        "verified",
        "configured",
        "configured",
        "approved",
    ]
