"""Prove sharded CI cannot silently omit, duplicate or reuse test results."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from tools import ci_backend_shards as shards


def test_shards_cover_every_module_exactly_once_and_balance_counts() -> None:
    modules = [f"tests/test_{number:03d}.py" for number in range(53)]
    partitions = shards.partition_test_files(list(reversed(modules)), 4)
    assert len(partitions) == 4
    assert sorted(path for group in partitions for path in group) == modules
    assert max(len(group) for group in partitions) - min(len(group) for group in partitions) <= 1
    assert partitions == shards.partition_test_files(modules, 4)


@pytest.mark.parametrize("files,count", [([], 4), (["tests/test_a.py"], 2), (["tests/test_a.py"], 0), (["x", "x"], 1)])
def test_invalid_shard_plans_fail_closed(files: list[str], count: int) -> None:
    with pytest.raises(ValueError):
        shards.partition_test_files(files, count)


def test_discovery_includes_nested_property_modules_but_not_helpers(tmp_path: Path) -> None:
    (tmp_path / "tests" / "property").mkdir(parents=True)
    for file in ("tests/test_basic.py", "tests/property/test_property.py", "tests/helper.py"):
        p = tmp_path / file
        p.write_text("", encoding="utf-8")
    assert shards.discover_test_files(tmp_path) == ("tests/property/test_property.py", "tests/test_basic.py")


def _write_shard_artifact(tmp_path: Path, index: int, paths: tuple[str, ...], *, name: str) -> None:
    folder = tmp_path / "artifacts" / f"backend-shard-{index}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"backend-shard-{index}.txt").write_text("\n".join(paths) + "\n", encoding="utf-8")
    (folder / f"backend-shard-{index}.xml").write_text(
        f'<testsuites><testsuite tests="1"><testcase classname="tests.shard{index}" name="{name}" /></testsuite></testsuites>',
        encoding="utf-8",
    )
    (folder / f".coverage.run{index}").write_bytes(b"coverage-data")


def test_coverage_validation_rejects_missing_files_and_duplicate_testcases(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_a.py", "test_b.py"):
        (tests / name).touch()
    parts = shards.partition_test_files(shards.discover_test_files(tmp_path), 2)
    for idx, names in enumerate(parts, start=1):
        _write_shard_artifact(tmp_path, idx, names, name=f"test_{idx}")
    artifacts = tmp_path / "artifacts"
    assert shards.verify_coverage_artifacts(tmp_path, artifacts, 2) == 2
    (artifacts / "backend-shard-2" / "backend-shard-2.txt").write_text("wrong\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        shards.verify_coverage_artifacts(tmp_path, artifacts, 2)
    _write_shard_artifact(tmp_path, 2, parts[1], name="test_2")
    (artifacts / "backend-shard-2" / ".coverage.run2").unlink()
    with pytest.raises(ValueError, match="coverage"):
        shards.verify_coverage_artifacts(tmp_path, artifacts, 2)
    _write_shard_artifact(tmp_path, 2, parts[1], name="test_2")
    (artifacts / "backend-shard-2" / "backend-shard-2.xml").write_text(
        '<testsuites><testsuite><testcase classname="tests.shard1" name="test_1" /></testsuite></testsuites>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repeated"):
        shards.verify_coverage_artifacts(tmp_path, artifacts, 2)


def test_run_shard_passes_only_assigned_modules_to_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py"):
        (tmp_path / "tests" / name).touch()
    captured: dict[str, object] = {}

    def fake_run(command: list[str], *, cwd: Path, check: bool) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, cwd=cwd, check=check)
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr(shards.subprocess, "run", fake_run)
    assert shards.run_shard(tmp_path, 2, 2) == 17
    assert captured["cwd"] == tmp_path
    cmd = captured["command"]
    assert isinstance(cmd, list)
    assert cmd[:6] == [sys.executable, "-m", "coverage", "run", "-p", "-m"]
    assert cmd[-2:] == ["tests/test_b.py", "tests/test_d.py"]
    assert (tmp_path / "backend-shard-2.txt").read_text(encoding="utf-8") == "tests/test_b.py\ntests/test_d.py\n"
    with pytest.raises(ValueError, match="outside"):
        shards.run_shard(tmp_path, 3, 2)
