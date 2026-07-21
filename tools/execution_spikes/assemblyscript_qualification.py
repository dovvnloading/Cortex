"""Disposable AssemblyScript guest-language qualification.

The probe downloads one pinned AssemblyScript package from npm, verifies the
tarball and dependency lock metadata, compiles only fixed TypeScript-like guest
sources, and executes the resulting Wasm through the already-qualified Wasmtime
Python package. It never accepts source paths or model text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
from uuid import uuid4


VERSION = "0.28.19"
TARBALL_SHA512 = (
    "C8E02501BF44B2B234D2851A7FAA944ED4DC61879E946DA20B77F52E7038E575"
    "1E4A532BA763B6145E4B963E0FF98F3866222587F722104A4E68FC3098E2AAF2"
)
EXPECTED_INTEGRITY = {
    "assemblyscript": "sha512-yOAlAb9EsrI00oUaf6qUTtTcYYeelG2iC3f1LnA45XUeSlMrp2O2FF5Llj4P+Y84ZiIlh/ciEEpOaPwwmOKq8g==",
    "binaryen": "sha512-Zyl9Tw638x08LDew22YtxdYiUGxn+quzpR3ySIS4Nccv6yAiO5j1Yko9IEPNpj/aBZf71xtD/6hYXsgZ2ye3ew==",
    "long": "sha512-mNAgZ1GmyNhD7AuqnTG3/VQ26o760+ZYBPKjPvugO8+nLbYfX6TVpJPseBvopbdY+qpZ/lKUnmEc1LeZYS3QAA==",
}


def _result(status: str, evidence: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "guest_language_qualification",
        "status": status,
        "evidence": evidence,
    }
    if details:
        payload["details"] = details
    return payload


def _run(command: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _compile(asc: Path, source: Path, output: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "node",
            str(asc),
            str(source),
            "--target",
            "release",
            "--noAssert",
            "-o",
            str(output),
        ],
        cwd=cwd,
    )


def run() -> dict[str, Any]:
    if sys.platform != "win32":
        return _result(BLOCKED, "The qualified baseline targets Windows and requires Node.js and Wasmtime.")

    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not node or not npm:
        return _result(
            BLOCKED,
            "Node.js and npm are required to compile the pinned guest-language qualification.",
            node=node,
            npm=npm,
        )

    try:
        import wasmtime  # type: ignore[import-not-found]
    except Exception as exc:
        return _result(
            BLOCKED,
            "The pinned Wasmtime Python package is not importable; no guest-language module was executed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    root = Path(tempfile.mkdtemp(prefix=f"cortex-assemblyscript-phase0-{uuid4().hex}-"))
    try:
        package_result = _run(
            [npm, "pack", f"assemblyscript@{VERSION}", "--pack-destination", str(root)],
            cwd=root,
            timeout=60,
        )
        tarball = root / f"assemblyscript-{VERSION}.tgz"
        if package_result.returncode != 0 or not tarball.is_file():
            raise RuntimeError(f"npm pack failed: {package_result.stderr[-1000:]}")
        actual_sha512 = _sha512(tarball)
        if actual_sha512 != TARBALL_SHA512:
            raise RuntimeError(f"AssemblyScript tarball hash mismatch: {actual_sha512}")

        install_result = _run(
            [
                npm,
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--prefix",
                str(root),
                str(tarball),
            ],
            cwd=root,
            timeout=120,
        )
        if install_result.returncode != 0:
            raise RuntimeError(f"npm install failed: {install_result.stderr[-1000:]}")

        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        packages = lock.get("packages", {})
        lock_integrity = {
            "assemblyscript": packages.get("node_modules/assemblyscript", {}).get("integrity"),
            "binaryen": packages.get("node_modules/binaryen", {}).get("integrity"),
            "long": packages.get("node_modules/long", {}).get("integrity"),
        }
        if lock_integrity != EXPECTED_INTEGRITY:
            raise RuntimeError(f"dependency integrity mismatch: {lock_integrity}")

        node_modules = root / "node_modules"
        native_files = [
            str(path.relative_to(node_modules))
            for path in node_modules.rglob("*")
            if path.is_file() and path.suffix.lower() in {".node", ".dll", ".exe"}
        ]
        if native_files:
            raise RuntimeError(f"guest compiler dependency contains native files: {native_files}")

        source = root / "guest.ts"
        source.write_text(
            "export function answer(): i32 { return 42; }\n"
            "export function add(a: i32, b: i32): i32 { return a + b; }\n"
            "export function spin(): void { while (true) {} }\n",
            encoding="utf-8",
        )
        asc = root / "node_modules" / "assemblyscript" / "bin" / "asc.js"
        first = root / "guest-one.wasm"
        second = root / "guest-two.wasm"
        first_start = time.perf_counter()
        first_result = _compile(asc, source, first, root)
        first_seconds = time.perf_counter() - first_start
        second_result = _compile(asc, source, second, root)
        if first_result.returncode != 0 or second_result.returncode != 0:
            raise RuntimeError(
                "AssemblyScript compile failed: "
                + (first_result.stderr[-1000:] or second_result.stderr[-1000:])
            )
        first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
        second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
        if first_hash != second_hash:
            raise RuntimeError(f"non-deterministic Wasm output: {first_hash} != {second_hash}")

        engine = wasmtime.Engine()
        module = wasmtime.Module.from_file(engine, str(first))
        imports = list(module.imports)
        if imports:
            raise RuntimeError(f"qualified module unexpectedly imports host capabilities: {imports}")
        store = wasmtime.Store(engine)
        instance = wasmtime.Instance(store, module, [])
        answer = instance.exports(store)["answer"](store)
        added = instance.exports(store)["add"](store, 20, 22)
        if answer != 42 or added != 42:
            raise RuntimeError(f"unexpected guest results: answer={answer}, add={added}")

        fuel_config = wasmtime.Config()
        fuel_config.consume_fuel = True
        fuel_engine = wasmtime.Engine(fuel_config)
        fuel_module = wasmtime.Module.from_file(fuel_engine, str(first))
        fuel_store = wasmtime.Store(fuel_engine)
        fuel_store.set_fuel(1000)
        fuel_instance = wasmtime.Instance(fuel_store, fuel_module, [])
        try:
            fuel_instance.exports(fuel_store)["spin"](fuel_store)
        except Exception:
            spin_trapped = True
        else:
            spin_trapped = False
        if not spin_trapped:
            raise RuntimeError("guest spin function did not trap under the fuel budget")

        return _result(
            "pass",
            "AssemblyScript 0.28.19 compiled deterministic TypeScript-like guest code to a host-import-free Wasm module; Wasmtime executed arithmetic and trapped an infinite guest loop under fuel.",
            version=VERSION,
            tarball_sha512=actual_sha512,
            dependency_integrity=lock_integrity,
            module_sha256=first_hash,
            module_bytes=first.stat().st_size,
            compile_seconds=round(first_seconds, 4),
            imports=len(imports),
            answer=answer,
            add_result=added,
            spin_trapped=spin_trapped,
            native_dependency_files=native_files,
        )
    except subprocess.TimeoutExpired as exc:
        return _result(
            "fail",
            "The fixed AssemblyScript qualification exceeded its fail-closed timeout.",
            error_type=type(exc).__name__,
        )
    except Exception as exc:
        return _result(
            "fail",
            "The fixed AssemblyScript qualification failed closed before the guest language could be accepted.",
            error_type=type(exc).__name__,
            error=str(exc),
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit compact JSON only.")
    args = parser.parse_args()
    report = run()
    if args.json:
        print(json.dumps(report, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
