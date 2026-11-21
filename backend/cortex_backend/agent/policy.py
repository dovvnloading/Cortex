"""Conservative, side-effect-free command policy for a *future* agent shell.

This module does not execute commands or inspect the host. It describes command
intent, not proof of containment: a caller must independently attest the actual
sandbox level and canonicalize a project root before hashing it for rules.
Unknown or ambiguous syntax never receives automatic permission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ntpath
import re
from typing import Literal

CommandClass = Literal["inspect", "build_test", "mutate_workcopy", "network", "dangerous", "ask"]
Decision = Literal["allow", "ask", "refuse"]
Mode = Literal["plan_only", "ask", "auto_sandboxed"]

# Higher values dominate a chain. Unrecognized syntax is more restrictive
# than known local edits, but an explicit network/dangerous operation wins.
_RANK: dict[CommandClass, int] = {
    "inspect": 0,
    "build_test": 1,
    "mutate_workcopy": 2,
    "ask": 3,
    "network": 4,
    "dangerous": 5,
}

_TEXT: dict[CommandClass, str] = {
    "inspect": "Reads project information without intentionally changing files.",
    "build_test": "Runs project checks that may change files in the work copy.",
    "mutate_workcopy": "Changes project files and needs your approval.",
    "network": "May connect to the internet or install software and needs your approval.",
    "dangerous": "Could access private information or change sensitive system settings and is blocked.",
    "ask": "Cannot be safely identified and needs your approval.",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """Result of classification; private fields bind approval to parsed tokens."""

    cls: CommandClass
    reasons: tuple[str, ...]
    plain_text: str
    _argv: tuple[tuple[str, ...], ...] = field(default=(), repr=False, compare=False)
    _level: str = field(default="unsandboxed", repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ProjectRule:
    """Rule for a *canonical* project root SHA-256 digest, not a path/string search.

    ``argv_prefix`` consists of complete arguments, e.g. ("git", "status").
    An allow rule can only narrow the existing auto-allow list; it cannot add
    authority to an unsafe command or a different sandbox/mode.
    """

    root_hash: str
    argv_prefix: tuple[str, ...]
    decision: Decision


@dataclass(frozen=True, slots=True)
class ProjectRules:
    """Active canonical project root hash and its optional scoped rules.

    If any matching-root ``allow`` entries exist, every command in the chain
    must match one. An ``ask`` or ``refuse`` entry overrides an allow entry.
    """

    root_hash: str
    rules: tuple[ProjectRule, ...] = ()


@dataclass(frozen=True, slots=True)
class _Token:
    value: str
    kind: Literal["word", "separator", "group", "redirect"] = "word"
    interpolates: bool = False


class _AmbiguousSyntax(ValueError):
    pass


# Only known executables get a --version inspection exemption. A random
# program with a --version switch is not necessarily harmless.
_VERSION_TOOLS = frozenset({
    "python", "python3", "py", "git", "node", "npm", "pnpm", "yarn",
    "pytest", "pip", "pip3", "cargo", "rustc", "go", "dotnet", "ruff",
    "mypy", "cmake", "java", "javac", "curl", "wget", "pwsh", "powershell",
})
_INSPECT = frozenset({"dir", "ls", "type", "cat", "get-content", "get-childitem", "gci", "pwd", "get-location"})
_MUTATE = frozenset({
    "mkdir", "md", "rmdir", "rd", "del", "erase", "rm", "remove-item",
    "copy", "copy-item", "cp", "move", "move-item", "mv", "ren", "rename-item",
    "new-item", "set-content", "add-content", "out-file", "set-item", "clear-content",
    "touch", "tee", "tee-object", "set-itemproperty", "new-itemproperty", "remove-itemproperty",
})
_NETWORK = frozenset({
    "curl", "curl.exe", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod",
    "start-bitstransfer", "bitsadmin", "ftp", "telnet", "ping", "nslookup",
})
_DANGEROUS = frozenset({
    "reg", "regedit", "regini", "schtasks", "sc", "bcdedit", "format", "diskpart",
    "set-executionpolicy", "iex", "invoke-expression", "cmdkey", "vaultcmd", "keymgr",
    "get-storedcredential", "get-secret", "set-secret", "remove-secret", "get-secretvault",
    "ssh", "ssh-add", "ssh-agent", "scp", "sftp", "ssh-keygen", "ssh-keyscan",
    "register-scheduledtask", "unregister-scheduledtask", "new-scheduledtask",
    "set-service", "start-service", "stop-service", "new-service", "remove-service",
    "shutdown", "restart-computer", "stop-computer", "takeown", "icacls", "start-process",
    "start-job", "invoke-command", "enter-pssession", "new-pssession",
})
_GIT_INSPECT = frozenset({"status", "diff", "log"})
_GIT_NETWORK = frozenset({"clone", "fetch", "pull", "push", "ls-remote", "submodule", "remote"})
_GIT_MUTATE = frozenset({
    "add", "commit", "checkout", "switch", "restore", "reset", "clean", "stash",
    "merge", "rebase", "cherry-pick", "revert", "branch", "tag", "init", "apply",
})
_PROFILE = re.compile(
    r"(?i)(?:%\s*(?:userprofile|appdata|localappdata|homepath|homedrive)\s*%|"
    r"\$\{?(?:env:)?(?:home|userprofile|appdata|localappdata|profile)\b|"
    r"(?:^|[\\/])users[\\/][^\\/]+|(?:^|[\\/])\.ssh(?:[\\/]|$)|"
    r"(?:^|[\\/])\.aws(?:[\\/]|$)|^~(?:[\\/]|$)|"
    r"hkey_(?:current_user|local_machine|classes_root|users)\b|"
    r"\bhk(?:cu|lm|cr|u|cc):(?:\\)?|registry::|"
    r"\b(?:credential|password)[-_]?manager\b)"
)
_PARENT = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_ENCODED = re.compile(r"(?i)^-(?:e|en|enc|enco|encod|encode|encoded|encodedc|encodedco|encodedcom|encodedcomm|encodedcomma|encodedcomman|encodedcommand)(?::.*)?$")
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_NETWORK_PATH = re.compile(r"(?i)^(?:\\\\|//|https?://|ftp://)")
_ABSOLUTE_PATH = re.compile(r"(?i)^[a-z]:[\\/]")


def _tokenize(command: str, shell: str) -> tuple[_Token, ...]:
    """Lex shell words, quotes, escaped names and operators; never evaluate."""
    result: list[_Token] = []
    buf: list[str] = []
    quote: str | None = None
    interpolates = False
    index = 0

    def flush() -> None:
        nonlocal interpolates
        if buf:
            result.append(_Token("".join(buf), interpolates=interpolates))
            buf.clear()
            interpolates = False

    while index < len(command):
        char = command[index]
        ahead = command[index + 1] if index + 1 < len(command) else ""
        if shell == "cmd" and char == "^" or shell == "powershell" and char == "`":
            if not ahead:
                raise _AmbiguousSyntax("Unterminated shell escape")
            # Treat escaped operators as operators too: never hide a second
            # command behind cmd's caret or PowerShell's backtick.
            if ahead in ";|&\r\n<>":
                flush()
                result.append(_Token(ahead, "redirect" if ahead in "<>" else "separator"))
            else:
                buf.append(ahead)
            index += 2
            continue
        if quote is not None:
            if char == quote:
                if quote == "'" and shell == "powershell" and ahead == "'":
                    buf.append("'")
                    index += 2
                    continue
                quote = None
            else:
                buf.append(char)
            index += 1
            continue
        if char == '"' or char == "'" and shell == "powershell":
            quote = char
            if char == '"' and shell == "powershell":
                interpolates = True
            index += 1
            continue
        if shell == "powershell" and char == "$" and ahead == "{":
            end = command.find("}", index + 2)
            if end < 0:
                raise _AmbiguousSyntax("Unclosed PowerShell variable")
            buf.append(command[index:end + 1])
            index = end + 1
            continue
        if char in " \t\v\f":
            flush()
            index += 1
            continue
        if char in "\r\n":
            flush()
            result.append(_Token(";", "separator"))
            index += 1
            if char == "\r" and ahead == "\n":
                index += 1
            continue
        if char in ";|&":
            flush()
            if ahead == char and char in "&|":
                result.append(_Token(char * 2, "separator"))
                index += 2
            else:
                result.append(_Token(char, "separator"))
                index += 1
            continue
        if char in "(){}":
            flush()
            result.append(_Token(char, "group"))
            index += 1
            continue
        if char in "<>":
            flush()
            result.append(_Token(char, "redirect"))
            index += 1
            if ahead == char:
                index += 1
            continue
        if shell == "powershell" and char == "#" and not buf:
            # PowerShell comments start at a new token, outside quoted text.
            while index < len(command) and command[index] not in "\r\n":
                index += 1
            continue
        if shell == "powershell" and char == "@" and ahead in "\"'":
            raise _AmbiguousSyntax("Here-string or splatting requires review")
        buf.append(char)
        index += 1
    if quote is not None:
        raise _AmbiguousSyntax("Unterminated quoted argument")
    flush()
    if len(result) > 2048:
        raise _AmbiguousSyntax("Too many command tokens")
    return tuple(result)


def _name(word: str) -> str:
    # Do not normalize paths into a trusted executable: .\\git.exe could be
    # an attacker-controlled program, not the installed git executable.
    lowered = word.casefold()
    if "/" in lowered or "\\" in lowered or ":" in lowered:
        return lowered
    return lowered.removesuffix(".exe")


def _sensitive(argv: tuple[str, ...]) -> bool:
    return any(_PROFILE.search(arg) is not None for arg in argv)


def _beyond_workcopy(argv: tuple[str, ...]) -> bool:
    return any(_PARENT.search(arg) is not None for arg in argv)


def _unsafe_recursive_delete_target(target: str) -> bool:
    """Reject recursive targets outside the work copy or targeting its root."""
    # ntpath.isabs differs across Python versions for rooted Windows paths.
    # A drive-relative path (C:foo) is unsafe too: it depends on drive state.
    if ntpath.splitdrive(target)[0] or target.startswith(("\\", "/")):
        return True
    if "$" in target or "%" in target or re.search(r"![^!]+!", target):
        return True

    parts: list[str] = []
    for part in re.split(r"[\\/]", target):
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return True
            parts.pop()
        else:
            parts.append(part)
    # A recursive delete of "." or a root-level wildcard can wipe the work copy.
    return not parts or (len(parts) == 1 and any(char in parts[0] for char in "*?"))


def _classify_argv(argv: tuple[str, ...]) -> CommandClass:
    if not argv:
        return "ask"
    name = _name(argv[0])
    base = ntpath.basename(argv[0].strip("\"' ")).casefold().removesuffix(".exe")
    args = tuple(arg.casefold() for arg in argv[1:])
    if _sensitive(argv):
        return "dangerous"
    if base in _DANGEROUS:
        return "dangerous"
    if base in {"powershell", "pwsh"} and any(_ENCODED.match(arg) for arg in args):
        return "dangerous"
    if base in {"python", "python3", "py", "node", "ruby", "perl"} and args and args[0] in {"-c", "-e", "-command"}:
        return "dangerous"
    if any(flag in args for flag in {"--user", "--global", "-g"}) and (
        name in {"pip", "pip3", "npm", "pnpm", "yarn", "cargo"}
        or name in {"python", "python3", "py"} and args[:2] == ("-m", "pip")
    ):
        return "dangerous"
    safe_recursive_delete = False
    if name in {"rm", "rd", "rmdir", "del", "erase", "remove-item"}:
        recursive = any(
            arg in {"/s", "/s/q", "/q/s", "-recurse", "--recursive"}
            or bool(re.fullmatch(r"-[rfvi]*r[rfvi]*", arg))
            for arg in args
        )
        if recursive:
            switches = {"-recurse", "--recursive", "-force", "--force", "-path", "-literalpath"}
            if name in {"rd", "rmdir", "del", "erase"}:
                switches.update({"/s", "/q", "/s/q", "/q/s", "/f", "/p"})
            # Check delete targets, not cmd switches such as /s and /q.
            targets = tuple(
                arg for arg in args
                if arg not in switches and arg != "--"
                and not (arg.startswith("-") and bool(re.fullmatch(r"-[rfvi]+", arg)))
            )
            if not targets or any(_unsafe_recursive_delete_target(target) for target in targets):
                return "dangerous"
            safe_recursive_delete = True
    if any(_NETWORK_PATH.search(arg) for arg in argv[1:]):
        return "network"
    if (
        (_beyond_workcopy(argv) and not safe_recursive_delete)
        or any(_ABSOLUTE_PATH.match(arg) for arg in argv[1:])
    ):
        return "ask"
    if any("$" in arg or "%" in arg for arg in argv):
        return "ask"
    if name != base:
        return "ask"
    if args in {("--version",), ("-version",), ("-v",)} and name in _VERSION_TOOLS:
        return "inspect"
    if name in {"python", "python3", "py"} and args == ("-v",):
        return "inspect"
    if name in {"python", "python3", "py"} and len(args) >= 3 and args[:2] == ("-m", "pip") and args[2] in {"install", "download", "uninstall", "wheel"}:
        return "network"
    if name in {"python", "python3", "py"} and len(args) >= 2 and args[:2] == ("-m", "pytest"):
        return "build_test"
    if name in {"python", "python3", "py"} and len(args) >= 3 and args[:3] == ("-m", "ruff", "check"):
        return "build_test"
    if name in {"python", "python3", "py"} and len(args) >= 2 and args[:2] == ("-m", "mypy"):
        return "build_test"
    if name in {"pytest", "py.test", "ruff", "mypy"}:
        return "build_test"
    if name in _INSPECT:
        return "inspect"
    if name == "git":
        if "--global" in args or "--system" in args:
            return "dangerous"
        if not args:
            return "ask"
        if args[0] in _GIT_NETWORK:
            return "network"
        if args[0] in _GIT_MUTATE:
            return "mutate_workcopy"
        if args[0] in _GIT_INSPECT:
            if any(arg in {"--ext-diff", "--textconv", "--no-index"} or arg.startswith("--output") for arg in args[1:]):
                return "ask"
            return "inspect"
        return "ask"
    if name in {"pip", "pip3"} and args and args[0] in {"install", "download", "uninstall", "wheel"}:
        return "network"
    if name in {"npm", "pnpm", "yarn"}:
        if args and args[0] in {"install", "i", "ci", "add", "update", "upgrade", "audit", "publish", "exec"}:
            return "network"
        if args and (args[0] == "test" or len(args) >= 2 and args[:2] in {("run", "build"), ("run", "lint"), ("run", "test")}):
            return "build_test"
        return "ask"
    if name == "npx":
        return "network"
    if name == "cargo":
        if args and args[0] in {"install", "fetch", "publish", "update"}:
            return "network"
        if args and args[0] in {"test", "build", "check", "clippy"}:
            return "build_test"
        if args and args[0] in {"clean", "fmt"}:
            return "mutate_workcopy"
    if name in {"go", "dotnet"}:
        if args and args[0] in {"test", "build", "vet", "lint"}:
            return "build_test"
        if args and args[0] in {"install", "get", "restore", "publish"}:
            return "network"
    if name in _NETWORK:
        return "network"
    if name in _MUTATE:
        return "mutate_workcopy"
    return "ask"


def _subexpressions(value: str) -> tuple[str, ...]:
    """Extract PowerShell $(...) expressions inside expandable double quotes."""
    found: list[str] = []
    pos = 0
    while True:
        start = value.find("$(", pos)
        if start < 0:
            return tuple(found)
        depth = 1
        cursor = start + 2
        while cursor < len(value) and depth:
            if value[cursor] == "(":
                depth += 1
            elif value[cursor] == ")":
                depth -= 1
            cursor += 1
        if depth:
            raise _AmbiguousSyntax("Unbalanced PowerShell interpolation")
        found.append(value[start + 2:cursor - 1])
        pos = cursor


def _delegated(argv: tuple[str, ...]) -> tuple[str, str] | None:
    """Return shell and script for an explicitly recognizable shell wrapper."""
    name = ntpath.basename(argv[0]).casefold().removesuffix(".exe")
    if name in {"powershell", "pwsh"}:
        options = {"-noprofile", "-nop", "-noninteractive", "-noni", "-nologo", "-nol", "-sta", "-mta"}
        for i, arg in enumerate(argv[1:], 1):
            if arg.casefold() in {"-command", "-c"}:
                if all(switch.casefold() in options for switch in argv[1:i]) and i + 1 < len(argv):
                    return "powershell", " ".join(argv[i + 1:])
                return None
        return None
    if name == "cmd":
        options = {"/d", "/q", "/s"}
        for i, arg in enumerate(argv[1:], 1):
            if arg.casefold() == "/c":
                if all(switch.casefold() in options for switch in argv[1:i]) and i + 1 < len(argv):
                    return "cmd", " ".join(argv[i + 1:])
                return None
        return None
    return None


def _analyze(command: str, shell: str, depth: int = 0) -> tuple[list[CommandClass], list[tuple[str, ...]]]:
    if depth > 8 or len(command) > 32768:
        raise _AmbiguousSyntax("Nested or oversized command")
    tokens = _tokenize(command, shell)
    classes: list[CommandClass] = []
    commands: list[tuple[str, ...]] = []
    pending: list[str] = []
    redirect = False

    def flush() -> None:
        nonlocal redirect
        if not pending:
            return
        argv = tuple(pending)
        pending.clear()
        # Before permitting a wrapper, inspect its flags and any protected
        # destinations; a -Command script's quoted text is parsed separately.
        outer = _classify_argv(argv)
        delegated = _delegated(argv)
        if delegated is None:
            classes.append(outer)
            commands.append(tuple(x.casefold() for x in argv))
        else:
            # Include the wrapper risk as well as its parsed script, so an
            # untrusted executable path never becomes a trusted inspect call.
            if outer != "ask":
                classes.append(outer)
            elif _name(argv[0]) != ntpath.basename(argv[0]).casefold().removesuffix(".exe"):
                classes.append("ask")
            sub_classes, sub_commands = _analyze(delegated[1], delegated[0], depth + 1)
            classes.extend(sub_classes)
            commands.extend(sub_commands)
        if redirect:
            classes.append("mutate_workcopy")
            redirect = False

    for token in tokens:
        if token.kind == "word":
            pending.append(token.value)
            if token.interpolates:
                for body in _subexpressions(token.value):
                    sub_classes, sub_commands = _analyze(body, "powershell", depth + 1)
                    classes.extend(sub_classes)
                    commands.extend(sub_commands)
        elif token.kind == "redirect":
            redirect = True
        else:
            flush()
    flush()
    if redirect:
        classes.append("mutate_workcopy")
    return classes, commands


def classify(command: str, shell: str, level: str) -> Verdict:
    """Classify PowerShell/cmd without spawning processes, reading files or resolving paths.

    ``level`` must be independently supplied by the sandbox implementation;
    this parser never claims that a sandbox exists. A missing or unknown level
    is treated as unsandboxed. Ambiguous syntax becomes ``ask``, not ``allow``.
    """
    normalized_level = level.casefold() if isinstance(level, str) else "unsandboxed"
    if normalized_level != "appcontainer":
        normalized_level = "unsandboxed"
    normalized_shell = shell.casefold().removesuffix(".exe") if isinstance(shell, str) else ""
    if normalized_shell in {"pwsh", "powershell"}:
        normalized_shell = "powershell"
    elif normalized_shell != "cmd":
        return Verdict("ask", ("Unsupported shell; no safe command parsing is available.",), _TEXT["ask"], _level=normalized_level)
    if not isinstance(command, str) or not command.strip():
        return Verdict("ask", ("Empty or invalid command.",), _TEXT["ask"], _level=normalized_level)
    try:
        classes, commands = _analyze(command, normalized_shell)
    except _AmbiguousSyntax as exc:
        return Verdict("ask", (str(exc),), _TEXT["ask"], _level=normalized_level)
    if not classes:
        return Verdict("ask", ("No executable command was identified.",), _TEXT["ask"], _level=normalized_level)
    cls = max(classes, key=_RANK.__getitem__)
    reason = {
        "inspect": "Recognized read-only command.",
        "build_test": "Recognized build or test command.",
        "mutate_workcopy": "Command can modify project files.",
        "network": "Command can access the network or install packages.",
        "dangerous": "Command targets a protected location or sensitive system operation.",
        "ask": "Command or arguments cannot be verified as safe.",
    }[cls]
    if len(classes) > 1:
        reason += " The most restrictive part of the command chain applies."
    return Verdict(cls, (reason,), _TEXT[cls], tuple(commands), normalized_level)


def _valid_rules(project_rules: ProjectRules) -> bool:
    return bool(_HASH.fullmatch(project_rules.root_hash)) and all(
        _HASH.fullmatch(rule.root_hash) is not None
        and rule.argv_prefix
        and all(isinstance(part, str) and bool(part) for part in rule.argv_prefix)
        and rule.decision in {"allow", "ask", "refuse"}
        for rule in project_rules.rules
    )


def _match(argv: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    normalized = tuple(_name(part) if i == 0 else part.casefold() for i, part in enumerate(argv))
    wanted = tuple(_name(part) if i == 0 else part.casefold() for i, part in enumerate(prefix))
    return len(normalized) >= len(wanted) and normalized[:len(wanted)] == wanted


def decide(verdict: Verdict, mode: Mode, project_rules: ProjectRules | None = None) -> Decision:
    """Apply fail-closed consent and optional project-scoped *argv* rules.

    Project rules may restrict automatic permission, never bypass the danger,
    mode, network or sandbox checks. Root hashes are compared, never path text.
    """
    if verdict.cls == "dangerous" or mode == "plan_only" or mode not in {"ask", "auto_sandboxed"}:
        return "refuse"
    if mode == "ask":
        return "ask"
    active: tuple[ProjectRule, ...] = ()
    if project_rules is not None:
        if not isinstance(project_rules, ProjectRules) or not _valid_rules(project_rules):
            return "ask"
        active = tuple(rule for rule in project_rules.rules if rule.root_hash.casefold() == project_rules.root_hash.casefold())
        for rule in active:
            if rule.decision == "refuse" and any(_match(argv, rule.argv_prefix) for argv in verdict._argv):
                return "refuse"
        for rule in active:
            if rule.decision == "ask" and any(_match(argv, rule.argv_prefix) for argv in verdict._argv):
                return "ask"
    if verdict._level != "appcontainer" or verdict.cls not in {"inspect", "build_test"}:
        return "ask"
    allow_rules = tuple(rule for rule in active if rule.decision == "allow")
    if allow_rules and (not verdict._argv or any(
        not any(_match(argv, rule.argv_prefix) for rule in allow_rules) for argv in verdict._argv
    )):
        return "ask"
    return "allow"
