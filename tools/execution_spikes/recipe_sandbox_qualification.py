"""Disposable qualification harness for the future Windows recipe worker.

This is a release-gate probe, not a provider or an execution fallback.  It runs
the already-reviewed fixed AppContainer and cancellation helpers out of process,
exercises a tiny fixed decoder corpus against the qualification-only core, and
then requires a signed, packaged recipe worker before it can report green.  The
worker package is intentionally absent in this stage.  A missing package,
signature keyring, or sandbox control therefore produces ``blocked`` rather than
silently running Pillow in the Cortex host process.

No command, source text, uploaded path, network target, or model input is
accepted.  The only filesystem location inspected is the repository's fixed
future package location.  This module is never imported by Cortex production
code or included as a PyInstaller hidden import.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[2]
PASS = "pass"
BLOCKED = "blocked"
FAIL = "fail"

# The location is deliberately fixed.  A future packaging change must update
# this constant and its ADR rather than allowing an operator/model-selected path.
EXPECTED_WORKER_ROOT = ROOT / "packaging" / "recipe-runtime"
EXPECTED_WORKER_ENTRYPOINT = "recipe_worker.exe"

# A one-pixel PNG is sufficient to exercise the complete provider boundary while
# keeping this probe deterministic and tiny.  It is not user or model input.
_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _result(name: str, status: str, evidence: str, **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "status": status, "evidence": evidence}
    if details:
        payload["details"] = details
    return payload


def _run_fixed_helper(
    helper: Path,
    expected_name: str,
    timeout_seconds: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run one reviewed helper without inheriting stdin or accepting arguments."""

    if not helper.is_file():
        return _result(
            expected_name,
            BLOCKED,
            "The reviewed fixed helper is missing; the sandbox gate fails closed.",
            helper=str(helper),
        )
    try:
        completed = runner(
            [sys.executable, str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        payload = json.loads(completed.stdout)
        if payload.get("name") != expected_name:
            raise ValueError("fixed helper returned an unexpected check name")
        if payload.get("status") == PASS and completed.returncode != 0:
            raise ValueError("fixed helper reported pass with a nonzero exit code")
        payload.setdefault("details", {})["helper_exit_code"] = completed.returncode
        if completed.stderr:
            payload["details"]["helper_stderr"] = completed.stderr[-2000:]
        return payload
    except subprocess.TimeoutExpired as exc:
        return _result(
            expected_name,
            FAIL,
            "The fixed sandbox helper exceeded its fail-closed watchdog.",
            error_type=type(exc).__name__,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return _result(
            expected_name,
            FAIL,
            "The fixed sandbox helper returned invalid evidence and the gate failed closed.",
            error_type=type(exc).__name__,
            error=str(exc),
        )


def _probe_os_controls() -> list[dict[str, Any]]:
    """Run the existing native containment and cancellation helpers out of process."""

    if sys.platform != "win32":
        return [
            _result(
                "recipe_appcontainer_control",
                BLOCKED,
                "Recipe sandbox qualification requires Windows AppContainer/LPAC APIs.",
            ),
            _result(
                "recipe_cancellation_control",
                BLOCKED,
                "Recipe sandbox qualification requires Windows Job Object APIs.",
            ),
        ]

    appcontainer = ROOT / "tools" / "execution_spikes" / "appcontainer_smoke.py"
    cancellation = ROOT / "tools" / "execution_spikes" / "cancellation_corpus.py"
    isolation = _run_fixed_helper(
        appcontainer,
        "appcontainer_process_isolation_smoke",
        20,
    )
    cancellation_result = _run_fixed_helper(
        cancellation,
        "containment_cancellation_corpus",
        30,
    )
    # Preserve the stable Phase 0 helper names while exposing recipe-specific
    # gate names in this report.  No PASS is inferred from a missing detail.
    isolation["name"] = "recipe_appcontainer_control"
    cancellation_result["name"] = "recipe_cancellation_control"
    return [isolation, cancellation_result]


def _probe_provider_core() -> dict[str, Any]:
    """Exercise fixed decoder cases without claiming OS sandbox attestation."""

    try:
        sys.path.insert(0, str(ROOT / "backend"))
        from cortex_backend.execution.lifecycle import RuntimeHealth
        from cortex_backend.execution.recipe_provider import (
            RecipeImageProvider,
            RecipeProviderError,
        )
        from cortex_backend.execution.recipes import parse_image_transform
    except Exception as exc:
        return _result(
            "recipe_decoder_corpus",
            BLOCKED,
            "The qualification-only decoder dependency is unavailable; no host fallback is permitted.",
            error_type=type(exc).__name__,
        )

    provider = RecipeImageProvider()
    health = provider.start(RuntimeHealth.ready("qualification-only core; no sandbox attestation"))
    if not health.available:
        return _result(
            "recipe_decoder_corpus",
            BLOCKED,
            "The qualification-only decoder core did not pass its local capability check.",
            health_code=health.code,
        )

    try:
        plan = parse_image_transform(
            {
                "schema_version": "artifact.transform.v1",
                "input_artifact_id": "qualification-input",
                "steps": [{"op": "grayscale"}],
                "output_format": "png",
                "strip_metadata": True,
            }
        )
    except Exception as exc:
        provider.stop()
        return _result(
            "recipe_decoder_corpus",
            FAIL,
            "The fixed decoder plan could not be parsed; the qualification gate failed closed.",
            error_type=type(exc).__name__,
            provider_enabled=provider.enabled,
        )
    cases: dict[str, str] = {}
    try:
        output = provider.transform(plan, _ONE_PIXEL_PNG)
        cases["valid_allowlisted_png"] = (
            "pass" if output.mime_type == "image/png" else "fail"
        )
    except Exception as exc:
        cases["valid_allowlisted_png"] = f"fail:{type(exc).__name__}"

    for case_name, payload in {
        "truncated_png": b"\x89PNG\r\n\x1a\ntruncated",
        "active_svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>",
    }.items():
        try:
            provider.transform(plan, payload)
        except RecipeProviderError as exc:
            cases[case_name] = (
                "pass"
                if exc.code in {"decode_failed", "unsupported_format", "invalid_input"}
                else f"fail:{exc.code}"
            )
        except Exception as exc:
            cases[case_name] = f"fail:{type(exc).__name__}"
        else:
            cases[case_name] = "fail:accepted"

    provider.stop()
    passed = all(value == "pass" for value in cases.values())
    return _result(
        "recipe_decoder_corpus",
        PASS if passed else FAIL,
        "Fixed allowlisted/hostile bytes exercised the qualification-only core; this is not sandbox evidence.",
        cases=cases,
        sandboxed=False,
        sandbox_attestation_required=True,
        provider_enabled=provider.enabled,
    )


def _is_reparse(path: Path) -> bool:
    """Reject symlink/junction-like package entries before any future launch."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _probe_signed_worker_precondition() -> dict[str, Any]:
    """Require the future signed worker package without accepting a fallback."""

    root = EXPECTED_WORKER_ROOT
    if not root.exists():
        return _result(
            "recipe_signed_worker_provenance",
            BLOCKED,
            "The signed recipe worker package is not shipped yet; no provider process may start.",
            expected_root=str(root),
            expected_entrypoint=EXPECTED_WORKER_ENTRYPOINT,
            signature_verified=False,
        )
    if _is_reparse(root) or not root.is_dir():
        return _result(
            "recipe_signed_worker_provenance",
            FAIL,
            "The fixed recipe worker root is not a private, ordinary directory.",
            expected_root=str(root),
            signature_verified=False,
        )
    manifest = root / "manifest.json"
    entrypoint = root / EXPECTED_WORKER_ENTRYPOINT
    if _is_reparse(manifest) or _is_reparse(entrypoint):
        return _result(
            "recipe_signed_worker_provenance",
            FAIL,
            "The fixed recipe worker package contains a reparse-point entry.",
            signature_verified=False,
            launch_refused=True,
        )
    if not manifest.is_file() or not entrypoint.is_file():
        return _result(
            "recipe_signed_worker_provenance",
            BLOCKED,
            "The worker package is incomplete; signature and entrypoint verification are required.",
            manifest_present=manifest.is_file(),
            entrypoint_present=entrypoint.is_file(),
            signature_verified=False,
        )
    # A future implementation must verify the manifest with the packaged trust
    # root and verify every declared byte before launching.  Do not infer trust
    # from the presence of files or a self-reported digest today.
    return _result(
        "recipe_signed_worker_provenance",
        BLOCKED,
        "A worker directory exists but signed-manifest trust-root verification is not implemented in this qualification stage.",
        expected_root=str(root),
        signature_verified=False,
        launch_refused=True,
    )


def _probe_future_worker_controls() -> list[dict[str, Any]]:
    """Run the fixed launcher policy probe and expose remaining blockers."""

    helper = ROOT / "tools" / "execution_spikes" / "native_launcher_qualification.py"
    launcher = _run_fixed_helper(
        helper,
        "cortex-native-launcher-qualification",
        30,
    )
    launcher["name"] = "recipe_native_launcher_policy"

    return [
        launcher,
        _result(
            "recipe_resource_controls",
            BLOCKED,
            "The fixed resource-policy spike reports its configured/queryable limits, but release still requires a real worker launch and external enforcement review.",
            launch_refused=True,
        ),
        _result(
            "recipe_broker_identity",
            BLOCKED,
            "The native broker transport and launcher binder are qualified separately; a signed installed worker still needs a real PID/token handshake.",
            launch_refused=True,
        ),
    ]


def build_report() -> dict[str, Any]:
    """Build the stable qualification report without modifying Cortex state."""

    checks = [
        *_probe_os_controls(),
        _probe_provider_core(),
        _probe_signed_worker_precondition(),
        *_probe_future_worker_controls(),
    ]
    ready = all(check["status"] == PASS for check in checks)
    return {
        "probe": "cortex-recipe-sandbox-qualification",
        "schema_version": 1,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repository_root": str(ROOT),
        "checks": checks,
        "provider_launch_authorized": False,
        "qualification_status": PASS if ready else BLOCKED,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit compact JSON only.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 unless every sandbox qualification check is green.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
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
