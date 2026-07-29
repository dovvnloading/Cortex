"""Explicit local qualification lifecycle wiring remains fail-closed."""

from __future__ import annotations

import pytest

from cortex_backend.execution.coordinator import DurableFakeCoordinator
from cortex_backend.execution.lifecycle import RuntimeHealth
from cortex_backend.execution.qualification import (
    QualificationLifecycleConfig,
    QualificationProfileError,
    build_execution_lifecycle,
    build_recipe_coordinator_factory,
    parse_execution_profile,
)
from cortex_backend.execution.bundle_installer import SignedBundleInstaller
from cortex_backend.execution.manifest import TrustedRecipeKeys
from cortex_backend.execution.release_gate import RecipeRuntimeReleaseGate
from cortex_backend.execution.repository import ExecutionRepository


def _gate(tmp_path, *, profile: str = "qualification") -> RecipeRuntimeReleaseGate:
    installer = SignedBundleInstaller(
        tmp_path / "bundle-store",
        TrustedRecipeKeys({"qualification": b"q" * 32}),
    )
    return RecipeRuntimeReleaseGate(
        installer,
        platform_name="nt",
        process_factory=object(),
        broker_binder=object(),
        release_profile=profile,
    )


def test_profile_parser_is_exact_and_defaults_to_disabled():
    assert parse_execution_profile(None) == "disabled"
    assert parse_execution_profile("disabled") == "disabled"
    assert parse_execution_profile("qualification") == "qualification"
    with pytest.raises(QualificationProfileError) as error:
        parse_execution_profile(" Qualification ")
    assert error.value.code == "execution_profile_invalid"


def test_disabled_profile_never_calls_health_or_coordinator(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    calls: list[str] = []
    lifecycle = build_execution_lifecycle(
        repository,
        profile=None,
    )
    assert lifecycle.snapshot.profile == "disabled"
    assert lifecycle.snapshot.state == "disabled"
    assert lifecycle.start().health.code == "runtime_disabled"
    assert calls == []


def test_preview_builder_remains_disabled_without_explicit_profile(tmp_path):
    from Cortex_Preview import build_preview_app

    app = build_preview_app(data_dir=tmp_path / "app-data", serve_frontend=False)

    assert app.state.execution_lifecycle.profile == "disabled"
    assert app.state.execution_lifecycle.snapshot.state == "disabled"


def test_qualification_without_controls_is_blocked_without_factory_call(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    lifecycle = build_execution_lifecycle(repository, profile="qualification")

    snapshot = lifecycle.start()

    assert snapshot.profile == "qualification"
    assert snapshot.state == "blocked"
    assert snapshot.health.code == "qualification_configuration_missing"
    assert lifecycle.coordinator is None


def test_qualification_config_rejects_official_release_gate(tmp_path):
    with pytest.raises(QualificationProfileError) as error:
        QualificationLifecycleConfig(
            release_gate=_gate(tmp_path, profile="official"),
            coordinator_factory=lambda repository: DurableFakeCoordinator(repository),
            provider_health_check=lambda _health: RuntimeHealth.ready(),
        )
    assert error.value.code == "qualification_gate_required"


def test_qualification_health_failure_stays_blocked_before_coordinator(tmp_path, monkeypatch):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    gate = _gate(tmp_path)
    monkeypatch.setattr(
        gate,
        "health",
        lambda: RuntimeHealth.blocked(
            "worker_bundle_unavailable", "The signed recipe worker is unavailable."
        ),
    )
    factory_calls: list[bool] = []
    config = QualificationLifecycleConfig(
        release_gate=gate,
        coordinator_factory=lambda repo: factory_calls.append(True)
        or DurableFakeCoordinator(repo, auto_recover=False),
        provider_health_check=lambda _health: RuntimeHealth.ready(),
    )

    snapshot = build_execution_lifecycle(
        repository,
        profile="qualification",
        qualification=config,
    ).start()

    assert snapshot.state == "blocked"
    assert snapshot.health.code == "worker_bundle_unavailable"
    assert factory_calls == []


def test_qualification_profile_composes_gate_provider_health_and_lifecycle(tmp_path, monkeypatch):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    gate = _gate(tmp_path)
    monkeypatch.setattr(gate, "health", lambda: RuntimeHealth.ready("gate passed"))
    provider_inputs: list[str] = []
    factory_calls: list[bool] = []
    config = QualificationLifecycleConfig(
        release_gate=gate,
        coordinator_factory=lambda repo: factory_calls.append(True)
        or DurableFakeCoordinator(repo, auto_recover=False),
        provider_health_check=lambda health: provider_inputs.append(health.code)
        or RuntimeHealth.ready("provider passed"),
    )

    lifecycle = build_execution_lifecycle(
        repository,
        profile="qualification",
        qualification=config,
    )
    snapshot = lifecycle.start()

    assert snapshot.available is True
    assert snapshot.profile == "qualification"
    assert snapshot.health.message == "Local recipe qualification controls are ready."
    assert provider_inputs == ["ready"]
    assert factory_calls == [True]
    assert lifecycle.stop().state == "stopped"


def test_recipe_factory_keeps_worker_attempt_injection_explicit_and_repository_bound(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    worker_calls: list[object] = []

    def worker_attempt_factory(job):
        worker_calls.append(job)
        return object()

    factory = build_recipe_coordinator_factory(worker_attempt_factory)
    assert worker_calls == []

    coordinator = factory(repository)
    assert coordinator.repository is repository
    assert coordinator.worker_factory is worker_attempt_factory
    assert coordinator.artifact_boundary.repository is repository
    coordinator.shutdown()
    assert worker_calls == []
