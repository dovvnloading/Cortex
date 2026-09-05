"""A reader that never disconnected must not lose events to retention.

The retention limits exist to bound memory for a client that drops and
reconnects -- the events() docstring is explicit that a cursor is "a monotonic
lower bound, not a durable replay promise". That contract is about reconnects.
It was also silently applying to readers that were still attached: a long
model response publishes more events than the buffer holds, and the deltas the
reader had not yet taken were evicted out from under it.
"""

from __future__ import annotations

import asyncio

from cortex_backend.api.jobs import JobRegistry


async def _publish_faster_than_the_reader(
    registry: JobRegistry, count: int
) -> tuple[list[int], int]:
    owner = "owner-1"
    job_id = registry.reserve(
        kind="generation", owner=owner, thread_id="thread-1"
    ).snapshot.job_id
    record = registry._records[job_id]
    seen: list[int] = []

    async def reader() -> None:
        async for event in registry.events(job_id, owner=owner):
            seen.append(event.sequence)

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.05)  # let the reader attach and park in its poll

    def producer() -> None:
        # The real generation runner publishes from a worker thread via
        # asyncio.to_thread, in a tight loop with no awaits.
        for index in range(count):
            with registry._lock:
                registry._append_event(
                    record,
                    kind="progress",
                    status="running",
                    phase="content",
                    data={"delta": f"chunk {index}"},
                )
        with registry._lock:
            registry._append_event(record, kind="completed", status="succeeded")

    await asyncio.to_thread(producer)
    await asyncio.wait_for(task, timeout=15)
    return seen, count + 1


def test_a_long_response_reaches_an_attached_reader_intact() -> None:
    """~2000 deltas is a normal long answer; the buffer holds 256.

    Before this was fixed the reader saw 257 of 2001 and lost 1745 -- a
    contiguous hole in the middle of the answer, on a connection that never
    dropped and never reconnected.
    """
    async def exercise() -> tuple[list[int], int]:
        return await _publish_faster_than_the_reader(JobRegistry(max_event_count=256), 2000)

    seen, published = asyncio.run(exercise())

    missing = sorted(set(range(1, published + 1)) - set(seen))
    assert missing == [], f"an attached reader lost {len(missing)} of {published} events"


def test_a_reader_that_stops_reading_cannot_pin_memory_forever() -> None:
    """The protection has a ceiling, or a stalled client becomes a leak.

    Past the headroom the documented lower-bound contract applies again, so
    the buffer stays bounded even with a reader attached and not draining.
    """
    from cortex_backend.api.jobs import LIVE_READER_EVENT_HEADROOM

    registry = JobRegistry(max_event_count=8)
    owner = "owner-1"
    job_id = registry.reserve(
        kind="generation", owner=owner, thread_id="thread-1"
    ).snapshot.job_id
    record = registry._records[job_id]
    # An attached reader that has read nothing at all.
    record.live_cursors[1] = 0

    for index in range(500):
        with registry._lock:
            registry._append_event(
                record, kind="progress", status="running", data={"delta": str(index)}
            )

    assert len(record.events) <= 8 * LIVE_READER_EVENT_HEADROOM
