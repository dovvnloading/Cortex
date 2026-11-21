"""Regression checks: backend test installs must never build actionlint."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _job(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match is not None
    return match.group(1)


def test_backend_jobs_install_no_actionlint_sdist() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        dev = tomllib.load(stream)["project"]["optional-dependencies"]["dev"]

    assert not any(requirement.startswith("actionlint-py") for requirement in dev)
    assert "actionlint-py" not in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "actionlint-py" not in (ROOT / "requirements-dev.lock.txt").read_text(encoding="utf-8")

    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    for job_name in ("backend-shard", "backend"):
        job = _job(quality, job_name)
        assert "python -m pip install -r requirements-dev.lock.txt" in job
        assert "run_actionlint.ps1" not in job


def test_ci_and_local_checks_use_the_same_verified_actionlint_binary() -> None:
    quality = (ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    local = (ROOT / "scripts/check.ps1").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/run_actionlint.ps1").read_text(encoding="utf-8")

    assert "run: ./scripts/run_actionlint.ps1" in _job(quality, "lint")
    assert "run_actionlint.ps1" in local
    assert 'actionlint_$($version)_windows_amd64.zip' in runner
    assert re.search(r"\$expectedSha256 = '[a-f0-9]{64}'", runner)
    assert "Get-FileHash" in runner
    assert "Expand-Archive" in runner
    assert runner.index("Get-FileHash") < runner.index("Expand-Archive")
    assert "& $binary -shellcheck= -pyflakes=" in runner

