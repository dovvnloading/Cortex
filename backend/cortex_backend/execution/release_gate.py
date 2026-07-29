"""Fail-closed Phase 2 release and lifecycle health preflight.

The qualification probes prove individual controls, while this module composes
the controls that a future ``ExecutionLifecycle`` health callback must require
before exposing a recipe provider.  It performs read-only provenance and shape
checks only; it never launches a process, opens a broker, loads a provider, or
turns the lifecycle on by itself.

External review is intentionally an explicit injected result.  A production
caller must supply a separately verified review attestation; a missing,
malformed, or failed callback keeps the runtime unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Literal, Protocol

from .bundle_installer import SignedBundleInstaller
from .lifecycle import RuntimeHealth
from .worker_provenance import WorkerProvenanceError, verify_active_worker


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")


class ExternalReviewProbe(Protocol):
    """Return a trust-anchored external-review health result."""

    def __call__(self) -> RuntimeHealth:
        """Return only a bounded, safe review result."""


@dataclass(frozen=True, slots=True)
class ReleaseGateCheck:
    """Safe status for one mandatory release control."""

    name: str
    available: bool
    code: str

    def __post_init__(self) -> None:
        if _SAFE_CODE.fullmatch(self.name) is None:
            raise ValueError("release gate check name is invalid")
        if _SAFE_CODE.fullmatch(self.code) is None:
            raise ValueError("release gate check code is invalid")
        if type(self.available) is not bool:
            raise TypeError("release gate check availability must be boolean")


@dataclass(frozen=True, slots=True)
class ReleaseGateSnapshot:
    """Public-safe release preflight result suitable for lifecycle diagnostics."""

    health: RuntimeHealth
    checks: tuple[ReleaseGateCheck, ...]

    @property
    def available(self) -> bool:
        return self.health.available and all(check.available for check in self.checks)


def _adapter_available(value: object, *methods: str) -> bool:
    return value is not None and all(callable(getattr(value, method, None)) for method in methods)


class RecipeRuntimeReleaseGate:
    """Compose controls for an official or explicit qualification profile.

    The gate is intentionally conservative.  It reports the first missing
    control in a fixed order so diagnostics are deterministic and no internal
    paths, exception text, or token values cross the lifecycle boundary. The
    default ``official`` profile requires an external review callback; the
    explicit ``qualification`` profile is for local/CI development with
    disposable signing material and does not require that optional release gate.
    """

    def __init__(
        self,
        installer: SignedBundleInstaller,
        *,
        process_factory: object | None = None,
        broker_binder: object | None = None,
        external_review_check: ExternalReviewProbe | None = None,
        platform_name: str | None = None,
        release_profile: Literal["official", "qualification"] = "official",
    ) -> None:
        if not isinstance(installer, SignedBundleInstaller):
            raise TypeError("installer must be a SignedBundleInstaller")
        if platform_name is not None and not isinstance(platform_name, str):
            raise TypeError("platform name must be a string")
        if external_review_check is not None and not callable(external_review_check):
            raise TypeError("external review check must be callable")
        if not isinstance(release_profile, str) or release_profile not in {
            "official",
            "qualification",
        }:
            raise ValueError("release profile must be official or qualification")
        self._installer = installer
        self._process_factory = process_factory
        self._broker_binder = broker_binder
        self._external_review_check = external_review_check
        self._platform_name = platform_name or os.name
        self._release_profile = release_profile

    @property
    def release_profile(self) -> Literal["official", "qualification"]:
        """Return the immutable profile selected for this preflight."""

        return self._release_profile

    @staticmethod
    def _blocked(
        checks: list[ReleaseGateCheck],
        *,
        check_name: str,
        code: str,
        message: str,
    ) -> ReleaseGateSnapshot:
        checks.append(ReleaseGateCheck(check_name, False, code))
        return ReleaseGateSnapshot(
            health=RuntimeHealth.blocked(code, message),
            checks=tuple(checks),
        )

    def check(self) -> ReleaseGateSnapshot:
        """Run deterministic read-only checks for lifecycle health gating."""

        checks: list[ReleaseGateCheck] = []
        if self._platform_name != "nt":
            return self._blocked(
                checks,
                check_name="native_platform",
                code="native_windows_required",
                message="The recipe runtime requires the reviewed Windows sandbox.",
            )
        checks.append(ReleaseGateCheck("native_platform", True, "ok"))

        try:
            verify_active_worker(self._installer)
        except WorkerProvenanceError as error:
            return self._blocked(
                checks,
                check_name="signed_worker",
                code=error.code,
                message="The signed recipe worker is not available for release.",
            )
        except Exception:
            return self._blocked(
                checks,
                check_name="signed_worker",
                code="worker_provenance_check_failed",
                message="The signed recipe worker could not be verified safely.",
            )
        checks.append(ReleaseGateCheck("signed_worker", True, "verified"))

        if not _adapter_available(self._process_factory, "create_suspended"):
            return self._blocked(
                checks,
                check_name="native_process_factory",
                code="native_process_factory_required",
                message="The reviewed native process factory is not configured.",
            )
        checks.append(ReleaseGateCheck("native_process_factory", True, "configured"))

        if not _adapter_available(self._broker_binder, "bind_worker", "close_binding"):
            return self._blocked(
                checks,
                check_name="native_broker_binding",
                code="native_broker_binding_required",
                message="The live native broker identity binder is not configured.",
            )
        checks.append(ReleaseGateCheck("native_broker_binding", True, "configured"))

        if self._release_profile == "qualification":
            checks.append(ReleaseGateCheck("external_review", True, "qualification_profile"))
            return ReleaseGateSnapshot(
                health=RuntimeHealth.ready("Recipe runtime qualification controls are ready."),
                checks=tuple(checks),
            )

        if self._external_review_check is None:
            return self._blocked(
                checks,
                check_name="external_review",
                code="external_review_required",
                message="External security review is required before provider enablement.",
            )
        try:
            review = self._external_review_check()
        except Exception:
            return self._blocked(
                checks,
                check_name="external_review",
                code="external_review_check_failed",
                message="External security review could not be verified safely.",
            )
        if not isinstance(review, RuntimeHealth):
            return self._blocked(
                checks,
                check_name="external_review",
                code="external_review_result_invalid",
                message="External security review returned an invalid result.",
            )
        if not review.available:
            return self._blocked(
                checks,
                check_name="external_review",
                code="external_review_unavailable",
                message="External security review has not approved provider enablement.",
            )
        checks.append(ReleaseGateCheck("external_review", True, "approved"))
        return ReleaseGateSnapshot(
            health=RuntimeHealth.ready("Recipe runtime release controls are ready."),
            checks=tuple(checks),
        )

    def health(self) -> RuntimeHealth:
        """Return only the bounded health value expected by ``ExecutionLifecycle``."""

        return self.check().health


__all__ = [
    "ExternalReviewProbe",
    "RecipeRuntimeReleaseGate",
    "ReleaseGateCheck",
    "ReleaseGateSnapshot",
]
