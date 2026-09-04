"""The three places that declare dependencies must agree.

``pyproject.toml`` is the source of truth. ``requirements.txt`` and
``requirements-dev.txt`` restate the same constraints for humans following the
README, and CI's lockfiles are compiled from ``pyproject.toml`` alone -- so a
pin edited in one file and not the other passes every gate while the file the
README tells users to install from says something different.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# tomllib is 3.11+. The repository supports 3.10, and CI runs the whole matrix,
# so covering 3.11+ is enough to catch drift on the pull request that causes it.
tomllib = pytest.importorskip("tomllib", reason="tomllib requires Python 3.11+")


def _requirement_lines(path: Path) -> list[str]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        lines.append(line)
    return lines


@pytest.fixture(scope="module")
def project() -> dict:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]


def test_requirements_txt_matches_the_project_dependencies(project: dict) -> None:
    declared = list(project["dependencies"])
    restated = _requirement_lines(REPOSITORY_ROOT / "requirements.txt")

    assert restated == declared, (
        "requirements.txt and pyproject.toml's [project.dependencies] have drifted. "
        "pyproject.toml is the source of truth; make requirements.txt match it."
    )


def test_requirements_dev_txt_matches_the_dev_extra(project: dict) -> None:
    declared = list(project["optional-dependencies"]["dev"])
    restated = _requirement_lines(REPOSITORY_ROOT / "requirements-dev.txt")

    assert restated == declared, (
        "requirements-dev.txt and pyproject.toml's dev extra have drifted. "
        "pyproject.toml is the source of truth; make requirements-dev.txt match it."
    )


def test_requirements_dev_txt_includes_the_runtime_requirements() -> None:
    """The dev file has to pull in the runtime one, or it installs half a tree."""
    text = (REPOSITORY_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "-r requirements.txt" in text


def test_supported_python_versions_match_the_compatibility_matrix(project: dict) -> None:
    """requires-python and the CI matrix have to agree on what is supported.

    A version in requires-python but not the matrix is claimed and never
    tested; a version in the matrix but not requires-python is tested and then
    refused at install time.
    """
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "quality.yml").read_text(
        encoding="utf-8"
    )
    matrix_line = next(
        line for line in workflow.splitlines() if line.strip().startswith("python-version: [")
    )
    tested = {
        part.strip().strip('"').strip("'")
        for part in matrix_line.split("[", 1)[1].rstrip("]").split(",")
    }

    requires = project["requires-python"]
    assert requires.startswith(">="), f"unexpected requires-python form: {requires}"
    minimum = tuple(int(part) for part in requires.removeprefix(">=").strip().split("."))

    assert minimum == (3, 10), "update this test if the supported floor moves"
    assert f"{minimum[0]}.{minimum[1]}" in tested, (
        f"requires-python claims {requires} but the CI matrix does not test it"
    )
    for version in tested:
        major, minor = (int(part) for part in version.split("."))
        assert (major, minor) >= minimum, (
            f"the CI matrix tests {version}, which requires-python ({requires}) refuses"
        )


def test_this_interpreter_is_one_the_project_claims_to_support(project: dict) -> None:
    """Catch a developer running the suite on an unsupported interpreter."""
    requires = project["requires-python"]
    minimum = tuple(int(part) for part in requires.removeprefix(">=").strip().split("."))

    assert sys.version_info[:2] >= minimum, (
        f"running on Python {sys.version_info.major}.{sys.version_info.minor}, "
        f"but pyproject.toml requires {requires}"
    )
