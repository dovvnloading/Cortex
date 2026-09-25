"""The README screenshots must show Cortex doing what it can actually do.

The showcase workspace (tools/screenshots/showcase_server.py) stages the code
tasks photographed for the README. They once showed a program that imported
``csv``, called methods and read a file off the disk -- all things the real
validator rejects -- so the headline image of the feature was of something
Cortex cannot run. These tests hold the staged tasks to the real validator
and the real restricted interpreter.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "screenshots"))

import showcase_server  # noqa: E402

from cortex_backend.execution.code_execution import (  # noqa: E402
    run_code_in_worker,
    validate_code_source,
)
from cortex_backend.execution.repository import ExecutionRepository  # noqa: E402
from cortex_backend.testing import DurableFakeCoordinator  # noqa: E402

STAGED_SOURCES = (showcase_server.APPROVAL_SOURCE, showcase_server.COMPLETED_SOURCE)


def test_every_staged_program_passes_the_real_validator() -> None:
    for source in STAGED_SOURCES:
        validate_code_source(source)


def test_the_staged_output_is_what_the_program_really_prints() -> None:
    result = run_code_in_worker(showcase_server.COMPLETED_SOURCE, {})

    assert result.stdout == showcase_server.COMPLETED_STDOUT
    assert result.stderr == ""


def test_the_staged_tasks_ask_for_no_host_access(tmp_path: Path) -> None:
    repository = ExecutionRepository(tmp_path / "execution.sqlite", tmp_path / "artifacts")
    showcase_server.stage_execution_tasks(DurableFakeCoordinator(repository))
    owner = repository.installation_principal_id

    staged = [
        repository.get_job(job_id, owner=owner)
        for job_id in ("code-loan-payment", "code-orders-above-average")
    ]
    for job in staged:
        assert job is not None
        assert job.payload["source"] in STAGED_SOURCES
        assert not any(job.payload["capabilities"].values()), job.job_id


def test_the_chat_shows_the_program_that_is_staged() -> None:
    chat = next(chat for chat in showcase_server.EXTRA_CHATS if chat["id"] == "demo-loan-payment")
    assert showcase_server.APPROVAL_SOURCE in chat["messages"][-1]["content"]
