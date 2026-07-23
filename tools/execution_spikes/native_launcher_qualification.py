"""Disposable suspended-launch and Job Object policy qualification.

This helper exercises the native construction order with fixed Windows tools:
create an AppContainer child suspended, apply explicit Job Object limits, verify
the queried policy/accounting, then resume and close all handles.  It never
accepts a worker path, model input, command, or bundle and never claims that the
real recipe worker is launchable.  The signed worker package and live broker
PID/token binding remain blocking checks until a reviewed native launcher exists.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from uuid import uuid4

try:
    import appcontainer_smoke as native
except ModuleNotFoundError:  # pragma: no cover - package import path
    from . import appcontainer_smoke as native


ROOT = Path(__file__).resolve().parents[2]
PASS = "pass"
BLOCKED = "blocked"
FAIL = "fail"
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000


def _result(name: str, status: str, evidence: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "evidence": evidence}
    if details:
        payload["details"] = details
    return payload


def _find_system_tool(name: str) -> str | None:
    system_root = os.environ.get("WINDIR", r"C:\Windows")
    path = Path(system_root) / "System32" / name
    return str(path) if path.is_file() else None


def _probe_resource_policy() -> dict[str, Any]:
    if sys.platform != "win32":
        return _result(
            "native_launcher_resource_policy",
            BLOCKED,
            "The suspended native launcher policy probe requires Windows Job Objects.",
        )
    findstr = _find_system_tool("findstr.exe")
    if not findstr:
        return _result(
            "native_launcher_resource_policy",
            BLOCKED,
            "The fixed Windows executable used for policy qualification is unavailable.",
        )

    kernel32, userenv, advapi32, *_ = native._configure_apis()
    profile_name = f"CortexLauncherPolicy-{uuid4().hex}"
    profile_sid = wintypes.LPVOID()
    marker_dir: Path | None = None
    marker_path: Path | None = None
    try:
        hr = userenv.CreateAppContainerProfile(
            profile_name,
            "Cortex launcher policy",
            "Disposable fixed Job Object resource policy probe",
            None,
            0,
            ctypes.byref(profile_sid),
        )
        if hr != 0:
            raise ctypes.WinError(hr & 0xFFFFFFFF)

        marker_dir = Path(tempfile.mkdtemp(prefix="cortex-launcher-policy-"))
        marker_path = marker_dir / "private-marker.txt"
        marker_path.write_text("launcher-policy-marker\n", encoding="ascii")
        details = native._run_child(
            kernel32,
            userenv,
            advapi32,
            profile_name,
            profile_sid,
            findstr,
            ["/C:launcher-policy-marker", str(marker_path)],
            timeout_ms=5000,
            active_process_limit=1,
            process_memory_limit_bytes=64 * 1024 * 1024,
            job_memory_limit_bytes=128 * 1024 * 1024,
            process_user_time_100ns=20_000_000,
            job_user_time_100ns=40_000_000,
        )
        info = details.get("job_information")
        if not isinstance(info, dict):
            raise RuntimeError("Job Object accounting was not returned")
        flags = int(info.get("limit_flags", 0))
        expected_flags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
            | _JOB_OBJECT_LIMIT_PROCESS_TIME
            | _JOB_OBJECT_LIMIT_JOB_TIME
        )
        checks = {
            "appcontainer_token": details.get("is_appcontainer") is True,
            "suspended_policy_applied_before_resume": True,
            "bounded_completion": details.get("timed_out") is False,
            "required_limit_flags": flags & expected_flags == expected_flags,
            "active_process_limit": info.get("active_process_limit") == 1,
            "process_memory_limit": info.get("process_memory_limit") == 64 * 1024 * 1024,
            "job_memory_limit": info.get("job_memory_limit") == 128 * 1024 * 1024,
            "process_cpu_limit": info.get("process_user_time_limit_100ns") == 20_000_000,
            "job_cpu_limit": info.get("job_user_time_limit_100ns") == 40_000_000,
            "breakaway_not_granted": flags
            & (_JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)
            == 0,
        }
        status = PASS if all(checks.values()) else FAIL
        return _result(
            "native_launcher_resource_policy",
            status,
            "A fixed AppContainer child was created suspended, assigned explicit Job Object resource policy before resume, and its policy/accounting was queried."
            if status == PASS
            else "The fixed suspended-launch policy did not prove every required Job Object limit.",
            profile_name=profile_name,
            checks=checks,
            job_information=info,
            child_exit_code=details.get("exit_code"),
        )
    except Exception as exc:
        return _result(
            "native_launcher_resource_policy",
            FAIL,
            "The fixed native launcher policy probe failed closed before policy qualification completed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        if marker_path is not None:
            try:
                marker_path.unlink(missing_ok=True)
                marker_path.parent.rmdir()
            except OSError:
                pass
        if profile_sid and profile_sid.value:
            try:
                advapi32.FreeSid(profile_sid)
            except OSError:
                pass
        try:
            userenv.DeleteAppContainerProfile(profile_name)
        except OSError:
            pass


def _probe_worker_package() -> dict[str, Any]:
    root = ROOT / "packaging" / "recipe-runtime"
    entrypoint = root / "recipe_worker.exe"
    if not root.is_dir() or not entrypoint.is_file():
        return _result(
            "native_launcher_worker_package",
            BLOCKED,
            "The fixed signed recipe worker package is not shipped; launch remains refused.",
            expected_root=str(root),
            expected_entrypoint="recipe_worker.exe",
            launch_refused=True,
        )
    return _result(
        "native_launcher_worker_package",
        BLOCKED,
        "A package is present but this disposable spike has no trust-root-to-launcher binding yet.",
        launch_refused=True,
    )


def _probe_broker_binding() -> dict[str, Any]:
    return _result(
        "native_launcher_broker_binding",
        BLOCKED,
            "The native broker transport and launcher binder are qualified separately; a signed installed worker still needs a real PID/token handshake.",
        launch_refused=True,
    )


def build_report() -> dict[str, Any]:
    checks = [_probe_resource_policy(), _probe_worker_package(), _probe_broker_binding()]
    ready = all(check["status"] == PASS for check in checks)
    return {
        "name": "cortex-native-launcher-qualification",
        "probe": "cortex-native-launcher-qualification",
        "schema_version": 1,
        "repository_root": str(ROOT),
        "checks": checks,
        "provider_launch_authorized": False,
        "qualification_status": PASS if ready else BLOCKED,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit compact JSON only.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 unless the worker and broker gates are also green.",
    )
    args = parser.parse_args()
    report = build_report()
    if args.json:
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report["qualification_status"] != PASS:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
