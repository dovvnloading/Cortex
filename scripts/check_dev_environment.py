"""Report where the installed environment disagrees with the declared pins.

CI installs from ``requirements-dev.lock.txt``, so it lints, tests and builds
against exactly what ``pyproject.toml`` resolves to. A developer's machine
drifts: a pin gets bumped in a pull request and nobody reinstalls. The result
is a green local run and a red CI run that the diff does not explain -- most
sharply for Ruff, where a different version simply has different rules.

It also prints advisories -- which never change the exit code -- for the
setups that drift quietly even when every pin matches: no virtual environment
(the interpreter's own site-packages, shared with every other project), an
interpreter other than the one the lock targets, and an import name of a
declared dependency that resolves to something that dependency did not
install.

Exit codes: 0 when everything satisfies its declared specifier, 1 otherwise
with the exact command to fix it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from importlib.machinery import PathFinder
from importlib.metadata import Distribution, PackageNotFoundError, distribution, version as installed_version
import inspect
import os
from pathlib import Path
import re
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = REPOSITORY_ROOT / "requirements-dev.lock.txt"

_LOCK_TARGET = re.compile(r"--python-version[ =](\d+)\.(\d+)")


def _fail(message: str) -> int:
    # Keep the advisories above the failure when stdout is a buffered pipe.
    sys.stdout.flush()
    print(message, file=sys.stderr)
    return 1


def lock_target(lock_file: Path = LOCK_FILE) -> tuple[int, int] | None:
    """The interpreter version the lock was compiled for, from its header."""

    try:
        with lock_file.open(encoding="utf-8") as stream:
            header = "".join(stream.readline() for _ in range(5))
    except OSError:
        return None
    match = _LOCK_TARGET.search(header)
    return (int(match.group(1)), int(match.group(2))) if match else None


def virtual_environment_advisory(prefix: str, base_prefix: str) -> str | None:
    if os.path.normcase(os.path.abspath(prefix)) != os.path.normcase(os.path.abspath(base_prefix)):
        return None
    return (
        f"No virtual environment is active: packages come from the interpreter at\n"
        f"    {base_prefix}, shared with every other project that uses it.\n"
        "    Create one with: python -m venv .venv; .venv\\Scripts\\Activate.ps1"
    )


def interpreter_advisory(running: tuple[int, int], target: tuple[int, int] | None) -> str | None:
    if target is None or running == target:
        return None
    return (
        f"This is Python {running[0]}.{running[1]}; the CI lock targets {target[0]}.{target[1]}.\n"
        "    Deprecations, resolver choices and wheels can differ between them."
    )


def _top_level_names(dist: Distribution) -> set[str]:
    """Import names a distribution provides, as its metadata declares or its files show."""

    declared = dist.read_text("top_level.txt")
    if declared:
        names = {line.strip() for line in declared.splitlines()}
    else:
        names = set()
        for file in dist.files or ():
            parts = file.parts
            if not parts or parts[0] == "..":
                continue
            names.add(parts[0] if len(parts) > 1 else (inspect.getmodulename(parts[0]) or ""))
    return {name for name in names if name.isidentifier() and name != "__pycache__"}


def _key(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def shadowing_advisories(names: Iterable[str], search_path: Sequence[str]) -> list[str]:
    """Import names of declared distributions that resolve to code they did not install.

    A directory or module earlier on the path, a second copy in another
    site-packages, or another distribution that writes the same top-level
    package all make ``import x`` load something other than what was pinned.
    Lookup only locates the module; nothing is imported or executed.
    """

    advisories: list[str] = []
    for name in names:
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue  # Already reported as not installed.
        if dist.files is None:
            continue  # No RECORD: nothing to compare the lookup against.
        root = Path(str(dist.locate_file("")))
        owned = {_key(root / file) for file in dist.files}
        for top in sorted(_top_level_names(dist)):
            spec = PathFinder.find_spec(top, list(search_path))
            if spec is None:
                continue
            if spec.origin is not None and spec.has_location:
                found = spec.origin
                ours = _key(found) in owned
            else:
                # A namespace package: several distributions may share it
                # legitimately, so it is ours if any portion is in our root.
                portions = list(spec.submodule_search_locations or ())
                found = os.pathsep.join(portions)
                ours = any(_key(Path(portion).parent) == _key(root) for portion in portions)
            if not ours:
                advisories.append(
                    f"`import {top}` loads {found},\n"
                    f"    which {dist.metadata['Name']} {dist.version} did not install: "
                    "something else on the path shadows it."
                )
    return advisories


def project_search_path() -> list[str]:
    """``sys.path`` as the tests and the app see it, not as this script does.

    Run as a script, the first entry is ``scripts/``; pytest and the launcher
    put the repository root and ``backend/`` there instead.
    """

    own_directory = _key(Path(__file__).resolve().parent)
    rest = [entry for entry in sys.path if _key(entry or os.curdir) != own_directory]
    return [str(REPOSITORY_ROOT), str(REPOSITORY_ROOT / "backend"), *rest]


def _print_advisories(advisories: Sequence[str]) -> None:
    if not advisories:
        return
    print("Advisories (these do not fail the check):")
    for advisory in advisories:
        print(f"  - {advisory}")
    print()


def main() -> int:
    advisories = [
        advisory
        for advisory in (
            virtual_environment_advisory(sys.prefix, sys.base_prefix),
            interpreter_advisory((sys.version_info[0], sys.version_info[1]), lock_target()),
        )
        if advisory is not None
    ]

    if sys.version_info < (3, 11):
        # tomllib is 3.11+. The check is advisory, so an older interpreter
        # simply skips it rather than failing a push it cannot evaluate.
        _print_advisories(advisories)
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

    advisories.extend(
        shadowing_advisories(
            (Requirement(raw).name for raw in requirements),
            project_search_path(),
        )
    )
    _print_advisories(advisories)

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
