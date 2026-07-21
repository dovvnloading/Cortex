"""Disposable hostile AppContainer cancellation and process-tree corpus."""

from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

import appcontainer_smoke as native


def _result(status: str, evidence: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "containment_cancellation_corpus",
        "status": status,
        "evidence": evidence,
    }
    if details:
        payload["details"] = details
    return payload


def run() -> dict[str, Any]:
    if sys.platform != "win32":
        return _result("blocked", "The native cancellation corpus requires Windows.")

    system_root = os.environ.get("WINDIR", r"C:\Windows")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    choice = Path(system_root) / "System32" / "choice.exe"
    if not powershell.is_file() or not choice.is_file():
        return _result(
            "blocked",
            "The fixed Windows process-tree corpus tools are unavailable.",
            powershell=str(powershell),
            choice=str(choice),
        )

    kernel32, userenv, advapi32, *_ = native._configure_apis()
    profile_name = f"CortexPhase0-Cancel-{uuid4().hex}"
    profile_sid = ctypes.c_void_p()
    try:
        hr = userenv.CreateAppContainerProfile(
            profile_name,
            "Cortex Phase 0 cancellation",
            "Disposable fixed process-tree cancellation corpus",
            None,
            0,
            ctypes.byref(profile_sid),
        )
        if hr != 0:
            raise ctypes.WinError(hr & 0xFFFFFFFF)

        # Fixed, non-user-controlled code: create a long-running native
        # descendant and then keep the launcher alive. The launcher is used only
        # to exercise Job Object full-tree semantics; production never invokes a
        # shell or accepts source through this path.
        script = (
            f'$child=Start-Process "{choice}" '
            '-ArgumentList "/T","30","/D","Y" -PassThru -WindowStyle Hidden; '
            'Start-Sleep -Seconds 30'
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        details = native._run_child(
            kernel32,
            userenv,
            advapi32,
            profile_name,
            profile_sid,
            str(powershell),
            [
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            timeout_ms=1000,
            active_process_limit=4,
        )
        checks = {
            "appcontainer_token": details["is_appcontainer"],
            "cancellation_timeout": details["timed_out"],
            "multiple_processes_observed": len(details["job_process_ids"]) >= 2,
            "full_tree_reaped": details["all_job_processes_reaped"] is True,
        }
        status = "pass" if all(checks.values()) else "fail"
        return _result(
            status,
            "A fixed AppContainer launcher created a native descendant; closing the kill-on-close Job Object reaped every observed process."
            if status == "pass"
            else "The fixed AppContainer cancellation corpus did not prove full-tree reaping.",
            profile_name=profile_name,
            checks=checks,
            process_ids=details["job_process_ids"],
            process_count=len(details["job_process_ids"]),
            launcher_exit_code=details["exit_code"],
            reap_seconds=details["reap_seconds"],
        )
    except Exception as exc:
        return _result(
            "fail",
            "The fixed cancellation corpus failed closed before full-tree reaping was proven.",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        if profile_sid.value:
            try:
                advapi32.FreeSid(profile_sid)
            except OSError:
                pass
        try:
            userenv.DeleteAppContainerProfile(profile_name)
        except OSError:
            pass


def main() -> int:
    report = run()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
