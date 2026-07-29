"""Bounded execution budgets, monotonic watchdogs, and redacted accounting.

The coordinator and worker use this module as a policy-only contract.  It never
discovers paths, starts processes, or grants capabilities.  Native Job Objects
remain responsible for OS enforcement; this layer makes the limits and their
failure precedence deterministic, validates monotonic samples, and prevents a
missing or regressing accounting source from becoming a green result.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from time import monotonic
from typing import Callable, Final


_SAFE_PROFILE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_WALL_TIME_MS: Final[int] = 600_000
MAX_CPU_TIME_MS: Final[int] = 600_000
MAX_MEMORY_BYTES: Final[int] = 1024 * 1024 * 1024
MAX_MESSAGES: Final[int] = 16_384
MAX_COUNTER: Final[int] = 1 << 40
MAX_CONSOLE_BYTES: Final[int] = 1 * 1024 * 1024
MAX_OBSERVATION_BYTES: Final[int] = 64 * 1024


class ResourceAccountingError(ValueError):
    """Stable resource/watchdog category with no host details."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("invalid resource accounting code")
        self.code = code
        super().__init__("The execution resource budget was exceeded safely.")


def _bounded_int(value: int, *, maximum: int, allow_zero: bool = False) -> int:
    lower = 0 if allow_zero else 1
    if type(value) is not int or not lower <= value <= maximum:
        raise ValueError("resource value is outside its hard ceiling")
    return value


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Immutable logical budget; callers can only select values under hard caps."""

    profile: str
    wall_time_ms: int
    cpu_time_ms: int
    memory_bytes: int
    max_messages: int
    max_bytes_read: int
    max_bytes_written: int
    max_console_bytes: int = MAX_CONSOLE_BYTES
    max_observation_bytes: int = MAX_OBSERVATION_BYTES
    idle_timeout_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile, str) or _SAFE_PROFILE.fullmatch(self.profile) is None:
            raise ValueError("resource profile is invalid")
        _bounded_int(self.wall_time_ms, maximum=MAX_WALL_TIME_MS)
        _bounded_int(self.cpu_time_ms, maximum=MAX_CPU_TIME_MS)
        _bounded_int(self.memory_bytes, maximum=MAX_MEMORY_BYTES)
        _bounded_int(self.max_messages, maximum=MAX_MESSAGES)
        _bounded_int(self.max_bytes_read, maximum=MAX_COUNTER, allow_zero=True)
        _bounded_int(self.max_bytes_written, maximum=MAX_COUNTER, allow_zero=True)
        _bounded_int(self.max_console_bytes, maximum=MAX_CONSOLE_BYTES, allow_zero=True)
        _bounded_int(self.max_observation_bytes, maximum=MAX_OBSERVATION_BYTES, allow_zero=True)
        if self.idle_timeout_ms is not None:
            _bounded_int(self.idle_timeout_ms, maximum=MAX_WALL_TIME_MS)
            if self.idle_timeout_ms > self.wall_time_ms:
                raise ValueError("idle timeout cannot exceed wall budget")

    @classmethod
    def scratch_auto_v1(cls) -> "ResourceBudget":
        return cls(
            profile="scratch.auto.v1",
            wall_time_ms=10_000,
            cpu_time_ms=5_000,
            memory_bytes=256 * 1024 * 1024,
            max_messages=256,
            max_bytes_read=0,
            max_bytes_written=0,
            idle_timeout_ms=10_000,
        )

    @classmethod
    def artifact_transform_v1(cls) -> "ResourceBudget":
        return cls(
            profile="artifact.transform.v1",
            wall_time_ms=60_000,
            cpu_time_ms=30_000,
            memory_bytes=512 * 1024 * 1024,
            max_messages=16_384,
            max_bytes_read=100 * 1024 * 1024,
            max_bytes_written=128 * 1024 * 1024,
            idle_timeout_ms=60_000,
        )

    def with_watchdog(
        self,
        *,
        wall_time_ms: int | None = None,
        idle_timeout_ms: int | None = None,
        max_messages: int | None = None,
    ) -> "ResourceBudget":
        """Return a bounded test/launch override without mutating this budget."""

        return replace(
            self,
            wall_time_ms=self.wall_time_ms if wall_time_ms is None else wall_time_ms,
            idle_timeout_ms=(
                self.idle_timeout_ms
                if idle_timeout_ms is None
                else idle_timeout_ms
            ),
            max_messages=self.max_messages if max_messages is None else max_messages,
        )


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """One cumulative sample from the worker or native Job Object."""

    cpu_time_ms: int = 0
    peak_memory_bytes: int | None = None
    bytes_read: int = 0
    bytes_written: int = 0
    messages: int = 0
    console_bytes: int = 0
    observation_bytes: int = 0

    def __post_init__(self) -> None:
        _bounded_int(self.cpu_time_ms, maximum=MAX_COUNTER, allow_zero=True)
        if self.peak_memory_bytes is not None:
            _bounded_int(self.peak_memory_bytes, maximum=MAX_MEMORY_BYTES, allow_zero=True)
        _bounded_int(self.bytes_read, maximum=MAX_COUNTER, allow_zero=True)
        _bounded_int(self.bytes_written, maximum=MAX_COUNTER, allow_zero=True)
        _bounded_int(self.messages, maximum=MAX_MESSAGES, allow_zero=True)
        _bounded_int(self.console_bytes, maximum=MAX_COUNTER, allow_zero=True)
        _bounded_int(self.observation_bytes, maximum=MAX_COUNTER, allow_zero=True)


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Safe cumulative usage returned to the coordinator/UI diagnostics."""

    wall_time_ms: int = 0
    cpu_time_ms: int = 0
    peak_memory_bytes: int | None = None
    bytes_read: int = 0
    bytes_written: int = 0
    messages: int = 0
    console_bytes: int = 0
    observation_bytes: int = 0

    def __post_init__(self) -> None:
        # A terminal sample can be observed after a deadline has already
        # elapsed.  Keep that value representable so the governor can return
        # the stable ``deadline_exceeded`` category instead of leaking a
        # validation exception.
        _bounded_int(self.wall_time_ms, maximum=MAX_COUNTER, allow_zero=True)
        ResourceSample(
            cpu_time_ms=self.cpu_time_ms,
            peak_memory_bytes=self.peak_memory_bytes,
            bytes_read=self.bytes_read,
            bytes_written=self.bytes_written,
            messages=self.messages,
            console_bytes=self.console_bytes,
            observation_bytes=self.observation_bytes,
        )

    @property
    def accounting_complete(self) -> bool:
        """Memory accounting is required for a release-qualified terminal result."""

        return self.peak_memory_bytes is not None


class MonotonicWatchdog:
    """Wall/idle watchdog that rejects clock regressions and invalid samples."""

    def __init__(
        self,
        *,
        wall_time_ms: int,
        idle_timeout_ms: int | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        _bounded_int(wall_time_ms, maximum=MAX_WALL_TIME_MS)
        idle = wall_time_ms if idle_timeout_ms is None else idle_timeout_ms
        _bounded_int(idle, maximum=MAX_WALL_TIME_MS)
        if idle > wall_time_ms:
            raise ValueError("idle timeout cannot exceed wall budget")
        if not callable(clock):
            raise TypeError("watchdog clock must be callable")
        self._clock = clock
        self._wall_time_ms = wall_time_ms
        self._idle_timeout_ms = idle
        now = self._read_clock()
        self._started_at = now
        self._last_progress_at = now
        self._last_now = now

    def _read_clock(self) -> float:
        try:
            value = float(self._clock())
        except Exception:
            raise ResourceAccountingError("watchdog_clock_invalid") from None
        if not math.isfinite(value):
            raise ResourceAccountingError("watchdog_clock_invalid")
        if hasattr(self, "_last_now") and value < self._last_now:
            raise ResourceAccountingError("watchdog_clock_invalid")
        self._last_now = value
        return value

    @staticmethod
    def _elapsed(now: float, origin: float) -> float:
        return max(0.0, (now - origin) * 1000.0)

    @classmethod
    def _elapsed_ms(cls, now: float, origin: float) -> int:
        return int(cls._elapsed(now, origin))

    def elapsed_ms(self) -> int:
        now = self._read_clock()
        return self._elapsed_ms(now, self._started_at)

    def check(self) -> int:
        now = self._read_clock()
        elapsed_raw = self._elapsed(now, self._started_at)
        idle_raw = self._elapsed(now, self._last_progress_at)
        if elapsed_raw > self._wall_time_ms:
            raise ResourceAccountingError("deadline_exceeded")
        if idle_raw > self._idle_timeout_ms:
            raise ResourceAccountingError("watchdog_stalled")
        return int(elapsed_raw)

    def progress(self) -> int:
        now = self._read_clock()
        elapsed_raw = self._elapsed(now, self._started_at)
        idle_raw = self._elapsed(now, self._last_progress_at)
        if elapsed_raw > self._wall_time_ms:
            raise ResourceAccountingError("deadline_exceeded")
        if idle_raw > self._idle_timeout_ms:
            raise ResourceAccountingError("watchdog_stalled")
        self._last_progress_at = now
        return int(elapsed_raw)


class ResourceGovernor:
    """Enforce one immutable budget over monotonic cumulative samples."""

    def __init__(
        self,
        budget: ResourceBudget,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not isinstance(budget, ResourceBudget):
            raise TypeError("resource budget is invalid")
        self.budget = budget
        self.watchdog = MonotonicWatchdog(
            wall_time_ms=budget.wall_time_ms,
            idle_timeout_ms=budget.idle_timeout_ms,
            clock=clock,
        )
        self._usage = ResourceUsage()
        self._sample: ResourceSample | None = None
        self._terminal_code: str | None = None

    @property
    def usage(self) -> ResourceUsage:
        return self._usage

    def _fail(self, code: str) -> None:
        self._terminal_code = code
        raise ResourceAccountingError(code)

    def _validate_monotonic(self, sample: ResourceSample) -> None:
        previous = self._sample
        if previous is None:
            return
        for field in (
            "cpu_time_ms",
            "bytes_read",
            "bytes_written",
            "messages",
            "console_bytes",
            "observation_bytes",
        ):
            if getattr(sample, field) < getattr(previous, field):
                self._fail("accounting_invalid")
        if (
            previous.peak_memory_bytes is not None
            and sample.peak_memory_bytes is None
        ):
            self._fail("accounting_invalid")
        if (
            previous.peak_memory_bytes is not None
            and sample.peak_memory_bytes is not None
            and sample.peak_memory_bytes < previous.peak_memory_bytes
        ):
            self._fail("accounting_invalid")

    def _enforce(self, usage: ResourceUsage) -> None:
        if usage.wall_time_ms > self.budget.wall_time_ms:
            self._fail("deadline_exceeded")
        if usage.cpu_time_ms > self.budget.cpu_time_ms:
            self._fail("cpu_exhausted")
        if (
            usage.peak_memory_bytes is not None
            and usage.peak_memory_bytes > self.budget.memory_bytes
        ):
            self._fail("memory_exhausted")
        if usage.bytes_read > self.budget.max_bytes_read:
            self._fail("input_limit")
        if usage.bytes_written > self.budget.max_bytes_written:
            self._fail("output_limit")
        if usage.console_bytes > self.budget.max_console_bytes:
            self._fail("console_limit")
        if usage.observation_bytes > self.budget.max_observation_bytes:
            self._fail("observation_limit")
        if usage.messages > self.budget.max_messages:
            self._fail("message_budget_exhausted")

    def observe(self, sample: ResourceSample, *, progress: bool = False) -> ResourceUsage:
        if self._terminal_code is not None:
            raise ResourceAccountingError(self._terminal_code)
        if not isinstance(sample, ResourceSample):
            self._fail("accounting_invalid")
        elapsed = self.watchdog.progress() if progress else self.watchdog.check()
        self._validate_monotonic(sample)
        usage = ResourceUsage(
            wall_time_ms=elapsed,
            cpu_time_ms=sample.cpu_time_ms,
            peak_memory_bytes=sample.peak_memory_bytes,
            bytes_read=sample.bytes_read,
            bytes_written=sample.bytes_written,
            messages=sample.messages,
            console_bytes=sample.console_bytes,
            observation_bytes=sample.observation_bytes,
        )
        self._enforce(usage)
        self._sample = sample
        self._usage = usage
        return usage

    def finish(self) -> ResourceUsage:
        if self._terminal_code is not None:
            raise ResourceAccountingError(self._terminal_code)
        self.watchdog.check()
        if not self._usage.accounting_complete:
            self._fail("accounting_unavailable")
        return self._usage


__all__ = [
    "MAX_CONSOLE_BYTES",
    "MAX_COUNTER",
    "MAX_CPU_TIME_MS",
    "MAX_MEMORY_BYTES",
    "MAX_MESSAGES",
    "MAX_OBSERVATION_BYTES",
    "MAX_WALL_TIME_MS",
    "MonotonicWatchdog",
    "ResourceAccountingError",
    "ResourceBudget",
    "ResourceGovernor",
    "ResourceSample",
    "ResourceUsage",
]
