"""The repository's own documents must stay true to the repository.

These are documents people (and agents) follow literally: ``AGENTS.md`` says it
is the only normative contract, ``CONTRIBUTING.md`` tells a newcomer which
commands to run, the issue forms decide what a bug report contains. Each one
had drifted from the repository it describes -- a command CI does not run, a
directory a clone does not contain, a template asking about smartphones -- so
the claims that can be checked without a network are checked here.

Claims about GitHub's own settings (branch protection, merge methods) cannot be
tested offline and are deliberately not asserted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cortex_backend.llamacpp import binary_fetcher

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Commands a document may show that no CI step runs, because they change files
# rather than verify them.
_DOCUMENTED_ONLY_COMMANDS = frozenset({"python tools/generate_contracts.py --write"})

_CHECKED_COMMAND = re.compile(r"^(python -m (ruff|mypy|pytest|compileall)\b|python tools/|npm )")


def _read(relative: str) -> str:
    return (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")


def _fenced_powershell_commands(markdown: str) -> list[str]:
    """Command lines inside ```powershell fences, minus comments and blanks."""

    commands: list[str] = []
    inside = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            inside = line == "```powershell"
            continue
        if not inside or not line or line.startswith("#"):
            continue
        # Drop a trailing `# comment`, which is prose rather than the command.
        commands.append(re.sub(r"\s+#\s.*$", "", line).strip())
    return commands


def _gate_text() -> str:
    """Everything CI and ``check.ps1`` run, with ``check.ps1``'s npm shim undone."""

    workflow = _read(".github/workflows/quality.yml")
    script = _read("scripts/check.ps1").replace("& $npm ", "npm ")
    return workflow + "\n" + script


# -- Change_Log.md -----------------------------------------------------------


def test_changelog_leads_with_an_unreleased_section_and_dates_every_version() -> None:
    headings = [line for line in _read("Change_Log.md").splitlines() if line.startswith("## ")]

    assert headings, "Change_Log.md has no second-level headings"
    assert headings[0] == "## [Unreleased]", "the newest changes belong in an [Unreleased] section at the top"
    for heading in headings[1:]:
        assert heading == "## Earlier entries" or re.fullmatch(
            r"## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}", heading
        ), f"unexpected changelog heading: {heading!r}"


def test_the_unreleased_section_is_grouped_and_names_its_changes() -> None:
    text = _read("Change_Log.md")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## ", 1)[0]

    for group in ("### Added", "### Changed", "### Fixed", "### Removed"):
        assert group in unreleased, f"[Unreleased] has no {group!r} group"
    assert len(re.findall(r"\(#\d+\)", unreleased)) >= 40, (
        "[Unreleased] should cite the pull requests behind the changes since 2026-09-04"
    )


def test_the_changelog_covers_the_managed_llamacpp_runtime() -> None:
    text = _read("Change_Log.md")

    assert "llama.cpp" in text and "GGUF" in text, "the local GGUF runtime has no changelog entry"


# -- AGENTS.md and CONTRIBUTING.md -------------------------------------------


@pytest.mark.parametrize("document", ["AGENTS.md", "CONTRIBUTING.md"])
def test_documented_commands_are_the_ones_the_gates_run(document: str) -> None:
    gates = _gate_text()
    unknown = [
        command
        for command in _fenced_powershell_commands(_read(document))
        if _CHECKED_COMMAND.match(command)
        and command not in _DOCUMENTED_ONLY_COMMANDS
        and command not in gates
    ]

    assert not unknown, f"{document} shows commands neither quality.yml nor check.ps1 runs: {unknown}"


def test_agents_lists_every_python_gate_check_ps1_runs() -> None:
    agents = _read("AGENTS.md")
    gates = [
        line.strip()
        for line in _read("scripts/check.ps1").splitlines()
        if re.match(r"^\s+python (-m (ruff|mypy|pytest)\b|tools/)", line)
    ]

    assert gates, "no gate commands found in scripts/check.ps1"
    missing = [gate for gate in gates if gate not in agents]
    assert not missing, f"AGENTS.md omits gates that check.ps1 runs: {missing}"


def test_agents_repository_map_names_only_paths_a_clone_contains() -> None:
    paths = re.findall(r"^- `([^`]+)`:", _read("AGENTS.md"), flags=re.MULTILINE)

    assert paths, "no repository map found in AGENTS.md"
    absent = [path for path in paths if not (REPOSITORY_ROOT / path).exists()]
    assert not absent, f"AGENTS.md names paths a fresh clone does not contain: {absent}"


def test_agents_does_not_depend_on_a_directory_a_clone_lacks() -> None:
    agents = _read("AGENTS.md")

    assert "Anything under `docs/` is background" not in agents, (
        "docs/ is untracked and absent from a clone; AGENTS.md cannot describe it as if it were there"
    )


def test_agents_does_not_name_an_agent_specific_tool() -> None:
    assert "apply_patch" not in _read("AGENTS.md")


def test_the_pull_request_template_has_the_headings_agents_md_requires() -> None:
    template = (REPOSITORY_ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    headings = {line[3:].strip() for line in template.splitlines() if line.startswith("## ")}

    for required in (
        "Problem",
        "Root cause",
        "Change",
        "Compatibility and rollback",
        "Checks",
        "Security, data loss and concurrency",
        "Limits",
    ):
        assert required in headings, f"pull_request_template.md has no {required!r} heading"


def test_contributing_documents_how_to_bump_the_llamacpp_pin() -> None:
    assert "pin_llamacpp_release.py" in _read("CONTRIBUTING.md")


def test_the_model_tag_advice_lives_in_one_place() -> None:
    tag = re.compile(r"\b[a-z][a-z0-9.-]*:\d+(?:\.\d+)?b\b")
    readme_pull = re.search(r"^ollama pull (\S+)$", _read("README.md"), flags=re.MULTILINE)

    assert readme_pull is not None, "README's quick start no longer names a model to pull"
    assert not tag.findall(_read("CONTRIBUTING.md")), (
        "CONTRIBUTING.md names its own model tag; it should point at the README's instead"
    )
    paragraphs = re.split(r"\n\s*\n", _read("CONTRIBUTING.md"))
    advice = next(paragraph for paragraph in paragraphs if "smoke checks" in paragraph)
    assert "README" in advice, "the smoke-check model advice should point at the README's quick start"


# -- Issue forms -------------------------------------------------------------


def test_issue_forms_replace_the_boilerplate_templates() -> None:
    directory = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE"

    assert sorted(path.name for path in directory.iterdir()) == [
        "bug_report.yml",
        "config.yml",
        "feature_request.yml",
    ]


def test_issue_forms_ask_about_a_windows_desktop_app() -> None:
    directory = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE"
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(directory.iterdir()))

    for boilerplate in ("iOS", "iPhone", "Smartphone", "stock browser"):
        assert boilerplate not in text, f"issue forms still carry GitHub boilerplate: {boilerplate!r}"

    bug = (directory / "bug_report.yml").read_text(encoding="utf-8")
    for needed in ("Windows", "Ollama", "GGUF", "GPU", "startup.log", "Model"):
        assert needed in bug, f"bug form does not ask about {needed!r}"
    assert "prompts" in bug and "memories" in bug, "bug form must remind reporters not to paste private data"


def test_issue_config_disables_blank_issues_and_routes_security_reports_privately() -> None:
    config = (REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(encoding="utf-8")

    assert re.search(r"^blank_issues_enabled:\s*false\s*$", config, flags=re.MULTILINE)
    assert "https://github.com/dovvnloading/Cortex/security/advisories/new" in config


def test_issue_forms_are_well_formed() -> None:
    directory = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE"
    for name in ("bug_report.yml", "feature_request.yml"):
        text = (directory / name).read_text(encoding="utf-8")
        assert text.startswith("name: "), f"{name} does not start with a form name"
        for key in ("description:", "body:", "type: textarea", "validations:"):
            assert key in text, f"{name} lacks {key!r}"
        assert "\t" not in text, f"{name} contains a tab, which YAML forbids for indentation"


# -- SECURITY.md and the README's isolation wording --------------------------


def test_security_policy_does_not_promise_a_preceding_release() -> None:
    text = re.sub(r"\s+", " ", _read("SECURITY.md"))

    assert "preceding stable release" not in text
    assert "only the latest release is supported" in text


def _code_worker_entry_statements() -> list[ast.stmt]:
    source = _read("backend/cortex_backend/execution/code_execution.py")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "code_worker_main":
            return node.body
    raise AssertionError("code_worker_main not found")


def test_the_code_worker_clears_its_environment_before_anything_else() -> None:
    """The README says the worker clears its inherited environment. It does so first."""

    (try_statement,) = _code_worker_entry_statements()
    assert isinstance(try_statement, ast.Try)
    first = try_statement.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name) and first.value.func.id == "_scrub_worker_environment"


def test_the_readme_describes_environment_clearing_rather_than_non_inheritance() -> None:
    readme = re.sub(r"\s+", " ", _read("README.md"))

    # Windows spawn hands the child the parent's whole environment block; the
    # worker clears it at start-up. "Not inherited" is stronger than that.
    assert "are not inherited" not in readme
    assert "clears the environment variables it inherited" in readme


# -- Module documentation ----------------------------------------------------


def _all_docstrings(source: str) -> str:
    """The module docstring plus every class and function docstring."""

    tree = ast.parse(source)
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef)))]
    return " ".join(ast.get_docstring(node) or "" for node in nodes)


def test_binary_release_docstrings_name_a_helper_that_exists() -> None:
    from cortex_backend.llamacpp import binary_release

    docstrings = _all_docstrings(Path(binary_release.__file__).read_text(encoding="utf-8"))
    referenced = re.findall(r"binary_fetcher\.(\w+)", docstrings)

    assert referenced, "binary_release no longer points at the directory-hash helper"
    for name in referenced:
        assert hasattr(binary_fetcher, name), f"binary_release cites binary_fetcher.{name}, which does not exist"


def test_pin_tool_docstring_says_what_it_hashes() -> None:
    source = (REPOSITORY_ROOT / "tools" / "pin_llamacpp_release.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source)) or ""

    # The pinned trust anchor is the whole extracted directory (hash_directory),
    # not the entry-point executable on its own.
    assert "hash_directory(extract_dir)" in source
    assert "separately" not in docstring
    assert "extracted directory" in docstring.replace("\n", " ")


def test_llamacpp_chat_client_docstring_matches_its_streaming() -> None:
    from cortex_backend.llamacpp import chat_client

    docstring = ast.get_docstring(ast.parse(Path(chat_client.__file__).read_text(encoding="utf-8"))) or ""

    assert "never actually streams" not in docstring
    assert "stream: false" not in docstring
