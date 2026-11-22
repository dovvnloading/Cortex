"""Private launchers for future, explicitly authorized Windows agent commands.

No HTTP routes or agent behaviors opt into these launchers implicitly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import logging

from cortex_backend.agent.sandbox.appcontainer import AppContainerLauncher
from cortex_backend.agent.sandbox.base import Launcher, ProcessHandle, SandboxLevel
from cortex_backend.agent.sandbox.jobonly import JobOnlyLauncher
from cortex_backend.core.win_appcontainer import ProfileCreationError
from cortex_backend.core.win_jobs import JobLimits

_LOG = logging.getLogger(__name__)


class UnsandboxedFallbackRequired(PermissionError):
    """AppContainer failed: the caller must separately authorize job-only work."""


class AutoLauncher:
    """Fallback only for a profile-creation failure, never for launch/ACL bugs.

    Callers must report level after start: a failed low-box creation changes
    it to unsandboxed. Job-only refuses network-disabled work; callers must
    explicitly authorize networking before starting this less isolated mode.
    """

    def __init__(self, workcopy: str | Path, run_id: str):
        self._appcontainer = AppContainerLauncher(workcopy, run_id)
        self._selected: Launcher = self._appcontainer

    @property
    def level(self) -> SandboxLevel:
        return self._selected.level

    @property
    def unavailable_toolchains(self) -> dict[str, tuple[str, ...]]:
        return dict(getattr(self._selected, "unavailable_toolchains", {}))

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: JobLimits | None = None,
    ) -> ProcessHandle:
        try:
            return self._selected.start(argv, cwd, env, network, limits)
        except ProfileCreationError as exc:
            if self._selected is not self._appcontainer:
                raise
            self._selected = JobOnlyLauncher(self._appcontainer.workcopy)
            _LOG.warning("Agent AppContainer profile could not be created; fallback is UNSANDBOXED")
            # Never execute with the user's full token as a *side effect* of a
            # sandboxed launch request. Report the level before caller consent.
            raise UnsandboxedFallbackRequired(
                "AppContainer unavailable; level=unsandboxed. Explicitly approve job-only execution to retry."
            ) from exc


__all__ = [
    "AppContainerLauncher", "AutoLauncher", "JobOnlyLauncher", "Launcher", "ProcessHandle",
    "SandboxLevel", "UnsandboxedFallbackRequired",
]
