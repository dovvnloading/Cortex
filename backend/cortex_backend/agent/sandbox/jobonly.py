"""Explicitly unsandboxed fallback when CreateAppContainerProfile is refused.

A Job Object contains descendants and resources, not filesystem or networking.
No-network requests are refused rather than misrepresented as isolated; callers
must display level == 'unsandboxed' before authorizing network-enabled work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys

from cortex_backend.agent.sandbox.base import (
    ProcessHandle, RunningProcess, SandboxLevel, canonical_workcopy,
    child_environment, command_argv, job_limits, toolchain, working_directory,
)
from cortex_backend.core.win_jobs import JobLimits, KillOnCloseJob
from cortex_backend.core.win_appcontainer import CREATE_NO_WINDOW, CREATE_SUSPENDED


class JobOnlyLauncher:
    level: SandboxLevel = "unsandboxed"

    def __init__(self, workcopy: str | Path):
        self.workcopy = canonical_workcopy(workcopy)

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None,
        network: bool = False, limits: JobLimits | None = None,
    ) -> ProcessHandle:
        if not network:
            raise PermissionError("unsandboxed fallback cannot enforce network=False")
        if sys.platform != "win32":
            raise OSError("Windows Job Objects are required for fallback")
        directory = working_directory(cwd, self.workcopy)
        tools, folders = toolchain()
        args = command_argv(argv, self.workcopy, tools)
        _ = env  # never forward arbitrary environment keys or parent secrets
        environment = child_environment(self.workcopy, folders)
        process = subprocess.Popen(
            args, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
            close_fds=True, creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW,
        )
        job: KillOnCloseJob | None = None
        try:
            job = KillOnCloseJob(job_limits(limits))
            job.assign(process.pid)
            # subprocess closes its initial thread handle; the NT resume API
            # resumes the one suspended thread via its retained process handle.
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            resume = ntdll.NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            resume.restype = ctypes.c_long
            handle = getattr(process, "_handle", None)
            if not handle or resume(wintypes.HANDLE(int(handle))) < 0:
                raise OSError("NtResumeProcess failed")
            return RunningProcess(process, job, self.level, lambda: None)
        except BaseException:
            if job is not None:
                job.close()
            process.kill()
            process.wait(timeout=10)
            raise
