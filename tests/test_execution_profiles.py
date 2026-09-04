"""The execution profile seam stays exact and fail-closed.

There are two profiles. ``disabled`` builds no coordinator and runs no health
probe; ``local`` starts the checked-in process-backed workers. A third,
``qualification``, existed for a code-signed native worker that was never
built, so it is now rejected like any other unknown name.
"""

from __future__ import annotations

import pytest

from cortex_backend.execution.profiles import (
    ExecutionProfileError,
    build_execution_lifecycle,
    parse_execution_profile,
)
from cortex_backend.execution.repository import ExecutionRepository


def test_profile_parser_is_exact_and_defaults_to_disabled():
    assert parse_execution_profile(None) == "disabled"
    assert parse_execution_profile("disabled") == "disabled"
    assert parse_execution_profile("local") == "local"

    with pytest.raises(ExecutionProfileError) as padded:
        parse_execution_profile(" Local ")
    assert padded.value.code == "execution_profile_unknown"

    with pytest.raises(ExecutionProfileError) as retired:
        parse_execution_profile("qualification")
    assert retired.value.code == "execution_profile_unknown"

    with pytest.raises(ExecutionProfileError) as wrong_type:
        parse_execution_profile(object())  # type: ignore[arg-type]
    assert wrong_type.value.code == "execution_profile_invalid"


def test_disabled_profile_never_calls_health_or_coordinator(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")

    lifecycle = build_execution_lifecycle(repository, profile=None)

    assert lifecycle.snapshot.profile == "disabled"
    assert lifecycle.snapshot.state == "disabled"
    assert lifecycle.start().health.code == "runtime_disabled"
    assert lifecycle.coordinator is None


def test_local_profile_starts_the_checked_in_runtime(tmp_path):
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")

    lifecycle = build_execution_lifecycle(repository, profile="local")
    snapshot = lifecycle.start()

    assert snapshot.profile == "local"
    assert snapshot.available is True
    assert lifecycle.coordinator is not None
    assert lifecycle.stop().state == "stopped"


def test_app_factory_uses_the_checked_in_local_profile_by_default(tmp_path):
    from app_factory import build_app

    app = build_app(data_dir=tmp_path / "app-data", serve_frontend=False)

    assert app.state.execution_lifecycle.profile == "local"
    assert app.state.execution_lifecycle.snapshot.state == "stopped"
