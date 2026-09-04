"""Report where the installed environment disagrees with the declared pins.

CI installs from ``requirements-dev.lock.txt``, so it lints, tests and builds
against exactly what ``pyproject.toml`` resolves to. A developer's machine
drifts: a pin gets bumped in a pull request and nobody reinstalls. The result
is a green local run and a red CI run that the diff does not explain -- most
sharply for Ruff, where a different version simply has different rules.

Exit codes: 0 when everything satisfies its declared specifier, 1 otherwise
with the exact command to fix it.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as installed_version
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main() -> int:
    if sys.version_info < (3, 11):
        # tomllib is 3.11+. The check is advisory, so an older interpreter
        # simply skips it rather than failing a push it cannot evaluate.
        print("Python < 3.11: skipping the pin check (tomllib is unavailable).")
        return 0

    import tomllib
    from packaging.requirements import Requirement

    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]

    requirements = [
        *project.get("dependencies", []),
        *project.get("optional-dependencies", {}).get("dev", []),
    ]

    mismatches: list[str] = []
    missing: list[str] = []
    for raw in requirements:
        requirement = Requirement(raw)
        try:
            found = installed_version(requirement.name)
        except PackageNotFoundError:
            missing.append(f"  {requirement.name}: not installed (declared {raw})")
            continue
        if requirement.specifier and not requirement.specifier.contains(found, prereleases=True):
            mismatches.append(f"  {requirement.name}: installed {found}, declared {raw}")

    if not mismatches and not missing:
        print(f"All {len(requirements)} declared dependencies satisfy their pins.")
        return 0

    lines = ["The installed environment does not match pyproject.toml."]
    if mismatches:
        lines.append("")
        lines.append("Version mismatches:")
        lines.extend(mismatches)
    if missing:
        lines.append("")
        lines.append("Not installed:")
        lines.extend(missing)
    lines.append("")
    lines.append("CI installs from the lock, so it will not see what you see here. Fix with:")
    lines.append("  python -m pip install -r requirements-dev.lock.txt")
    return _fail("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
