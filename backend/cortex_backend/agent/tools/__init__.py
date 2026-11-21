"""Explicitly consented agent capabilities; no automatic tool dispatch."""

from .files import FILE_TOOLS, register_file_tools
from .plan import plan_guard, register_plan_tool
from .web import WebFetchError, fetch_url

__all__ = [
    "FILE_TOOLS",
    "WebFetchError",
    "fetch_url",
    "plan_guard",
    "register_file_tools",
    "register_plan_tool",
]
