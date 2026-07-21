"""Non-production Phase 0 probes for Cortex execution-harness prerequisites.

This module is intentionally outside ``backend/cortex_backend`` and is never
imported by the application. It checks host/package prerequisites and performs
only a benign Job Object smoke test plus a fixed WebAssembly module smoke test
when the optional Wasmtime Python package is installed.

It does *not* execute model-generated code, create a production AppContainer,
or provide an execution fallback. The optional native helper opens only a
disposable loopback listener for its denial check. A blocked probe is a valid
result: Phase 0 must not silently convert a missing prerequisite into a weaker
isolation mode.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import importlib
import importlib.util
import json
from multiprocessing.connection import Listener
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
PASS = "pass"
BLOCKED = "blocked"
FAIL = "fail"
NOT_RUN = "not_run"


def result(name: str, status: str, evidence: str, **details: Any) -> dict[str, Any]:
    """Return the stable, JSON-serializable shape consumed by the evidence log."""

    payload: dict[str, Any] = {
        "name": name,
        "status": status,
        "evidence": evidence,
    }
    if details:
        payload["details"] = details
    return payload


def probe_environment() -> dict[str, Any]:
    return result(
        "environment",
        PASS if sys.platform == "win32" else BLOCKED,
        "Cortex currently targets Windows; probes are read-only on other hosts.",
        platform=sys.platform,
        platform_release=platform.release(),
        platform_version=platform.version(),
        machine=platform.machine(),
        python=sys.version.split()[0],
        executable=sys.executable,
        windows=sys.platform == "win32",
    )


def _has_exports(dll_name: str, exports: tuple[str, ...]) -> tuple[bool, list[str]]:
    """Check that required Win32 entry points exist without invoking them."""

    try:
        dll = ctypes.WinDLL(dll_name, use_last_error=True)
    except OSError:
        return False, []
    found = [name for name in exports if hasattr(dll, name)]
    return len(found) == len(exports), found


def probe_windows_api_surface() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return [
            result(
                "appcontainer_api_surface",
                BLOCKED,
                "Win32 userenv.dll is unavailable on this host.",
            ),
            result(
                "job_object_api_surface",
                BLOCKED,
                "Win32 kernel32.dll Job Object APIs are unavailable on this host.",
            ),
            result(
                "named_pipe_api_surface",
                BLOCKED,
                "Win32 named-pipe APIs are unavailable on this host.",
            ),
        ]

    appcontainer_exports = (
        "CreateAppContainerProfile",
        "DeleteAppContainerProfile",
        "DeriveAppContainerSidFromAppContainerName",
    )
    job_exports = (
        "CreateJobObjectW",
        "SetInformationJobObject",
        "AssignProcessToJobObject",
        "TerminateJobObject",
        "QueryInformationJobObject",
    )
    pipe_exports = ("CreateNamedPipeW", "ConnectNamedPipe", "DisconnectNamedPipe")

    app_ok, app_found = _has_exports("userenv.dll", appcontainer_exports)
    job_ok, job_found = _has_exports("kernel32.dll", job_exports)
    pipe_ok, pipe_found = _has_exports("kernel32.dll", pipe_exports)
    return [
        result(
            "appcontainer_api_surface",
            PASS if app_ok else BLOCKED,
            "Required AppContainer profile APIs are exported by userenv.dll."
            if app_ok
            else "One or more AppContainer profile APIs are unavailable.",
            required=list(appcontainer_exports),
            found=app_found,
        ),
        result(
            "job_object_api_surface",
            PASS if job_ok else BLOCKED,
            "Required Job Object lifecycle APIs are exported by kernel32.dll."
            if job_ok
            else "One or more Job Object APIs are unavailable.",
            required=list(job_exports),
            found=job_found,
        ),
        result(
            "named_pipe_api_surface",
            PASS if pipe_ok else BLOCKED,
            "Required named-pipe APIs are exported by kernel32.dll."
            if pipe_ok
            else "One or more named-pipe APIs are unavailable.",
            required=list(pipe_exports),
            found=pipe_found,
        ),
    ]


if sys.platform == "win32":

    class _LargeInteger(ctypes.Union):
        _fields_ = [("quad_part", ctypes.c_longlong)]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operations", ctypes.c_ulonglong),
            ("write_operations", ctypes.c_ulonglong),
            ("other_operations", ctypes.c_ulonglong),
            ("read_transfers", ctypes.c_ulonglong),
            ("write_transfers", ctypes.c_ulonglong),
            ("other_transfers", ctypes.c_ulonglong),
        ]


    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time", _LargeInteger),
            ("per_job_user_time", _LargeInteger),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]


    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", _BasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
    ]


def probe_appcontainer_isolation_smoke(run_smoke: bool) -> dict[str, Any]:
    """Run the reviewed native profile-backed process test in a child process."""

    if sys.platform != "win32":
        return result(
            "appcontainer_process_isolation_smoke",
            BLOCKED,
            "The native AppContainer process test requires Windows.",
        )
    helper = ROOT / "tools" / "execution_spikes" / "appcontainer_smoke.py"
    if not run_smoke:
        return result(
            "appcontainer_process_isolation_smoke",
            NOT_RUN,
            "Rerun with --appcontainer-smoke to execute the fixed native containment corpus.",
            helper=str(helper),
        )
    if not helper.is_file():
        return result(
            "appcontainer_process_isolation_smoke",
            BLOCKED,
            "The reviewed native AppContainer helper is missing.",
            helper=str(helper),
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload.get("name") != "appcontainer_process_isolation_smoke":
            raise ValueError("native helper returned an unexpected check name")
        payload.setdefault("details", {})["helper_exit_code"] = completed.returncode
        if completed.stderr:
            payload["details"]["helper_stderr"] = completed.stderr[-2000:]
        return payload
    except subprocess.TimeoutExpired as exc:
        return result(
            "appcontainer_process_isolation_smoke",
            FAIL,
            "The native AppContainer helper exceeded its fail-closed timeout.",
            error_type=type(exc).__name__,
            timeout_seconds=20,
        )
    except Exception as exc:
        return result(
            "appcontainer_process_isolation_smoke",
            FAIL,
            "The native AppContainer helper returned invalid evidence and the gate failed closed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def probe_job_object_smoke() -> dict[str, Any]:
    """Verify kill-on-close for a benign, fixed child process on Windows."""

    if sys.platform != "win32":
        return result(
            "job_object_kill_on_close_smoke",
            BLOCKED,
            "The Job Object smoke test requires Windows.",
        )

    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process: subprocess.Popen[str] | None = None
    job = wintypes.HANDLE()
    process_handle = wintypes.HANDLE()
    try:
        # This is fixed probe code, not a model/code-execution path. The delay
        # provides a deterministic window to assign the child to its Job Object.
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())

        limits = _ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = 0x00002000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        process_handle = kernel32.OpenProcess(
            0x0001 | 0x0100 | 0x1000,  # terminate, set quota, query limited info
            False,
            process.pid,
        )
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

        kernel32.CloseHandle(process_handle)
        process_handle = wintypes.HANDLE()
        kernel32.CloseHandle(job)
        job = wintypes.HANDLE()
        process.wait(timeout=3)
        return result(
            "job_object_kill_on_close_smoke",
            PASS,
            "A fixed benign child terminated after its Job Object handle closed.",
            returncode=process.returncode,
        )
    except Exception as exc:  # pragma: no cover - exercised on Windows only
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        return result(
            "job_object_kill_on_close_smoke",
            FAIL,
            "The benign Job Object smoke test did not prove kill-on-close.",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        if process_handle:
            kernel32.CloseHandle(process_handle)
        if job:
            kernel32.CloseHandle(job)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def probe_named_pipe_smoke() -> dict[str, Any]:
    """Verify a bounded authenticated local named-pipe exchange."""

    if sys.platform != "win32":
        return result(
            "named_pipe_ipc_smoke",
            BLOCKED,
            "The named-pipe smoke test requires Windows.",
        )

    address = rf"\\.\pipe\cortex-phase0-{uuid4().hex}"
    authkey = uuid4().bytes
    listener: Listener | None = None
    child: subprocess.Popen[str] | None = None
    accepted: list[Any] = []
    accept_error: list[BaseException] = []

    def accept_one() -> None:
        try:
            assert listener is not None
            accepted.append(listener.accept())
        except BaseException as exc:  # pragma: no cover - Windows timing path
            accept_error.append(exc)

    try:
        listener = Listener(address=address, family="AF_PIPE", authkey=authkey)
        child_code = (
            "import sys; "
            "from multiprocessing.connection import Client; "
            "c=Client(sys.argv[1], family='AF_PIPE', authkey=bytes.fromhex(sys.argv[2])); "
            "c.send_bytes(b'cortex-phase0-ipc-v1'); c.close()"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, address, authkey.hex()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        accept_thread = threading.Thread(target=accept_one, daemon=True)
        accept_thread.start()
        accept_thread.join(timeout=5)
        if accept_thread.is_alive():
            raise TimeoutError("named-pipe accept timed out")
        if accept_error:
            raise accept_error[0]
        if not accepted:
            raise RuntimeError("named-pipe accept returned no connection")
        connection = accepted[0]
        payload = connection.recv_bytes(64)
        connection.close()
        child.wait(timeout=5)
        if payload != b"cortex-phase0-ipc-v1":
            raise ValueError(f"unexpected IPC payload: {payload!r}")
        return result(
            "named_pipe_ipc_smoke",
            PASS,
            "A fixed child exchanged one authenticated, length-bounded IPC frame.",
            payload_bytes=len(payload),
        )
    except Exception as exc:  # pragma: no cover - Windows timing path
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        return result(
            "named_pipe_ipc_smoke",
            FAIL,
            "The fixed named-pipe IPC smoke test did not complete safely.",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        for connection in accepted:
            try:
                connection.close()
            except Exception:
                pass
        if listener is not None:
            listener.close()
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def probe_wasmtime(run_smoke: bool) -> dict[str, Any]:
    spec = importlib.util.find_spec("wasmtime")
    if spec is None:
        return result(
            "wasmtime_guest_runtime",
            BLOCKED,
            "The optional Wasmtime Python package is not installed; no guest runtime was executed.",
            package="wasmtime",
        )
    if not run_smoke:
        return result(
            "wasmtime_guest_runtime",
            NOT_RUN,
            "Wasmtime is importable; rerun with --wasi-smoke to execute the fixed 42-returning module.",
            module_origin=str(spec.origin),
        )
    try:
        wasmtime = importlib.import_module("wasmtime")
        engine = wasmtime.Engine()
        module = wasmtime.Module(
            engine,
            "(module (func (export \"answer\") (result i32) i32.const 42))",
        )
        store = wasmtime.Store(engine)
        instance = wasmtime.Instance(store, module, [])
        value = instance.exports(store)["answer"](store)
        if value != 42:
            return result(
                "wasmtime_guest_runtime",
                FAIL,
                "The fixed Wasm smoke module returned an unexpected value.",
                value=value,
            )
        return result(
            "wasmtime_guest_runtime",
            PASS,
            "The fixed, side-effect-free Wasm module executed and returned 42.",
            module_origin=str(spec.origin),
            version=getattr(wasmtime, "__version__", "unknown"),
        )
    except Exception as exc:  # pragma: no cover - depends on optional package
        return result(
            "wasmtime_guest_runtime",
            FAIL,
            "Wasmtime imported but the fixed guest smoke test failed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def probe_wasmtime_controls(run_smoke: bool) -> dict[str, Any]:
    """Exercise fixed Wasmtime no-import and resource-limit controls."""

    spec = importlib.util.find_spec("wasmtime")
    if spec is None:
        return result(
            "wasmtime_runtime_controls",
            BLOCKED,
            "The optional Wasmtime package is not installed; resource-control evidence is unavailable.",
            package="wasmtime",
        )
    if not run_smoke:
        return result(
            "wasmtime_runtime_controls",
            NOT_RUN,
            "Rerun with --wasi-smoke to execute fixed no-import, fuel, and memory-limit probes.",
            module_origin=str(spec.origin),
        )

    try:
        wasmtime = importlib.import_module("wasmtime")
        checks: dict[str, bool] = {}

        # A module that requests a host import must not instantiate against an
        # empty import list. This is the first proof that host capabilities are
        # not silently supplied by the disposable runtime.
        engine = wasmtime.Engine()
        imported = wasmtime.Module(
            engine,
            '(module (import "env" "host" (func)))',
        )
        try:
            wasmtime.Instance(wasmtime.Store(engine), imported, [])
        except Exception:
            checks["no_import_denied"] = True
        else:
            checks["no_import_denied"] = False

        fuel_config = wasmtime.Config()
        fuel_config.consume_fuel = True
        fuel_engine = wasmtime.Engine(fuel_config)
        fuel_module = wasmtime.Module(
            fuel_engine,
            '(module (func (export "loop") (loop br 0)))',
        )
        fuel_store = wasmtime.Store(fuel_engine)
        fuel_store.set_fuel(1000)
        try:
            wasmtime.Instance(fuel_store, fuel_module, []).exports(fuel_store)["loop"](
                fuel_store
            )
        except Exception:
            checks["fuel_limit_enforced"] = True
        else:
            checks["fuel_limit_enforced"] = False

        memory_engine = wasmtime.Engine()
        memory_module = wasmtime.Module(memory_engine, "(module (memory 2))")
        memory_store = wasmtime.Store(memory_engine)
        memory_store.set_limits(memory_size=65536)
        try:
            wasmtime.Instance(memory_store, memory_module, [])
        except Exception:
            checks["memory_limit_enforced"] = True
        else:
            checks["memory_limit_enforced"] = False

        status = PASS if all(checks.values()) else FAIL
        return result(
            "wasmtime_runtime_controls",
            status,
            "Fixed Wasmtime probes enforced no host imports, fuel exhaustion, and a memory limit."
            if status == PASS
            else "One or more fixed Wasmtime control probes did not fail closed.",
            module_origin=str(spec.origin),
            checks=checks,
        )
    except Exception as exc:  # pragma: no cover - depends on optional package
        return result(
            "wasmtime_runtime_controls",
            FAIL,
            "Wasmtime imported but its fixed control probes failed unexpectedly.",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def probe_guest_language_qualification(run_smoke: bool) -> dict[str, Any]:
    """Run the isolated AssemblyScript-to-Wasm qualification helper."""

    if sys.platform != "win32":
        return result(
            "guest_language_qualification",
            BLOCKED,
            "The qualified AssemblyScript baseline targets Windows.",
        )
    helper = ROOT / "tools" / "execution_spikes" / "assemblyscript_qualification.py"
    if not run_smoke:
        return result(
            "guest_language_qualification",
            BLOCKED,
            "Rerun with --guest-language-smoke to qualify the pinned AssemblyScript guest language.",
            helper=str(helper),
        )
    if not helper.is_file():
        return result(
            "guest_language_qualification",
            BLOCKED,
            "The reviewed guest-language qualification helper is missing.",
            helper=str(helper),
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(helper), "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=240,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload.get("name") != "guest_language_qualification":
            raise ValueError("guest-language helper returned an unexpected check name")
        payload.setdefault("details", {})["helper_exit_code"] = completed.returncode
        if completed.stderr:
            payload["details"]["helper_stderr"] = completed.stderr[-2000:]
        return payload
    except subprocess.TimeoutExpired as exc:
        return result(
            "guest_language_qualification",
            FAIL,
            "The fixed guest-language helper exceeded its fail-closed timeout.",
            error_type=type(exc).__name__,
            timeout_seconds=240,
        )
    except Exception as exc:
        return result(
            "guest_language_qualification",
            FAIL,
            "The guest-language helper returned invalid evidence and the gate failed closed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def probe_containment_cancellation_corpus(run_smoke: bool) -> dict[str, Any]:
    """Run the fixed AppContainer process-tree cancellation corpus."""

    if sys.platform != "win32":
        return result(
            "containment_cancellation_corpus",
            BLOCKED,
            "The native cancellation corpus requires Windows.",
        )
    helper = ROOT / "tools" / "execution_spikes" / "cancellation_corpus.py"
    if not run_smoke:
        return result(
            "containment_cancellation_corpus",
            BLOCKED,
            "Rerun with --cancellation-smoke to prove AppContainer full-tree cancellation.",
            helper=str(helper),
        )
    if not helper.is_file():
        return result(
            "containment_cancellation_corpus",
            BLOCKED,
            "The reviewed cancellation corpus helper is missing.",
            helper=str(helper),
        )
    try:
        completed = subprocess.run(
            [sys.executable, str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload.get("name") != "containment_cancellation_corpus":
            raise ValueError("cancellation helper returned an unexpected check name")
        payload.setdefault("details", {})["helper_exit_code"] = completed.returncode
        if completed.stderr:
            payload["details"]["helper_stderr"] = completed.stderr[-2000:]
        return payload
    except subprocess.TimeoutExpired as exc:
        return result(
            "containment_cancellation_corpus",
            FAIL,
            "The native cancellation helper exceeded its fail-closed timeout.",
            error_type=type(exc).__name__,
            timeout_seconds=30,
        )
    except Exception as exc:
        return result(
            "containment_cancellation_corpus",
            FAIL,
            "The cancellation helper returned invalid evidence and the gate failed closed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def probe_security_review() -> dict[str, Any]:
    review = ROOT / "docs" / "adr" / "0001-phase0-security-review.md"
    if not review.is_file():
        return result(
            "security_review",
            BLOCKED,
            "The Phase 0 security review artifact has not been completed.",
            review=str(review),
        )
    return result(
        "security_review",
        PASS,
        "The Phase 0 security review artifact records reviewed threats, evidence, residual blockers, and the no-production-execution decision.",
        review=str(review),
    )


def probe_packaging() -> dict[str, Any]:
    spec = ROOT / "packaging" / "Cortex.spec"
    build_script = ROOT / "packaging" / "build_windows.ps1"
    frontend_index = ROOT / "frontend" / "dist" / "index.html"
    bootstrapper = (
        ROOT
        / "packaging"
        / ".runtime"
        / "webview2"
        / "MicrosoftEdgeWebview2Setup.exe"
    )
    pyinstaller = importlib.util.find_spec("PyInstaller") is not None
    required = {
        "spec": spec.is_file(),
        "build_script": build_script.is_file(),
        "frontend_index": frontend_index.is_file(),
        "webview2_bootstrapper": bootstrapper.is_file(),
        "pyinstaller_importable": pyinstaller,
    }
    ready = all(required.values())
    missing = [name for name, present in required.items() if not present]
    return result(
        "pyinstaller_package_preconditions",
        PASS if ready else BLOCKED,
        "All package inputs are present; a package build may be attempted."
        if ready
        else "The one-folder package cannot be built until required inputs are present.",
        required=required,
        missing=missing,
    )


def build_report(
    *,
    run_job_smoke: bool,
    run_ipc_smoke: bool,
    run_wasi_smoke: bool,
    run_appcontainer_smoke: bool = False,
    run_guest_language_smoke: bool = False,
    run_cancellation_smoke: bool = False,
) -> dict[str, Any]:
    checks = [
        probe_environment(),
        *probe_windows_api_surface(),
        probe_appcontainer_isolation_smoke(run_appcontainer_smoke),
        probe_job_object_smoke() if run_job_smoke else result(
            "job_object_kill_on_close_smoke",
            NOT_RUN,
            "Rerun with --job-smoke to launch only the fixed benign child probe.",
        ),
        probe_named_pipe_smoke() if run_ipc_smoke else result(
            "named_pipe_ipc_smoke",
            NOT_RUN,
            "Rerun with --ipc-smoke to launch only the fixed IPC child probe.",
        ),
        probe_wasmtime(run_wasi_smoke),
        probe_wasmtime_controls(run_wasi_smoke),
        probe_guest_language_qualification(run_guest_language_smoke),
        probe_containment_cancellation_corpus(run_cancellation_smoke),
        probe_security_review(),
        probe_packaging(),
    ]
    required_names = {
        "environment",
        "appcontainer_api_surface",
        "appcontainer_process_isolation_smoke",
        "job_object_api_surface",
        "named_pipe_api_surface",
        "job_object_kill_on_close_smoke",
        "named_pipe_ipc_smoke",
        "wasmtime_guest_runtime",
        "wasmtime_runtime_controls",
        "guest_language_qualification",
        "containment_cancellation_corpus",
        "security_review",
        "pyinstaller_package_preconditions",
    }
    required = [check for check in checks if check["name"] in required_names]
    ready = all(check["status"] == PASS for check in required)
    return {
        "probe": "cortex-execution-phase0",
        "schema_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository_root": str(ROOT),
        "checks": checks,
        "phase0_ready_for_phase1": ready,
        "phase0_status": PASS if ready else BLOCKED,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit compact JSON only.")
    parser.add_argument(
        "--job-smoke",
        action="store_true",
        help="Run the benign Windows Job Object kill-on-close smoke test.",
    )
    parser.add_argument(
        "--wasi-smoke",
        action="store_true",
        help="Run the fixed Wasmtime module smoke test if Wasmtime is installed.",
    )
    parser.add_argument(
        "--ipc-smoke",
        action="store_true",
        help="Run the fixed authenticated named-pipe IPC smoke test.",
    )
    parser.add_argument(
        "--appcontainer-smoke",
        action="store_true",
        help="Run the fixed native AppContainer filesystem/network containment corpus.",
    )
    parser.add_argument(
        "--guest-language-smoke",
        action="store_true",
        help="Run the fixed pinned AssemblyScript guest-language qualification.",
    )
    parser.add_argument(
        "--cancellation-smoke",
        action="store_true",
        help="Run the fixed hostile AppContainer process-tree cancellation corpus.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 when any required Phase 0 check is blocked or fails.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(
        run_job_smoke=args.job_smoke,
        run_ipc_smoke=args.ipc_smoke,
        run_wasi_smoke=args.wasi_smoke,
        run_appcontainer_smoke=args.appcontainer_smoke,
        run_guest_language_smoke=args.guest_language_smoke,
        run_cancellation_smoke=args.cancellation_smoke,
    )
    if args.json:
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.strict and report["phase0_status"] != PASS:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
