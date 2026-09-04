"""Cortex's version is written down once, and everything else derives from it.

It used to be written six times -- ``pyproject.toml``, ``main.py``, the FastAPI
app, two launcher defaults, and ``frontend/package.json`` -- with nothing
holding them together. npm cannot read a Python constant, so ``package.json``
is checked here rather than derived.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex_backend import __version__


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

tomllib = pytest.importorskip("tomllib", reason="tomllib requires Python 3.11+")


@pytest.fixture(scope="module")
def project() -> dict:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]


def test_pyproject_reads_the_version_instead_of_restating_it(project: dict) -> None:
    assert "version" not in project, (
        "pyproject.toml declares a literal version; it should stay dynamic so "
        "cortex_backend.__version__ remains the only place it is written"
    )
    assert project["dynamic"] == ["version"]


def test_the_frontend_package_declares_the_same_version() -> None:
    package_json = json.loads(
        (REPOSITORY_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )

    assert package_json["version"] == __version__, (
        f"frontend/package.json says {package_json['version']}, "
        f"cortex_backend.__version__ says {__version__}"
    )


@pytest.mark.parametrize(
    "relative",
    [
        "main.py",
        "backend/cortex_backend/api/app.py",
        "backend/cortex_backend/launcher/frontend.py",
    ],
)
def test_no_module_hard_codes_the_version(relative: str) -> None:
    """A literal that slipped back in would defeat the single source."""
    source = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")

    assert f'"{__version__}"' not in source, (
        f"{relative} hard-codes {__version__}; import cortex_backend.__version__"
    )


def test_the_api_reports_the_declared_version() -> None:
    """The version a client sees has to be the one the package declares."""
    from cortex_backend.api import build_demo_dependencies, create_app

    app = create_app(build_demo_dependencies(), allowed_hosts=("testserver",))

    assert app.version == __version__


def test_the_generated_contract_matches_the_declared_version() -> None:
    """contracts/openapi.json is generated, so drift here means it is stale."""
    contract = json.loads(
        (REPOSITORY_ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8")
    )

    assert contract["info"]["version"] == __version__, (
        "contracts/openapi.json is stale; run python tools/generate_contracts.py --write"
    )
