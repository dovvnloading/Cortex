"""The execution SSE poll must never report a terminal job without its event.

`_poll_execution_stream` returns an event batch and a job row read from two
separate SQLite connections. The stream consumer yields the batch, then stops
as soon as the job row shows a terminal status -- so if the terminal
transition commits *between* the two reads, the batch is the one from before
it and the terminal event is never delivered. The client sees the connection
close mid-run with no completion.

`ExecutionRepository.transition()` writes the status update and its event in
one BEGIN IMMEDIATE transaction, so the two are visible together or not at
all. Reading the job first therefore makes the pair safe: a terminal job read
guarantees the event read that follows it can see the event.
"""

from __future__ import annotations

from pathlib import Path

from cortex_backend.api.routes import _poll_execution_stream
from cortex_backend.execution.models import TerminalExecutionStatus
from cortex_backend.execution.repository import ExecutionRepository


def _job(tmp_path: Path) -> tuple[ExecutionRepository, str, str]:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    owner = repository.installation_principal_id
    job, _ = repository.create_job(
        job_id="job-1",
        owner=owner,
        request_id="request-1",
        profile="scratch.auto.v1",
        payload={},
    )
    repository.transition(job.job_id, status="running", event="started")
    return repository, job.job_id, owner


class _TransitionsMidPoll:
    """A repository that commits the terminal transition between the reads.

    Whichever read happens first, this makes the job terminal immediately
    afterwards -- the exact interleaving the race needs. It is deterministic,
    so the test does not depend on thread timing.
    """

    def __init__(self, inner: ExecutionRepository, job_id: str) -> None:
        self._inner = inner
        self._job_id = job_id
        self._fired = False

    def _finish_once(self) -> None:
        if not self._fired:
            self._fired = True
            self._inner.transition(
                self._job_id, status="succeeded", event="completed", result={"ok": True}
            )

    def events(self, job_id: str, *, after_sequence: int):
        result = self._inner.events(job_id, after_sequence=after_sequence)
        self._finish_once()
        return result

    def get_job(self, job_id: str, *, owner: str):
        result = self._inner.get_job(job_id, owner=owner)
        self._finish_once()
        return result


def test_a_terminal_job_is_never_returned_without_its_terminal_event(tmp_path: Path) -> None:
    repository, job_id, owner = _job(tmp_path)
    racing = _TransitionsMidPoll(repository, job_id)

    events, current = _poll_execution_stream(racing, job_id, owner, 0)

    assert current is not None
    if current.status in TerminalExecutionStatus:
        # The consumer returns immediately after this batch, so the terminal
        # event has to be inside it or the client never receives one.
        assert events, "terminal job reported with an empty event batch"
        assert events[-1].status in TerminalExecutionStatus, (
            "terminal job reported without its terminal event; the stream "
            "would close on a run the client still believes is in flight"
        )


def test_the_poll_reads_the_job_before_the_events(tmp_path: Path) -> None:
    """Pin the ordering itself, so a future edit cannot quietly reintroduce it."""
    repository, job_id, owner = _job(tmp_path)
    order: list[str] = []

    class _Recording:
        def events(self, job_id: str, *, after_sequence: int):
            order.append("events")
            return repository.events(job_id, after_sequence=after_sequence)

        def get_job(self, job_id: str, *, owner: str):
            order.append("get_job")
            return repository.get_job(job_id, owner=owner)

    _poll_execution_stream(_Recording(), job_id, owner, 0)

    assert order == ["get_job", "events"], (
        "the job must be read first: transition() commits the status and its "
        "event together, so a job read that is already terminal guarantees "
        "the following event read can see the event"
    )
