"""Conventions of the repository's own tooling: hooks, gate scripts and workflows.

None of these change what Cortex does. They pin down things that are easy to
break without any test noticing -- a hook that Git silently ignores, a script
that calls the tool its own workaround was written to avoid, a linter that
checks fewer paths in CI than on a laptop -- and each one had drifted before it
was written down here.

The checks read the files as text on purpose: parsing YAML or PowerShell would
need a dependency the project does not otherwise carry, and the properties
asserted are all visible in the text.
"""

from __future__ import annotations

from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
NO_SHA = "0" * 40
SOME_SHA = "a" * 40


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ruff_paths(text: str) -> list[str]:
    """The paths of every ``python -m ruff check`` invocation in ``text``."""

    found = re.findall(r"python -m ruff check ([^\r\n]+)", text)
    assert found, "no ruff invocation found"
    return [paths.strip() for paths in found]


def _job_block(workflow: str, job: str) -> str:
    """The text of one top-level job, from its key to the next job or the end."""

    match = re.search(rf"^  {re.escape(job)}:\n(.*?)(?=^  [a-z0-9_-]+:\n|\Z)", workflow, re.DOTALL | re.MULTILINE)
    assert match, f"no job named {job!r}"
    return match.group(1)


# -- Git hooks ---------------------------------------------------------------


def test_git_hooks_are_executable() -> None:
    """Git ignores a hook that is not executable, so ``core.hooksPath`` would do nothing.

    The mode that matters is the one in the index: it is what a clone on macOS
    or Linux gets, whatever the file system of the machine that committed it.
    """

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    listing = subprocess.run(
        [git, "ls-files", "-s", "--", ".githooks"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        pytest.skip("not a git checkout")

    entries = [line.split(None, 3) for line in listing.stdout.splitlines() if line.strip()]
    assert entries, "no tracked files under .githooks"
    not_executable = [entry[3].strip() for entry in entries if entry[0] != "100755"]
    assert not not_executable, (
        f"tracked hooks without the executable bit: {not_executable}. "
        "Fix with: git update-index --chmod=+x <file>"
    )


def _posix_shell() -> str | None:
    """A ``sh`` to run the hook with, including the one Git for Windows ships.

    Git for Windows runs hooks with its own bundled shell, which is often not on
    the ``PATH`` of the process running the tests, so look beside ``git`` too.
    """

    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if git is None:
        return None
    installation = Path(git).resolve().parent.parent
    for candidate in (installation / "bin" / "sh.exe", installation / "usr" / "bin" / "sh.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def _hook_environment(tmp_path: Path, *, exit_code: int) -> tuple[dict[str, str], Path]:
    """An environment whose ``pwsh`` is a stub that records how it was called."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "pwsh-arguments.txt"
    stub = bin_dir / "pwsh"
    stub.write_text('#!/bin/sh\necho "$@" > "$PWSH_RECORD"\nexit "$PWSH_EXIT"\n', encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "PWSH_RECORD": str(record),
        "PWSH_EXIT": str(exit_code),
    }
    environment.pop("CORTEX_SKIP_HOOK", None)
    return environment, record


def _run_hook(
    tmp_path: Path,
    refs: str,
    *,
    exit_code: int = 0,
    extra_environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    shell = _posix_shell()
    if shell is None:
        pytest.skip("no POSIX shell to run the hook with")
    environment, record = _hook_environment(tmp_path, exit_code=exit_code)
    environment.update(extra_environment or {})
    result = subprocess.run(
        [shell, ".githooks/pre-push"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        input=refs,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, record


def _push_line(local_ref: str, local_sha: str, remote_ref: str, remote_sha: str = NO_SHA) -> str:
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def test_pre_push_runs_the_quick_tier_for_a_branch_push(tmp_path: Path) -> None:
    result, record = _run_hook(tmp_path, _push_line("refs/heads/topic", SOME_SHA, "refs/heads/topic"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert record.exists(), "the hook did not run the check script"
    assert "-Tier quick" in record.read_text(encoding="utf-8")


def test_pre_push_blocks_the_push_when_the_checks_fail(tmp_path: Path) -> None:
    result, record = _run_hook(
        tmp_path, _push_line("refs/heads/topic", SOME_SHA, "refs/heads/topic"), exit_code=1
    )

    assert record.exists()
    assert result.returncode == 1
    assert "push aborted" in result.stdout


def test_pre_push_skips_a_push_of_only_tags(tmp_path: Path) -> None:
    result, record = _run_hook(tmp_path, _push_line("refs/tags/v1.0.0", SOME_SHA, "refs/tags/v1.0.0"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not record.exists(), "a tag push ran the whole quick tier"
    assert "skipping" in result.stdout


def test_pre_push_skips_a_push_of_only_deletions(tmp_path: Path) -> None:
    result, record = _run_hook(tmp_path, _push_line("(delete)", NO_SHA, "refs/heads/old", SOME_SHA))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not record.exists(), "a branch deletion ran the whole quick tier"


def test_pre_push_still_checks_a_push_that_carries_a_branch_alongside_a_tag(tmp_path: Path) -> None:
    refs = _push_line("refs/tags/v1.0.0", SOME_SHA, "refs/tags/v1.0.0") + _push_line(
        "refs/heads/topic", SOME_SHA, "refs/heads/topic"
    )
    result, record = _run_hook(tmp_path, refs)

    assert result.returncode == 0, result.stdout + result.stderr
    assert record.exists(), "a branch pushed together with a tag was not checked"


def test_pre_push_treats_an_empty_ref_list_as_a_reason_to_check(tmp_path: Path) -> None:
    """Fail closed: nothing on stdin must never read as 'nothing to verify'."""

    result, record = _run_hook(tmp_path, "")

    assert result.returncode == 0, result.stdout + result.stderr
    assert record.exists()


def test_cortex_skip_hook_skips_even_a_branch_push(tmp_path: Path) -> None:
    result, record = _run_hook(
        tmp_path,
        _push_line("refs/heads/topic", SOME_SHA, "refs/heads/topic"),
        extra_environment={"CORTEX_SKIP_HOOK": "1"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not record.exists()
    assert "CORTEX_SKIP_HOOK" in result.stdout


# -- check.ps1 ----------------------------------------------------------------


def test_check_script_never_calls_npm_or_npx_directly() -> None:
    """``npm.cmd`` and ``npx.cmd`` exist so the script works when the execution
    policy blocks their ``.ps1`` shims; a bare call goes through the shim again.
    """

    script = _read(REPOSITORY_ROOT / "scripts" / "check.ps1")
    offenders = []
    for number, raw in enumerate(script.splitlines(), start=1):
        code = raw.split("#", 1)[0]
        if re.match(r"^\s*(?:&\s+)?(?:npm|npx)(?:\s|$)", code):
            offenders.append(f"line {number}: {raw.strip()}")

    assert not offenders, f"check.ps1 invokes npm/npx without its .cmd shim workaround: {offenders}"
    assert "$npx" in script, "check.ps1 no longer defines $npx"


# -- Linting and type checking cover the same code ---------------------------


def test_ci_and_check_script_lint_the_same_paths() -> None:
    ci = _ruff_paths(_read(WORKFLOWS / "quality.yml"))
    local = _ruff_paths(_read(REPOSITORY_ROOT / "scripts" / "check.ps1"))

    assert len(set(ci)) == 1, f"quality.yml lints different paths in different places: {ci}"
    assert len(set(local)) == 1, f"check.ps1 lints different paths in different places: {local}"
    assert set(ci[0].split()) == set(local[0].split()), (
        f"CI lints {ci[0]!r} but check.ps1 lints {local[0]!r}"
    )
    assert "scripts" in ci[0].split(), "scripts/ is not linted"


def test_the_type_checker_covers_every_python_tree_ruff_lints_except_tests() -> None:
    tomllib = pytest.importorskip("tomllib", reason="tomllib requires Python 3.11+")
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)
    checked = set(config["tool"]["mypy"]["files"])
    linted = set(_ruff_paths(_read(WORKFLOWS / "quality.yml"))[0].split())

    # Test modules are deliberately untyped fixtures and helpers; the backend
    # package is checked by its own path.
    expected = (linted - {"tests"}) - {"backend"} | {"backend/cortex_backend"}
    missing = expected - checked
    assert not missing, f"ruff lints these but mypy does not check them: {sorted(missing)}"
    assert config["tool"]["mypy"].get("explicit_package_bases") is True


# -- Workflows ---------------------------------------------------------------


def test_superseded_pull_request_runs_are_cancelled_but_main_runs_are_not() -> None:
    workflow = _read(WORKFLOWS / "quality.yml")
    header = workflow.split("\njobs:\n", 1)[0]

    assert re.search(r"^concurrency:\n  group: .*github\.event_name == 'pull_request'", header, re.MULTILINE)
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in header


def test_packaging_waits_for_every_gate() -> None:
    workflow = _read(WORKFLOWS / "quality.yml")
    heavy = _job_block(workflow, "heavy")
    needs = re.search(r"needs: \[([^\]]*)\]", heavy)

    assert needs, "the heavy job has no needs list"
    gates = {name.strip() for name in needs.group(1).split(",")}
    assert {"lint", "backend", "frontend", "e2e"} <= gates


def test_every_gate_is_skipped_together_for_a_document_only_change() -> None:
    """Skipped jobs report success, so a required check is never left pending."""

    workflow = _read(WORKFLOWS / "quality.yml")
    for job in ("lint", "backend", "frontend", "e2e"):
        block = _job_block(workflow, job)
        assert "needs: changes" in block, f"{job} does not wait for the change classification"
        assert "if: needs.changes.outputs.code == 'true'" in block, f"{job} ignores the classification"
    assert not re.search(r"^\s*paths(-ignore)?:", workflow, re.MULTILINE), (
        "a paths filter on the trigger leaves required checks pending; use the changes job"
    )


def test_pull_requests_do_not_run_the_interpreter_matrix() -> None:
    quality = _read(WORKFLOWS / "quality.yml")
    compatibility = _read(WORKFLOWS / "python-compatibility.yml")
    triggers = compatibility.split("\njobs:\n", 1)[0]

    assert "python-version: [" not in quality, "the interpreter matrix belongs in python-compatibility.yml"
    assert "pull_request" not in triggers
    assert "schedule:" in triggers and "push:" in triggers and "workflow_dispatch:" in triggers
    assert "python -m ruff" not in compatibility


def test_the_locked_interpreter_is_not_repeated_in_the_matrix() -> None:
    quality = _read(WORKFLOWS / "quality.yml")
    compatibility = _read(WORKFLOWS / "python-compatibility.yml")
    locked = set(re.findall(r'python-version: "(\d+\.\d+)"', quality))
    matrix_line = next(line for line in compatibility.splitlines() if line.strip().startswith("python-version: ["))
    matrix = {part.strip().strip('"') for part in matrix_line.split("[", 1)[1].rstrip("]").split(",")}

    assert locked == {"3.11"}
    assert not (locked & matrix), f"the matrix repeats the locked run: {sorted(locked & matrix)}"


def test_every_workflow_action_is_pinned_to_a_commit_with_its_version() -> None:
    pinned = re.compile(r"uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d")
    unpinned = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(_read(workflow).splitlines(), start=1):
            stripped = line.strip().lstrip("- ")
            if stripped.startswith("uses: ") and not stripped.startswith("uses: ./") and not pinned.search(stripped):
                unpinned.append(f"{workflow.name}:{number}: {stripped}")

    assert not unpinned, f"actions not pinned to a commit with a version comment: {unpinned}"


# -- Coverage ------------------------------------------------------------------


def test_coverage_is_measured_with_branches_and_has_a_floor() -> None:
    tomllib = pytest.importorskip("tomllib", reason="tomllib requires Python 3.11+")
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)
    coverage = config["tool"]["coverage"]

    assert coverage["run"]["branch"] is True
    assert coverage["run"]["source"] == ["backend/cortex_backend"]
    assert coverage["report"]["fail_under"] >= 78.4, "the coverage floor only ratchets up"
    assert any(dependency.startswith("coverage[toml]") for dependency in config["project"]["optional-dependencies"]["dev"])


def test_the_backend_job_runs_the_suite_under_coverage_and_keeps_the_report() -> None:
    backend = _job_block(_read(WORKFLOWS / "quality.yml"), "backend")

    assert "python -m coverage run -m pytest" in backend
    assert "python -m coverage report" in backend
    assert "python -m coverage xml" in backend
    assert "actions/upload-artifact@" in backend
