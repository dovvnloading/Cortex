"""Bounded in-memory event replay for API jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json

from cortex_backend.api.jobs import JobRegistry


class _BrokenMapping(Mapping[str, object]):
    """Mapping-like input that fails while being copied into an event."""

    def __getitem__(self, key: str) -> object:
        raise AssertionError(key)

    def __iter__(self):
        raise UnicodeError("malformed event mapping")

    def __len__(self) -> int:
        return 1


def _data_bytes(events) -> int:
    return sum(
        len(
            json.dumps(
                dict(event.data),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        for event in events
    )


def test_job_events_obey_count_and_byte_caps() -> None:
    async def exercise() -> None:
        registry = JobRegistry(
            poll_seconds=0.001,
            max_event_count=3,
            max_event_bytes=100,
        )

        def runner(sink, _cancel_event):
            for index in range(10):
                sink.publish_event(
                    "progress",
                    phase="streaming",
                    data={"message": f"chunk-{index}", "content": "x" * 65},
                )
            return {"answer": "done"}

        try:
            job = await registry.start(
                kind="generation",
                owner="owner",
                thread_id="thread",
                runner=runner,
            )
            while registry.status(job.job_id, owner="owner").status != "succeeded":
                await asyncio.sleep(0.001)
            events = [
                event
                async for event in registry.events(job.job_id, owner="owner")
            ]
            assert len(events) <= 3
            assert _data_bytes(events) <= 100
            assert [event.sequence for event in events] == sorted(
                event.sequence for event in events
            )
            assert events[-1].status == "succeeded"
            assert events[-1].data == {"answer": "done"}
        finally:
            await registry.shutdown()

    asyncio.run(exercise())


def test_oversized_event_is_compacted_and_terminal_result_is_retained() -> None:
    async def exercise() -> None:
        registry = JobRegistry(
            poll_seconds=0.001,
            max_event_count=2,
            max_event_bytes=64,
        )

        def runner(sink, _cancel_event):
            sink.publish_progress("streaming", "x" * 10_000)
            return {"answer": "done"}

        try:
            job = await registry.start(
                kind="generation",
                owner="owner",
                thread_id="thread",
                runner=runner,
            )
            while registry.status(job.job_id, owner="owner").status != "succeeded":
                await asyncio.sleep(0.001)
            events = [
                event
                async for event in registry.events(job.job_id, owner="owner")
            ]
            assert _data_bytes(events) <= 64
            assert any(event.kind == "completed" for event in events)
            assert events[-1].data == {"answer": "done"}
            snapshot = registry.status(job.job_id, owner="owner")
            assert snapshot.status == "succeeded"
            assert snapshot.result == {"answer": "done"}
        finally:
            await registry.shutdown()

    asyncio.run(exercise())


def test_malformed_event_data_falls_back_without_failing_the_job() -> None:
    async def exercise() -> None:
        registry = JobRegistry(poll_seconds=0.001)

        def runner(sink, _cancel_event):
            sink.publish_event("progress", phase="streaming", data=_BrokenMapping())
            return {"answer": "done"}

        try:
            job = await registry.start(
                kind="generation",
                owner="owner",
                thread_id="thread",
                runner=runner,
            )
            while registry.status(job.job_id, owner="owner").status != "succeeded":
                await asyncio.sleep(0.001)
            events = [
                event
                async for event in registry.events(job.job_id, owner="owner")
            ]
            assert any(
                event.kind == "progress" and event.data == {} for event in events
            )
            assert events[-1].data == {"answer": "done"}
        finally:
            await registry.shutdown()

    asyncio.run(exercise())


def test_replay_cursor_starts_at_oldest_retained_event_after_eviction() -> None:
    async def exercise() -> None:
        registry = JobRegistry(
            poll_seconds=0.001,
            max_event_count=2,
            max_event_bytes=1_024,
        )

        def runner(sink, _cancel_event):
            for index in range(4):
                sink.publish_progress("streaming", f"chunk-{index}")
            return {"answer": "done"}

        try:
            job = await registry.start(
                kind="generation",
                owner="owner",
                thread_id="thread",
                runner=runner,
            )
            while registry.status(job.job_id, owner="owner").status != "succeeded":
                await asyncio.sleep(0.001)
            retained = [
                event
                async for event in registry.events(job.job_id, owner="owner")
            ]
            assert len(retained) == 2
            assert retained[-1].kind == "completed"
            assert retained[0].sequence < retained[-1].sequence

            replay = [
                event
                async for event in registry.events(
                    job.job_id,
                    owner="owner",
                    after_sequence=retained[0].sequence - 1,
                )
            ]
            assert [event.sequence for event in replay] == [
                event.sequence for event in retained
            ]
            terminal_only = [
                event
                async for event in registry.events(
                    job.job_id,
                    owner="owner",
                    after_sequence=retained[0].sequence,
                )
            ]
            assert [event.sequence for event in terminal_only] == [
                retained[-1].sequence
            ]
        finally:
            await registry.shutdown()

    asyncio.run(exercise())
