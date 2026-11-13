"""Run disjoint, file-level CI pytest shards and verify their coverage artifacts.

The normal local and Python-compatibility suites remain serial. GitHub Actions
runs four separate Windows processes; this avoids pytest-xdist's missing worker
coverage and keeps per-test fixture cleanup in each process.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def discover_test_files(root: Path) -> tuple[str, ...]:
    """Discover the same test_*.py modules that pyproject.toml lets pytest collect."""

    files = tuple(sorted(path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py") if path.is_file()))
    if not files:
        raise ValueError("No test_*.py modules found; refusing to report a passing empty suite")
    return files


def partition_test_files(files: Sequence[str], shard_count: int) -> tuple[tuple[str, ...], ...]:
    """Assign each test module once, with module counts balanced to within one.

    Sorting first makes the assignment deterministic across Windows and Linux.
    Nothing is excluded based on subsystem or an estimate of test importance.
    """

    if shard_count < 1 or len(files) < shard_count:
        raise ValueError("Shard count must be positive and no greater than the number of modules")
    if len(set(files)) != len(files):
        raise ValueError("Duplicate test-module paths")
    ordered = sorted(files)
    return tuple(tuple(ordered[i::shard_count]) for i in range(shard_count))


def verify_coverage_artifacts(root: Path, downloaded: Path, shard_count: int) -> int:
    """Fail closed on absent, unexpected, empty or stale shard artifacts.

    Each shard's file manifest is compared with the current checkout, not just
    counted. This catches a misconfigured matrix or a stale artifact download.
    """

    expected = partition_test_files(discover_test_files(root), shard_count)
    expected_dirs = {f"backend-shard-{i}" for i in range(1, shard_count + 1)}
    if not downloaded.is_dir():
        raise ValueError(f"Missing shard artifact directory: {downloaded}")
    actual_dirs = {p.name for p in downloaded.iterdir() if p.is_dir()}
    if actual_dirs != expected_dirs:
        raise ValueError(f"Shard artifacts differ: expected {sorted(expected_dirs)}, got {sorted(actual_dirs)}")

    total_tests = 0
    observed_tests: set[tuple[str, str]] = set()
    for index, expected_files in enumerate(expected, start=1):
        directory = downloaded / f"backend-shard-{index}"
        manifest = directory / f"backend-shard-{index}.txt"
        if not manifest.is_file() or manifest.read_text(encoding="utf-8").splitlines() != list(expected_files):
            raise ValueError(f"Shard {index} file manifest does not match the test modules in this checkout")
        data_files = [p for p in directory.glob(".coverage.*") if p.is_file()]
        if not data_files or any(p.stat().st_size == 0 for p in data_files):
            raise ValueError(f"Shard {index} is missing a nonempty coverage database")
        junit = directory / f"backend-shard-{index}.xml"
        if not junit.is_file():
            raise ValueError(f"Shard {index} is missing its pytest JUnit report")
        try:
            reports = ET.parse(junit).getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Shard {index} JUnit XML is malformed") from exc
        cases = reports.findall(".//testcase")
        if not cases:
            raise ValueError(f"Shard {index} ran zero tests")
        for case in cases:
            identity = (case.get("classname", ""), case.get("name", ""))
            if identity in observed_tests:
                raise ValueError(f"Test repeated across shards: {identity}")
            observed_tests.add(identity)
        total_tests += len(cases)
    return total_tests


def run_shard(root: Path, shard_index: int, shard_count: int) -> int:
    if not 1 <= shard_index <= shard_count:
        raise ValueError(f"Shard index {shard_index} is outside 1..{shard_count}")
    files = partition_test_files(discover_test_files(root), shard_count)[shard_index - 1]
    manifest = root / f"backend-shard-{shard_index}.txt"
    manifest.write_text("\n".join(files) + "\n", encoding="utf-8")
    print(f"Backend test shard {shard_index}/{shard_count}: {len(files)} modules", flush=True)
    # Use the same coverage source and branch configuration as the old serial
    # job. -p creates a .coverage.* file that can be safely joined later.
    command = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        "-p",
        "-m",
        "pytest",
        "--durations=20",
        f"--junitxml=backend-shard-{shard_index}.xml",
        *files,
    ]
    return subprocess.run(command, cwd=root, check=False).returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--shard-index", type=int, required=True)
    run_parser.add_argument("--shard-count", type=int, required=True)
    verify_parser = commands.add_parser("verify-coverage")
    verify_parser.add_argument("--shard-count", type=int, required=True)
    verify_parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_shard(REPOSITORY_ROOT, args.shard_index, args.shard_count)
        total = verify_coverage_artifacts(REPOSITORY_ROOT, args.directory, args.shard_count)
        print(f"Verified all {args.shard_count} backend shards: {total} tests, complete file coverage", flush=True)
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(2, f"CI backend sharding error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
