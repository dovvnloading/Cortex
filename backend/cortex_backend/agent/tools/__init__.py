"""Explicitly consented agent capabilities; no automatic tool dispatch."""

from .files import FILE_TOOLS, register_file_tools
from .git import GIT_TOOLS, register_git_tools
from .plan import plan_guard, register_plan_tool
from .shell import SHELL_TOOLS, ShellTools, register_shell_tools
from .web import WebFetchError, fetch_url

__all__ = [
    "FILE_TOOLS",
    "GIT_TOOLS",
    "SHELL_TOOLS",
    "ShellTools",
    "WebFetchError",
    "fetch_url",
    "plan_guard",
    "register_file_tools",
    "register_git_tools",
    "register_plan_tool",
    "register_shell_tools",
]
