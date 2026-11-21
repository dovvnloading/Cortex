"""Detect project checks from disposable source files; execute only via ShellTools.

Manifests are data, never an executable command supplied by a model. The
work-copy snapshot has bounded reads and refuses links/reparse points. Each
check is an independent approval-gated tool call so multiple approvals never
share a one-shot grant.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
from typing import Final, Literal
from xml.etree import ElementTree

from ..policy import classify
from ..protocol import ToolCall, ToolSpec
from ..sandbox.base import canonical_workcopy
from ..tool_registry import NeedsApproval, ToolContext, ToolRegistry, ToolResult
from ..workcopy import _snapshot
from cortex_backend.services.workspace_context import _is_reparse, _safe_root
from .shell import ShellTools

_CHECK_TIMEOUT_S: Final = 60.0
_JS_SCRIPTS: Final = ("test", "lint", "build", "typecheck")


@dataclass(frozen=True, slots=True)
class DetectedCheck:
    key: str
    command: str
    # Opaque project hooks can run code that the fixed command conceals.
    risk: Literal["safe", "ask", "network", "dangerous"] = "safe"


def _quoted(value: str) -> str:
    """Literal PowerShell argument; never interpolate a manifest path."""
    return "'" + value.replace("'", "''") + "'"


def _files(root: str | os.PathLike[str]) -> dict[str, bytes]:
    """Only content in the verified, bounded disposable work copy is visible."""
    # Refuse a swapped symlink/junction root before canonicalizing it. A
    # resolved outside directory must never become the detection input.
    requested = Path(root)
    verified = _safe_root(str(requested))
    source = _snapshot(canonical_workcopy(verified))
    return {name: item.contents for name, item in source.items()}


# Bundled Microsoft.NET.Sdk imports are part of the installed dotnet toolchain.
# Any other SDK name/version can resolve to arbitrary project-supplied targets.
_MSBUILD_BUNDLED_SDKS: Final = frozenset({"microsoft.net.sdk"})
_MSBUILD_OPAQUE_ELEMENTS: Final = frozenset({
    "import", "importgroup", "usingtask", "exec", "target",
    "projectreference", "packagereference", "packagedownload",
})
_MSBUILD_OPAQUE_PROPERTIES: Final = frozenset({
    "prebuildevent", "postbuildevent", "prelinkevent",
    "directorybuildpropspath", "directorybuildtargetspath",
    "msbuildprojectextensionspath", "baseintermediateoutputpath",
    "msbuildextensionspath",
})


def _msbuild_file_has_opaque_hooks(content: bytes) -> bool:
    """Classify executable/importing MSBuild XML without following any import.

    Built-in SDK syntax is normal for an otherwise plain dotnet project. The
    standard SDK may still load user-supplied Directory.Build files, generated
    project extensions and references; those are gated separately.
    """
    try:
        project = ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError, UnicodeError):
        return True
    if not isinstance(project.tag, str) or project.tag.rsplit("}", 1)[-1].casefold() != "project":
        return True
    for element in project.iter():
        if not isinstance(element.tag, str):
            continue
        tag = element.tag.rsplit("}", 1)[-1].casefold()
        if tag == "sdk":
            # The top-level <Sdk Name="Microsoft.NET.Sdk" /> form is another
            # spelling of the bundled SDK import, without a custom version.
            if (element not in project or set(element.attrib) != {"Name"}
                    or element.attrib["Name"].casefold() not in _MSBUILD_BUNDLED_SDKS):
                return True
            continue
        if (tag in _MSBUILD_OPAQUE_ELEMENTS or tag in _MSBUILD_OPAQUE_PROPERTIES
                or tag.endswith("dependson")
                or tag.startswith(("custombefore", "customafter")) and tag.endswith(("props", "targets"))):
            return True
        # Project Sdk="Microsoft.NET.Sdk" is an expected toolchain import.
        # Unknown SDKs, SDKs on other elements and expressions are not.
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].casefold()
            if name == "sdk" and (element is not project or value.casefold() not in _MSBUILD_BUNDLED_SDKS):
                return True
            if "$([" in value:
                return True
        if element.text and "$([" in element.text:
            return True
    return False


def _msbuild_project_directory(root: Path, project_path: str) -> Path | None:
    """Reject every non-snapshot path before doing even a metadata lookup."""
    if (not project_path or "\\" in project_path or ":" in project_path
            or any(part in {"", ".", ".."} for part in project_path.split("/"))):
        return None
    relative = PurePosixPath(project_path)
    if relative.is_absolute():
        return None
    directory = root.joinpath(*relative.parent.parts)
    return directory if directory.is_relative_to(root) else None


def _msbuild_file_present(path: Path) -> bool:
    """Existence, links and unreadability all require approval; never open."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _implicit_msbuild_import(root: Path, project_path: str) -> bool:
    """Check MSBuild auto-inputs along ancestors inside the work copy only.

    Directory.Build.* searches walk upward from a project and can include
    response files. MSBuild.rsp may also be read at the project or CLI cwd.
    We never inspect the contents or follow a link, and never inspect parents
    outside root. The launcher remains responsible for sandboxing execution.
    """
    directory = _msbuild_project_directory(root, project_path)
    if directory is None:
        return True
    project_dir = directory
    while directory.is_relative_to(root):
        try:
            info = directory.lstat()
        except OSError:
            return True
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            return True
        for filename in ("Directory.Build.props", "Directory.Build.targets", "Directory.Build.rsp"):
            if _msbuild_file_present(directory / filename):
                return True
        if directory == root:
            break
        directory = directory.parent
    return (_msbuild_file_present(project_dir / "MSBuild.rsp")
            or _msbuild_file_present(root / "MSBuild.rsp"))


def _msbuild_global_sdk_override(root: Path, files: dict[str, bytes]) -> bool:
    """An unscanned global.json or custom SDK resolution is not a trusted SDK."""
    if not _msbuild_file_present(root / "global.json"):
        return False
    content = files.get("global.json")
    if content is None:
        return True
    try:
        config = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return True
    if not isinstance(config, dict):
        return True
    sdk = config.get("sdk", {})
    return (not isinstance(sdk, dict) or "paths" in sdk
            or "msbuild-sdks" in config)


def _msbuild_generated_import(root: Path, project_path: str) -> bool:
    """SDK/MSBuild may glob generated *.props/targets under project obj/."""
    directory = _msbuild_project_directory(root, project_path)
    if directory is None:
        return True
    extension_dir = directory / "obj"
    try:
        info = extension_dir.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        return True
    prefix = PurePosixPath(project_path).name.casefold() + "."
    try:
        with os.scandir(extension_dir) as entries:
            return any(
                entry.name.casefold().startswith(prefix)
                and entry.name.casefold().endswith((".props", ".targets"))
                for entry in entries
            )
    except OSError:
        return True


# Only these options change pytest presentation/caching, not plugin discovery.
# -p no:cacheprovider disables the built-in cache plugin; other -p flags,
# --rootdir, and --override-ini must not gain approval by prefix matching.
_PYTEST_CONFIG_NAMES: Final = frozenset({"pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg"})
_SAFE_PYTEST_ADDOPTS: Final = frozenset({"", "-q", "-q -p no:cacheprovider"})


def _pytest_options_need_approval(path: str, content: bytes) -> bool:
    """Allow only exact inert addopts, with no other pytest configuration.

    A second key can load plugins or change which Python modules pytest
    imports (python_files, pythonpath, testpaths, required_plugins, etc.).
    Both native [tool.pytest] and legacy [tool.pytest.ini_options] are
    meaningful; malformed or ambiguous configuration must fail closed.
    """
    name = PurePosixPath(path).name.casefold()
    try:
        text = content.decode("utf-8-sig")
        if name == "pyproject.toml":
            parsed = tomllib.loads(text)
            tool = parsed.get("tool", {})
            if not isinstance(tool, dict):
                return True
            pytest_config = tool.get("pytest", {})
            if not isinstance(pytest_config, dict):
                return True
            if "ini_options" in pytest_config:
                # Mixed native and legacy pytest settings cannot be trusted.
                if set(pytest_config) != {"ini_options"}:
                    return True
                options = pytest_config["ini_options"]
            else:
                options = pytest_config
            if not isinstance(options, dict):
                return True
        else:
            parsed_ini = configparser.ConfigParser(interpolation=None, strict=True)
            parsed_ini.read_string(text)
            section = "tool:pytest" if name == "setup.cfg" else "pytest"
            options = dict(parsed_ini.items(section)) if parsed_ini.has_section(section) else {}
        if set(options) - {"addopts"}:
            return True
        addopts = options.get("addopts", "")
    except (configparser.Error, UnicodeError, ValueError, TypeError):
        return True
    return not (isinstance(addopts, str) and addopts.strip() in _SAFE_PYTEST_ADDOPTS)


def _unscanned_pytest_inputs(root: Path, files: dict[str, bytes]) -> bool:
    """Links/unreadable pytest configs or conftests are not harmless omissions.

    The source snapshot excludes linked/unsafe files. Check only metadata at
    known in-work-copy directories; never open or follow those entries.
    """
    directories = {PurePosixPath(".")}
    directories.update(PurePosixPath(path).parent for path in files)
    captured = {path.casefold() for path in files}
    for directory in directories:
        parent = root.joinpath(*directory.parts)
        try:
            info = parent.lstat()
        except OSError:
            return True
        if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            return True
        for name in (*_PYTEST_CONFIG_NAMES, "conftest.py"):
            relative = name if str(directory) == "." else f"{directory}/{name}"
            candidate = parent / name
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or relative.casefold() not in captured:
                return True
    return False


def workcopy_fingerprint(root: str | os.PathLike[str]) -> str:
    """Content (not mtime) identity, so a tool edit invalidates prior checks."""
    digest = sha256()
    for name, contents in sorted(_files(root).items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256(contents).digest())
    return digest.hexdigest()


def detect_checks(root: str | os.PathLike[str]) -> tuple[DetectedCheck, ...]:
    """Build only fixed, code-owned commands from work-copy manifest presence.

    JavaScript script *names* are allowlisted; script bodies are never turned
    into check commands. The sandbox must handle their execution as untrusted.
    """
    files = _files(root)
    checks: list[DetectedCheck] = []
    # The source snapshot intentionally skips linked or unreadable files.
    # A root-level pytest config/conftest that was skipped still needs to
    # produce an approval-gated check rather than silently disappearing.
    root_path = canonical_workcopy(_safe_root(str(Path(root))))
    has_pytest_files = any(
        PurePosixPath(p).name.casefold() in _PYTEST_CONFIG_NAMES | {"conftest.py"} for p in files
    ) or any(
        _msbuild_file_present(root_path / filename)
        for filename in _PYTEST_CONFIG_NAMES | {"conftest.py"}
    )
    if has_pytest_files:
        hooks = any(PurePosixPath(p).name.casefold() == "conftest.py" for p in files)
        opaque = _unscanned_pytest_inputs(root_path, files) or any(
            PurePosixPath(p).name.casefold() in _PYTEST_CONFIG_NAMES
            and _pytest_options_need_approval(p, body)
            for p, body in files.items()
        )
        checks.append(DetectedCheck("pytest", "python -m pytest", "ask" if hooks or opaque else "safe"))

    for path in sorted(files):
        item = PurePosixPath(path)
        directory = str(item.parent)
        if item.name == "package.json":
            try:
                manifest = json.loads(files[path].decode("utf-8-sig"))
            except (UnicodeError, ValueError):
                continue
            scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
            if not isinstance(scripts, dict):
                continue
            for name in _JS_SCRIPTS:
                script = scripts.get(name)
                if not isinstance(script, str) or not script.strip():
                    continue
                command = f"npm run {name}" if directory == "." else f"npm --prefix {_quoted(directory)} run {name}"
                # The launcher sees "npm run test", not the script body.
                # npm also executes lifecycle pre/post scripts for each target.
                # Classify explicit danger/network intent in all three before
                # the wrapper can gain automatic build/test permission.
                script_classes = []
                for phase in (f"pre{name}", name, f"post{name}"):
                    body = scripts.get(phase)
                    if isinstance(body, str) and body.strip():
                        script_classes.append(classify(body, "cmd", "appcontainer").cls)
                risk: Literal["safe", "network", "dangerous"] = "safe"
                if "dangerous" in script_classes:
                    risk = "dangerous"
                elif "network" in script_classes:
                    risk = "network"
                checks.append(DetectedCheck(f"npm:{name}:{path}", command, risk))
        elif item.name == "Cargo.toml":
            command = "cargo test" if directory == "." else f"cargo test --manifest-path {_quoted(path)}"
            build_hook = str(item.parent / "build.rs") in files or re.search(
                rb"(?m)^\s*build\s*=", files[path],
            ) is not None
            checks.append(DetectedCheck(f"cargo:{path}", command, "ask" if build_hook else "safe"))
        elif item.name == "go.mod":
            command = "go test ./..." if directory == "." else f"go -C {_quoted(directory)} test ./..."
            checks.append(DetectedCheck(f"go:{path}", command))

    # A solution already contains its projects; do not run every csproj again.
    solutions = sorted(path for path in files if path.casefold().endswith((".sln", ".slnx")))
    projects = sorted(path for path in files if path.casefold().endswith((".csproj", ".fsproj", ".vbproj")))
    root_path = canonical_workcopy(_safe_root(str(Path(root))))
    if solutions:
        # A solution can reference projects absent from the bounded snapshot,
        # and MSBuild can load Directory.Solution.* and solution targets.
        # Do not parse guessed project paths or treat an incomplete view as safe.
        for path in solutions:
            checks.append(DetectedCheck(f"dotnet:{path}", f"dotnet test {_quoted(path)}", "ask"))
    else:
        sdk_override = _msbuild_global_sdk_override(root_path, files)
        for path in projects:
            opaque = (
                sdk_override
                or _msbuild_file_has_opaque_hooks(files[path])
                or _implicit_msbuild_import(root_path, path)
                or _msbuild_generated_import(root_path, path)
            )
            checks.append(DetectedCheck(
                f"dotnet:{path}", f"dotnet test {_quoted(path)}", "ask" if opaque else "safe",
            ))
    return tuple(checks)


CHECK_TOOL: Final = ToolSpec(
    "run_checks",
    "Run a detected work-copy project check by index. Commands come only from local manifests, "
    "never model arguments; execution uses the policy-gated, network-disabled shell.",
    {
        "type": "object",
        "properties": {"index": {"type": "integer", "minimum": 0}},
        "required": [],
        "additionalProperties": False,
    },
)


class CheckTools:
    def __init__(self, workcopy: str | os.PathLike[str], shell: ShellTools) -> None:
        root = canonical_workcopy(Path(workcopy))
        if root != shell.workcopy:
            raise ValueError("checks and shell must use the same work copy")
        self.root = root
        self.shell = shell

    def handle(self, call: ToolCall, context: ToolContext) -> ToolResult | NeedsApproval:
        index = call.arguments.get("index", 0)
        if set(call.arguments) - {"index"} or type(index) is not int or index < 0:
            return ToolResult('{"status":"not_run","reason":"invalid_arguments"}', success=False)
        try:
            checks = detect_checks(self.root)
        except (OSError, ValueError, RuntimeError):
            return ToolResult('{"status":"not_run","reason":"workcopy_unavailable"}', success=False)
        if not checks:
            return ToolResult('{"status":"not_run","reason":"no_test_command_found"}')
        if index >= len(checks):
            return ToolResult('{"status":"not_run","reason":"invalid_index"}', success=False)
        check = checks[index]
        # Crucially, call the *same* ShellTools path as run_command. Its
        # classify/decide, one-shot approval and AppContainer launcher govern
        # every attempt; never start a process here or enable network.
        outcome = self.shell.handle(ToolCall(call.id, "run_command", {
            "command": check.command, "timeout_s": _CHECK_TIMEOUT_S,
            "network": False, "shell": "powershell",
        }), context, check_risk=check.risk)
        if isinstance(outcome, NeedsApproval):
            return NeedsApproval({**outcome.request, "tool": "run_checks"})
        try:
            detail = json.loads(outcome.content)
        except (ValueError, TypeError):
            detail = {}
        if not isinstance(detail, dict):
            detail = {}
        timed_out = detail.get("timed_out") is True
        exit_code = detail.get("exit_code")
        status = (
            "timed_out" if timed_out else "passed" if outcome.success and exit_code == 0
            else "failed" if isinstance(exit_code, int) else "not_run"
        )
        result = {
            "check": check.key, "status": status,
            "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            "timed_out": timed_out,
            "output": str(detail.get("output", ""))[:4096],
        }
        if status == "not_run":
            result["reason"] = str(detail.get("error", "shell_unavailable"))[:80]
        return ToolResult(
            json.dumps(result, ensure_ascii=False),
            summary="Project check passed" if status == "passed" else "Project check not passed",
            success=status == "passed",
        )


def register_check_tools(
    registry: ToolRegistry, workcopy: str | os.PathLike[str], shell: ShellTools,
) -> None:
    """Offer one code-owned check selector alongside the registered shell."""
    checks = CheckTools(workcopy, shell)
    registry.register(CHECK_TOOL, checks.handle)
