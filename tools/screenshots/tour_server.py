"""Serve the staged showcase workspace for the README tour recordings.

This is ``showcase_server.py`` with three additions the animated tour needs:

* a generation engine that streams one scripted answer a few tokens at a time,
  each step released by the recorder, so the GIF shows the answer arriving
  smoothly instead of whenever the screenshot loop happened to look;
* a real Agent mode run against a small sample project. Only the model is
  scripted. The work copy, the edit approval, the staged diff and the accept
  into the project are Cortex's own Agent code; and
* an approved code task finished by Cortex's real restricted interpreter, so
  the output in the tray is what that program prints.

    python tools/screenshots/tour_server.py --port 8802 --project-parent C:\\cortex-tour

Like the showcase, it prints the bootstrap token on its first line. The second
line is the sample project's folder. Everything it writes lives in temporary
folders that it removes on exit; nothing touches the user's Cortex data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from threading import Semaphore
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

import showcase_server  # noqa: E402
from cortex_backend.agent.protocol import RuntimeCapabilities  # noqa: E402
from cortex_backend.core.generation import GenerationStats, MemoryCommand  # noqa: E402
from cortex_backend.execution.code_execution import run_code_in_worker  # noqa: E402
from cortex_backend.services.workspace_checkpoint import WorkspaceCheckpointStore  # noqa: E402
from cortex_backend.testing.fake_ollama import (  # noqa: E402
    FakeGenerationEngine,
    FakeOllamaState,
)

# --- The streamed chat answer --------------------------------------------------

QUESTION = (
    "Some Excel CSV exports break my importer: row[\"id\"] raises KeyError even "
    "though the header says id. Why?"
)

THOUGHTS = (
    "Only the first column fails, and the header text looks right. That pattern is "
    "an invisible prefix on the first header -- in an Excel export, the UTF-8 byte "
    "order mark."
)

ANSWER = r"""Excel's **CSV UTF-8** export starts the file with a byte order mark, `\ufeff`. Read as
plain `utf-8`, the mark stays in the text, so your first header is really `"\ufeffid"` --
which is why only `row["id"]` fails.

Open the file with `utf-8-sig`. It drops the mark when it is there and reads ordinary UTF-8
unchanged:

```python
def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as source:
        yield from csv.DictReader(source)
```

Excel's older **CSV (Comma delimited)** export is not UTF-8 at all: it uses the Windows code
page, usually `cp1252`.
"""

TITLE = "Excel CSV exports and the byte order mark"

# Word-sized pieces, whitespace kept with the word before it, the way a model's
# tokens arrive. Code is split the same way so the block fills in line by line.
_TOKEN = re.compile(r"\S+\s*|\s+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


class Gate:
    """Lets the recorder release streamed tokens and Agent steps one at a time."""

    def __init__(self) -> None:
        self._permits = Semaphore(0)

    def release(self, count: int) -> None:
        for _ in range(max(0, count)):
            self._permits.release()

    def wait(self) -> None:
        # Generous: a recorder that stops releasing should not hang the
        # server forever, and a timed-out step simply streams on.
        self._permits.acquire(timeout=60)


class SteppedEngine(FakeGenerationEngine):
    """The showcase engine, except the tour question streams token by token."""

    def __init__(self, state: FakeOllamaState, gate: Gate) -> None:
        super().__init__(state)
        self._gate = gate

    def generate(self, *, query: str, on_delta=None, **kwargs: Any):
        if query.strip() != QUESTION or on_delta is None:
            return super().generate(query=query, on_delta=on_delta, **kwargs)
        for piece in _tokens(THOUGHTS):
            self._gate.wait()
            on_delta("thinking", piece)
        for piece in _tokens(ANSWER):
            self._gate.wait()
            on_delta("content", piece)
        output_tokens = len(_tokens(ANSWER)) + len(_tokens(THOUGHTS))
        stats = GenerationStats(
            prompt_eval_count=64,
            eval_count=output_tokens,
            prompt_eval_duration_ms=170.0,
            eval_duration_ms=round(output_tokens / 46.2 * 1000, 1),
            total_duration_ms=round(output_tokens / 46.2 * 1000 + 170.0, 1),
            tokens_per_second=46.2,
        )
        return ANSWER, THOUGHTS, MemoryCommand(), stats

    def generate_chat_title(self, chat_history: str, **kwargs: Any) -> str | None:
        if QUESTION in chat_history:
            return TITLE
        return super().generate_chat_title(chat_history, **kwargs)


# --- The Agent mode run ----------------------------------------------------------

AGENT_GOAL = (
    "Excel exports make customer_ids() fail with KeyError: 'id'. Fix the importer."
)

PROJECT_FILES = {
    "importer.py": '''"""Load customer rows from CSV exports."""

import csv
from pathlib import Path


def read_rows(path: Path):
    with open(path, encoding="utf-8", newline="") as source:
        yield from csv.DictReader(source)


def customer_ids(path: Path) -> list[str]:
    return [row["id"] for row in read_rows(path)]
''',
    "report.py": '''"""Print a one-line summary of an export."""

import sys
from pathlib import Path

from importer import customer_ids

if __name__ == "__main__":
    ids = customer_ids(Path(sys.argv[1]))
    print(f"{len(ids)} customers")
''',
    "README.md": "# csv-importer\n\nReads customer exports from the CRM and from Excel.\n",
}


def _plan(*statuses: str) -> str:
    items = (
        "Find where the CSV export is opened",
        "Read Excel's byte order mark correctly",
        "Explain the change",
    )
    return _call("update_plan", {
        "items": [{"text": text, "status": state} for text, state in zip(items, statuses, strict=True)],
    })


def _call(name: str, arguments: dict[str, object], text: str = "") -> str:
    return json.dumps({
        "text": text, "finish": "tool_calls",
        "tool_calls": [{"name": name, "arguments": arguments}],
    })


AGENT_REPLIES = [
    _plan("in_progress", "pending", "pending"),
    _call("grep", {"pattern": "open\\("}),
    _call("read_file", {"path": "importer.py"}),
    _plan("done", "in_progress", "pending"),
    _call("edit_file", {
        "path": "importer.py",
        "old": 'encoding="utf-8",',
        "new": 'encoding="utf-8-sig",',
    }),
    _plan("done", "done", "done"),
    json.dumps({
        "text": (
            "importer.py now opens exports with `utf-8-sig`. Excel's CSV UTF-8 export "
            "starts with a byte order mark, which plain `utf-8` kept as part of the "
            "first header, so the column was named `\\ufeffid` instead of `id`. "
            "Files without the mark read exactly as before."
        ),
        "finish": "stop",
        "tool_calls": [],
    }),
]


class ScriptedAgentModel:
    """Stands in for the model only; Agent mode does everything else for real."""

    tool_protocol_capabilities = RuntimeCapabilities(json_schema=True)

    def __init__(self, replies: list[str], gate: Gate) -> None:
        self._replies = list(replies)
        self._gate = gate

    def chat(self, **_kwargs: Any) -> dict[str, object]:
        self._gate.wait()
        if not self._replies:
            return {"message": {"content": json.dumps(
                {"text": "Done.", "finish": "stop", "tool_calls": []})}}
        return {"message": {"content": self._replies.pop(0)}}


def write_sample_project(parent: Path) -> Path:
    """Create the sample project in a new ``csv-importer`` folder under ``parent``.

    ``parent`` is the caller's choice and may already hold a real project of
    that name, so an existing folder is refused (``FileExistsError``), never
    overwritten or removed.
    """
    root = parent / "csv-importer"
    parent.mkdir(parents=True, exist_ok=True)
    root.mkdir()
    for name, text in PROJECT_FILES.items():
        (root / name).write_text(text, encoding="utf-8", newline="\n")
    return root


# --- Finishing an approved code task ----------------------------------------------

def finish_approved_task(coordinator, job_id: str) -> dict[str, object]:
    """Run an approved staged task through the real restricted interpreter."""
    repository = coordinator.repository
    owner = repository.installation_principal_id
    job = repository.get_job(job_id, owner=owner)
    if job is None:
        return {"finished": False, "reason": "missing"}
    # Only what the user allowed on screen runs, as in the app itself.
    if repository.get_approval_state(job_id, owner=owner) != "approved":
        return {"finished": False, "reason": "not_approved"}
    result = run_code_in_worker(job.payload["source"], {})
    repository.transition(
        job_id,
        status="succeeded",
        event="execution.completed",
        phase="completed",
        data={"message": "Local code execution finished."},
        result={
            "stdout": result.stdout,
            "stderr": result.stderr,
            "value": None,
            "duration_ms": 388,
        },
    )
    return {"finished": True, "stdout": result.stdout}


def build_tour_app(*, frontend_dist: Path, work_dir: Path):
    gate = Gate()
    state = FakeOllamaState(installed_models=set(showcase_server.INSTALLED_MODELS))
    agent_dir = work_dir / "agent-data"
    agent_dir.mkdir(parents=True, exist_ok=True)
    app = showcase_server.build_showcase_app(
        frontend_dist=frontend_dist,
        execution_dir=work_dir / "execution",
        engine_factory=lambda snapshot: SteppedEngine(state, gate),
        translation_enabled=False,
        agent_enabled=True,
        agent_data_dir=agent_dir,
        agent_chat_client=ScriptedAgentModel(AGENT_REPLIES, gate),
        workspace_checkpoint_store=WorkspaceCheckpointStore(
            agent_dir / "workspace_checkpoints.sqlite"
        ),
    )
    coordinator = app.state.showcase_coordinator

    @app.post("/showcase/step", include_in_schema=False)
    def step(count: int = 1) -> dict:
        gate.release(count)
        return {"released": count}

    @app.post("/showcase/execution/finish/{job_id}", include_in_schema=False)
    def finish(job_id: str) -> dict:
        return finish_approved_task(coordinator, job_id)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8802)
    parser.add_argument("--frontend-dist", type=Path, default=ROOT / "frontend" / "dist")
    parser.add_argument(
        "--project-parent",
        type=Path,
        default=None,
        help="Folder to create the sample csv-importer project in (default: a temp folder).",
    )
    args = parser.parse_args()
    if not (args.frontend_dist / "index.html").is_file():
        raise SystemExit(
            f"No built frontend at {args.frontend_dist}. Run: npm run build --prefix frontend"
        )

    work_dir = Path(tempfile.mkdtemp(prefix="cortex-tour-"))
    project_parent = args.project_parent or work_dir
    created_parent = not project_parent.exists()
    project: Path | None = None
    try:
        try:
            project = write_sample_project(project_parent)
        except FileExistsError:
            raise SystemExit(
                f"{project_parent / 'csv-importer'} already exists. Remove it or pass "
                "another --project-parent; the tour never overwrites an existing folder."
            ) from None
        app = build_tour_app(frontend_dist=args.frontend_dist, work_dir=work_dir)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
        )

        # The recorder ends the server this way so the cleanup below runs; a
        # killed process on Windows never reaches a ``finally``.
        @app.post("/showcase/shutdown", include_in_schema=False)
        def shutdown() -> dict:
            server.should_exit = True
            return {"stopping": True}

        print(app.state.session_manager.bootstrap_token, flush=True)
        print(project, flush=True)
        server.run()
    finally:
        # Only what this run created: the sample folder, and the parent if it
        # did not exist before.
        if project is not None:
            shutil.rmtree(project, ignore_errors=True)
        if created_parent:
            shutil.rmtree(project_parent, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
