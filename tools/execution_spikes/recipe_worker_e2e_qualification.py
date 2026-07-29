"""Disposable signed-worker/AppContainer/broker qualification gate.

This probe is deliberately separate from Cortex runtime wiring.  It signs the
already-built fixed worker with an in-memory ephemeral Ed25519 key, installs it
into a disposable store, launches it through the reviewed AppContainer factory,
and exercises a fixed PNG transform, hostile decoder corpus, and in-flight
cancellation protocol corpus.  No user files, model text, commands, paths, or
production trust roots are accepted.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
from ctypes import wintypes
from queue import Queue
from tempfile import mkdtemp
from threading import Thread
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from PIL import Image  # noqa: E402

from cortex_backend.execution.broker import BrokerMessage  # noqa: E402
from cortex_backend.execution.bundle_installer import SignedBundleInstaller  # noqa: E402
from cortex_backend.execution.manifest import TrustedRecipeKeys  # noqa: E402
from cortex_backend.execution.native_broker import NativeBrokerConnection  # noqa: E402
from cortex_backend.execution.native_launcher import (  # noqa: E402
    BrokerWorkerBinding,
    NativeBrokerIdentityBinder,
    NativeWorkerLauncher,
)
from cortex_backend.execution.native_win32 import NativeWin32ProcessFactory  # noqa: E402
from cortex_backend.execution.recipes import parse_image_transform  # noqa: E402
from cortex_backend.execution.worker_protocol import (  # noqa: E402
    WorkerCancel,
    WorkerCollect,
    WorkerInputChunk,
    WorkerInputComplete,
    WorkerPrepare,
)
from cortex_backend.execution.worker_provenance import verify_active_worker  # noqa: E402
from cortex_backend.execution.worker_release import build_signed_worker_manifest  # noqa: E402


DEFAULT_TIMEOUT_SECONDS = 20.0
# The worker protocol permits 48 KiB chunks, but base64 plus the authenticated
# broker envelope must also fit within its 64 KiB frame ceiling.  Keep this
# qualification transport bound conservative so the test exercises the worker
# rather than failing at the outer framing layer.
QUALIFICATION_CHUNK_BYTES = 32 * 1024
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


def _current_user_sid() -> str:
    if os.name != "nt":
        raise RuntimeError("windows_required")
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise RuntimeError("user_sid_unavailable")
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        if not required.value:
            raise RuntimeError("user_sid_unavailable")
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, required, ctypes.byref(required)
        ):
            raise RuntimeError("user_sid_unavailable")
        user = _TokenUser.from_buffer_copy(buffer)
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(user.sid, ctypes.byref(sid_text)):
            raise RuntimeError("user_sid_unavailable")
        try:
            if not sid_text.value:
                raise RuntimeError("user_sid_unavailable")
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
            kernel32.CloseHandle(token)


def _process_executable(process_id: int) -> Path | None:
    """Return a process image path without adding a host dependency."""

    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value).resolve()
    finally:
        kernel32.CloseHandle(process)


def _fixed_png() -> bytes:
    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()


def _slow_png() -> bytes:
    """Return a deterministic bounded image large enough for cancellation timing."""

    image = Image.new("RGB", (4, 3), (120, 80, 40))
    try:
        with BytesIO() as stream:
            image.save(stream, format="PNG")
            return stream.getvalue()
    finally:
        image.close()


def _plan(*, artifact_id: str, slow: bool = False) -> Any:
    steps: list[dict[str, Any]] = [{"op": "grayscale"}]
    if slow:
        steps = [
            {"op": "resize", "width": 3500 - (index % 2), "height": 3500 - (index % 2)}
            for index in range(8)
        ]
    return parse_image_transform(
        {
            "schema_version": "artifact.transform.v1",
            "input_artifact_id": artifact_id,
            "steps": steps,
            "output_format": "png",
        }
    )


def _receive_with_timeout(
    connection: NativeBrokerConnection,
    timeout_seconds: float,
) -> BrokerMessage:
    result: Queue[object] = Queue(maxsize=1)

    def receive() -> None:
        try:
            result.put(connection.receive_message())
        except Exception as error:  # pragma: no cover - native failure path.
            result.put(error)

    Thread(target=receive, name="cortex-qualification-reader", daemon=True).start()
    try:
        value = result.get(timeout=timeout_seconds)
    except Exception:
        raise TimeoutError("broker response timeout") from None
    if isinstance(value, Exception):
        raise value
    return value


def _message(operation: str, model: Any, *, principal: str, job_id: str) -> BrokerMessage:
    return BrokerMessage(
        schema_version="broker.message.v1",
        direction="to_executor",
        operation=operation,
        request_id=model.request_id,
        job_id=job_id,
        installation_principal_id=principal,
        body=model.model_dump(mode="json"),
    )


def _response_code(message: BrokerMessage, operation: str) -> str:
    if message.operation != operation or message.body.get("request_id") != message.request_id:
        raise ValueError("broker response identity mismatch")
    return str(message.body.get("schema_version", ""))


def _bounded_cleanup(action: Any, timeout_seconds: float = 5.0) -> None:
    finished = Queue(maxsize=1)

    def run() -> None:
        try:
            action()
        finally:
            finished.put(True)

    thread = Thread(target=run, name="cortex-qualification-cleanup", daemon=True)
    thread.start()
    try:
        finished.get(timeout=timeout_seconds)
    except Exception:
        pass


def _install_ephemeral(source_root: Path, store_root: Path) -> SignedBundleInstaller:
    signer = Ed25519PrivateKey.generate()
    private_bytes = signer.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = signer.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    release = build_signed_worker_manifest(
        source_root,
        private_key_bytes=private_bytes,
        key_id="qualification-ephemeral",
        bundle_version="1.0.0",
        sequence=1,
    )
    installer = SignedBundleInstaller(
        store_root,
        TrustedRecipeKeys({"qualification-ephemeral": public_bytes}),
    )
    installer.install(release.manifest, source_root)
    return installer


def _close_worker_case(
    connection: NativeBrokerConnection | None,
    worker: Any,
    binder: NativeBrokerIdentityBinder,
    workspace: Path,
) -> None:
    """Boundedly close one disposable worker, broker binding, and process tree."""

    if connection is not None:
        _bounded_cleanup(connection.close)
    _bounded_cleanup(binder.close_binding)
    if worker is not None:
        _bounded_cleanup(worker.close)
        try:
            executable = _process_executable(worker.process_id)
            if executable is not None and executable.is_relative_to(workspace.resolve()):
                subprocess.run(
                    ["taskkill", "/PID", str(worker.process_id), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except Exception:
            pass


def qualify(
    source_root: Path,
    *,
    timeout_seconds: float,
    case_name: str | None = None,
) -> dict[str, Any]:
    if not source_root.is_dir():
        return {"status": "blocked", "code": "package_missing", "stages": []}
    workspace = Path(mkdtemp(prefix="cortex-worker-e2e-qualification-"))
    stages: list[str] = []
    cases: dict[str, dict[str, Any]] = {}
    binder: NativeBrokerIdentityBinder | None = None
    try:
        installer = _install_ephemeral(source_root, workspace / "store")
        stages.extend(["signed_ephemeral_manifest", "installed_immutable_generation"])
        verify_active_worker(installer)
        stages.append("provenance_verified")
        binder = NativeBrokerIdentityBinder(allowed_user_sids=frozenset({_current_user_sid()}))
        launcher = NativeWorkerLauncher(
            installer,
            process_factory=NativeWin32ProcessFactory(),
            broker_binder=binder,
        )

        def run_case(
            case_name: str,
            content: bytes,
            plan: Any,
            *,
            mode: str,
        ) -> None:
            principal = sha256(os.urandom(32)).hexdigest()
            job_id = f"qualification-{case_name}-{uuid4().hex[:12]}"
            request_id = f"qualification-{case_name}"
            binding = BrokerWorkerBinding(
                pipe_name=rf"\\.\pipe\cortex-qualification-{uuid4().hex}",
                broker_process_id=os.getpid(),
                installation_principal_id=principal,
                job_id=job_id,
            )
            connection: NativeBrokerConnection | None = None
            worker: Any = None
            case_stages: list[str] = []
            try:
                worker = launcher.launch(binding)
                case_stages.append("appcontainer_job_policy_and_identity_bound")
                accepted: Queue[object] = Queue(maxsize=1)

                def accept() -> None:
                    try:
                        accepted.put(
                            binder.accept(
                                owner_for_job=lambda value: principal if value == job_id else None
                            )
                        )
                    except Exception as error:  # pragma: no cover - native failure path.
                        accepted.put(error)

                Thread(target=accept, name="cortex-qualification-accept", daemon=True).start()
                try:
                    value = accepted.get(timeout=timeout_seconds)
                except Exception:
                    raise TimeoutError("broker handshake timeout") from None
                if isinstance(value, Exception):
                    raise value
                connection = value
                case_stages.append("authenticated_broker_handshake")

                digest = sha256(content).hexdigest()
                prepare = WorkerPrepare(
                    schema_version="recipe.worker.prepare.v1",
                    request_id=request_id,
                    job_id=job_id,
                    plan=plan,
                    input_size=len(content),
                    input_sha256=digest,
                    input_mime_type="image/png",
                )
                connection.send_message(_message("prepare", prepare, principal=principal, job_id=job_id))
                response = _receive_with_timeout(connection, timeout_seconds)
                if _response_code(response, "prepare") != "recipe.worker.ack.v1":
                    raise ValueError("prepare acknowledgement invalid")
                case_stages.append("prepare_ack")

                offset = 0
                chunk_number = 0
                while offset < len(content):
                    part = content[offset : offset + QUALIFICATION_CHUNK_BYTES]
                    chunk = WorkerInputChunk(
                        schema_version="recipe.worker.input_chunk.v1",
                        request_id=request_id,
                        job_id=job_id,
                        offset=offset,
                        data=base64.urlsafe_b64encode(part).decode("ascii").rstrip("="),
                        sha256=sha256(part).hexdigest(),
                    )
                    connection.send_message(
                        _message("input_chunk", chunk, principal=principal, job_id=job_id)
                    )
                    response = _receive_with_timeout(connection, timeout_seconds)
                    if _response_code(response, "input_chunk") != "recipe.worker.ack.v1":
                        raise ValueError("input acknowledgement invalid")
                    offset += len(part)
                    chunk_number += 1
                case_stages.append("input_chunk_ack")

                complete = WorkerInputComplete(
                    schema_version="recipe.worker.input_complete.v1",
                    request_id=request_id,
                    job_id=job_id,
                    input_size=len(content),
                    input_sha256=digest,
                )
                connection.send_message(
                    _message("input_complete", complete, principal=principal, job_id=job_id)
                )
                if mode == "cancel":
                    case_stages.append("input_complete_sent")
                    cancel = WorkerCancel(
                        schema_version="recipe.worker.cancel.v1",
                        request_id=request_id,
                        job_id=job_id,
                        reason="user",
                    )
                    connection.send_message(
                        _message("cancel", cancel, principal=principal, job_id=job_id)
                    )
                    response = _receive_with_timeout(connection, timeout_seconds)
                    if response.operation == "cancel" and _response_code(response, "cancel") == "recipe.worker.ack.v1":
                        case_stages.append("cancel_ack")
                    elif (
                        response.operation == "input_complete"
                        and response.body.get("schema_version") == "recipe.worker.error.v1"
                        and response.body.get("code") == "cancelled"
                    ):
                        case_stages.append("cancelled_result")
                    else:
                        raise ValueError(
                            "cancellation was not acknowledged before completion:"
                            f" operation={response.operation}"
                            f" schema={response.body.get('schema_version')}"
                            f" code={response.body.get('code')}"
                        )
                    cases[case_name] = {"status": "passed", "stages": case_stages, "chunks": chunk_number}
                    stages.extend(f"{case_name}:{stage}" for stage in case_stages)
                    return

                response = _receive_with_timeout(connection, timeout_seconds)
                if mode == "hostile":
                    if (
                        response.operation != "input_complete"
                        or response.body.get("schema_version") != "recipe.worker.error.v1"
                        or response.body.get("code") not in {"invalid_input", "decode_failed", "unsupported_format"}
                    ):
                        raise ValueError("hostile decoder input was not rejected safely")
                    case_stages.append("hostile_input_rejected")
                    cases[case_name] = {"status": "passed", "stages": case_stages, "chunks": chunk_number}
                    stages.extend(f"{case_name}:{stage}" for stage in case_stages)
                    return

                if response.operation != "input_complete":
                    raise ValueError("input completion response invalid")
                case_stages.append("input_complete_result")
                collect = WorkerCollect(
                    schema_version="recipe.worker.collect.v1",
                    request_id=request_id,
                    job_id=job_id,
                    offset=0,
                    max_bytes=QUALIFICATION_CHUNK_BYTES,
                )
                connection.send_message(_message("collect", collect, principal=principal, job_id=job_id))
                response = _receive_with_timeout(connection, timeout_seconds)
                if _response_code(response, "collect") != "recipe.worker.output_chunk.v1":
                    raise ValueError("output response invalid")
                case_stages.append("collect_output")
                cases[case_name] = {"status": "passed", "stages": case_stages, "chunks": chunk_number}
                stages.extend(f"{case_name}:{stage}" for stage in case_stages)
            except TimeoutError:
                cases[case_name] = {
                    "status": "blocked",
                    "stages": case_stages,
                    "error_type": "TimeoutError",
                    "error": "worker response timeout",
                }
                raise
            except Exception as error:
                cases[case_name] = {
                    "status": "blocked",
                    "stages": case_stages,
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
                raise
            finally:
                _close_worker_case(connection, worker, binder, workspace)

        selected_cases = (
            ("transform", _fixed_png(), _plan(artifact_id="qualification-transform"), "success"),
            (
                "hostile_truncated_png",
                b"\x89PNG\r\n\x1a\ntruncated",
                _plan(artifact_id="qualification-hostile-truncated"),
                "hostile",
            ),
            (
                "hostile_active_svg",
                b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>",
                _plan(artifact_id="qualification-hostile-svg"),
                "hostile",
            ),
            (
                "cancellation",
                _slow_png(),
                _plan(artifact_id="qualification-cancellation", slow=True),
                "cancel",
            ),
        )
        for selected_name, content, plan, mode in selected_cases:
            if case_name is None or case_name == selected_name:
                run_case(selected_name, content, plan, mode=mode)
        return {"status": "passed", "stages": stages, "cases": cases}
    except TimeoutError:
        return {"status": "blocked", "code": "worker_response_timeout", "stages": stages, "cases": cases}
    except Exception:
        return {"status": "blocked", "code": "qualification_failed_closed", "stages": stages, "cases": cases}
    finally:
        if binder is not None:
            _bounded_cleanup(binder.close_binding)
        try:
            shutil.rmtree(workspace)
        except OSError:
            # The disposable AppContainer generation is never reused; cleanup
            # failure remains visible in the local process but is not a pass.
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT / "dist" / "recipe-runtime")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--case",
        choices=("transform", "hostile_truncated_png", "hostile_active_svg", "cancellation"),
        help="qualify one case instead of the full signed-worker corpus",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be between 1 and 120")
    result = qualify(
        args.source_root.resolve(),
        timeout_seconds=args.timeout_seconds,
        case_name=args.case,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":") if args.json else None))
    return 2 if args.strict and result["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
