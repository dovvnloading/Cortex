"""Controlled Windows evidence for the concrete suspended worker factory."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import pytest

from cortex_backend.execution.native_launcher import (
    BrokerWorkerBinding,
    NativeWorkerLaunchPlan,
    NativeWorkerPolicy,
)
from cortex_backend.execution.native_win32 import NativeWin32ProcessFactory
from cortex_backend.execution.worker_provenance import VerifiedRecipeWorker


@pytest.mark.skipif(os.name != "nt", reason="Win32 process factory requires Windows")
def test_win32_factory_creates_suspended_appcontainer_and_job_policy():
    system_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
    executable = system_root / "System32" / "findstr.exe"
    if not executable.is_file():
        pytest.skip("findstr.exe is unavailable on this Windows host")
    with TemporaryDirectory(prefix="cortex-native-factory-") as temp_root:
        package_root = Path(temp_root)
        test_executable = package_root / "findstr.exe"
        shutil.copy2(executable, test_executable)
        _run_factory_probe(test_executable)


def _run_factory_probe(executable: Path) -> None:
    binding = BrokerWorkerBinding(
        pipe_name=r"\\.\pipe\cortex-worker-factory-test",
        broker_process_id=os.getpid(),
        installation_principal_id="a" * 64,
        job_id="job-factory-test",
    )
    plan = NativeWorkerLaunchPlan(
        worker=VerifiedRecipeWorker(
            bundle_root=executable.parent,
            bundle_digest="0" * 64,
            key_id="release-1",
            worker_path="recipe_worker.exe",
            worker_sha256="0" * 64,
            worker_size=1,
            recipe_version="1.0.0",
        ),
        executable=executable,
        command_line=subprocess.list2cmdline(
            [
                str(executable),
                "--native-broker",
                "--broker-pipe",
                binding.pipe_name,
                "--broker-pid",
                str(binding.broker_process_id),
            ]
        ),
        broker=binding,
        policy=NativeWorkerPolicy(),
    )
    worker = NativeWin32ProcessFactory().create_suspended(plan)
    try:
        assert worker.process_id > 0
        assert worker.app_container_sid.startswith("S-1-15-2-")
        worker.apply_job_policy(plan.policy)
    finally:
        worker.close()
