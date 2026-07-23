"""Fixed recipe worker package entrypoint.

The executable has one supported launch shape: the native launcher supplies a
protected named-pipe endpoint plus the exact broker principal and job identity.
There is deliberately no stdio, shell, path, or direct-process fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sys

from cortex_backend.execution.native_broker import (
    NativeBrokerClient,
    NativeBrokerClientConfig,
)
from cortex_backend.execution.worker_runtime import (
    RecipeWorkerBrokerRuntime,
)


EXIT_SAFE_REFUSAL = 78
_PIPE = re.compile(r"^\\\\\.\\pipe\\cortex-[A-Za-z0-9._-]{1,200}$")
_PRINCIPAL = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class WorkerArguments:
    pipe_name: str
    broker_process_id: int
    installation_principal_id: str
    job_id: str


def _parse_args(argv: list[str]) -> WorkerArguments:
    expected = (
        "--native-broker",
        "--broker-pipe",
        "--broker-pid",
        "--broker-principal",
        "--job-id",
    )
    if len(argv) != 9 or argv[0] != expected[0]:
        raise ValueError("native broker arguments are required")
    values = dict(zip(argv[1::2], argv[2::2], strict=True))
    if tuple(values) != expected[1:] or any(not isinstance(value, str) for value in values.values()):
        raise ValueError("native broker arguments are invalid")
    pipe_name = values["--broker-pipe"]
    if _PIPE.fullmatch(pipe_name) is None:
        raise ValueError("native broker pipe is invalid")
    try:
        broker_process_id = int(values["--broker-pid"], 10)
    except (TypeError, ValueError):
        raise ValueError("native broker process ID is invalid") from None
    if not 1 <= broker_process_id <= 0xFFFFFFFF:
        raise ValueError("native broker process ID is invalid")
    principal = values["--broker-principal"]
    if _PRINCIPAL.fullmatch(principal) is None:
        raise ValueError("native broker principal is invalid")
    job_id = values["--job-id"]
    if _SAFE_ID.fullmatch(job_id) is None:
        raise ValueError("native broker job ID is invalid")
    return WorkerArguments(pipe_name, broker_process_id, principal, job_id)


def _run(arguments: WorkerArguments) -> int:
    client = NativeBrokerClient(
        NativeBrokerClientConfig(
            pipe_name=arguments.pipe_name,
            expected_server_process_id=arguments.broker_process_id,
        )
    )
    connection = client.connect(
        expected_principal_id=arguments.installation_principal_id,
        owner_for_job=lambda job_id: (
            arguments.installation_principal_id if job_id == arguments.job_id else None
        ),
    )
    try:
        report = RecipeWorkerBrokerRuntime(
            connection,
            expected_principal_id=arguments.installation_principal_id,
            job_id=arguments.job_id,
        ).run()
    except Exception:
        connection.close()
        raise
    return 0 if report.terminal_state in {"complete", "cancelled"} else EXIT_SAFE_REFUSAL


def main(argv: list[str] | None = None) -> int:
    """Run only when the native broker launch contract is complete."""

    try:
        arguments = _parse_args(list(sys.argv[1:] if argv is None else argv))
        return _run(arguments)
    except Exception:
        # Keep direct launches and all runtime failures indistinguishable and
        # avoid printing broker, token, path, or decoder details from a package.
        return EXIT_SAFE_REFUSAL


if __name__ == "__main__":  # pragma: no cover - exercised by the packaged exe.
    raise SystemExit(main(sys.argv[1:]))
