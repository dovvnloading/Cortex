"""Serve Cortex with a fully staged workspace for product screenshots.

This is the documentation demo server (``demo_server.py``) widened for demo
captures: a filed chat library with groups, saved memories, configured
translation, and staged local-execution tasks so the approval tray can be
photographed in its real state.

    python tools/screenshots/showcase_server.py --port 8801

Nothing here contacts a model and nothing is generated at capture time, so a
re-run produces the same pixels. The UI is the real application; only the
workspace content is fixture data.

Prints the bootstrap token on the first line of stdout so the capture script
can authenticate.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from demo_server import (  # noqa: E402
    CHATS as BASE_CHATS,
    INSTALLED_MODELS,
    _stats,
    _with_message_timestamps,
)

from cortex_backend.api import BackendDependencies, create_app  # noqa: E402
from cortex_backend.core.settings import (  # noqa: E402
    AppearanceSettings,
    CortexSettings,
    ExecutionSettings,
    GenerationSettings,
    MemorySettings,
    ModelSettings,
    TranslationSettings,
)
from cortex_backend.testing import DurableFakeCoordinator  # noqa: E402
from cortex_backend.execution.repository import ExecutionRepository  # noqa: E402
from cortex_backend.repositories.chats import InMemoryChatRepository  # noqa: E402
from cortex_backend.repositories.memories import InMemoryMemoryRepository  # noqa: E402
from cortex_backend.repositories.settings import InMemorySettingsRepository  # noqa: E402
from cortex_backend.services.attachments import ChatAttachmentService  # noqa: E402
from cortex_backend.services.generation import GenerationService  # noqa: E402
from cortex_backend.services.models import ModelService  # noqa: E402
from cortex_backend.testing.fake_ollama import (  # noqa: E402
    FakeGenerationEngine,
    FakeOllamaGateway,
    FakeOllamaState,
)

SYSTEM_INSTRUCTIONS = (
    "Answer directly and state the caveats rather than hedging the whole answer. "
    "Prefer standard-library Python unless a dependency genuinely earns its place. "
    "When you show a command or a query, show the output you expect from it."
)

# --- Extra staged conversations ---------------------------------------------
# These sit alongside the documentation fixtures so the library has enough
# filed material to look like a workspace someone actually uses.

EXTRA_CHATS: list[dict] = [
    {
        "id": "demo-orders-total",
        "title": "Totalling a month of orders on disk",
        "timestamp": "2026-08-09T16:58:00Z",
        "messages": [
            {
                "id": "m-50",
                "role": "user",
                "content": "There is an `exports/orders-2026-08.csv` on this machine. Can you total the `amount` column and tell me the row count?",
            },
            {
                "id": "m-51",
                "role": "assistant",
                "content": """I can, but reading a file off your disk is a capability I have to ask for rather
than assume. I have prepared the task and it is waiting in the tray for your approval -- nothing
runs until you allow it.

Here is exactly what I want to execute:

```python
import csv
from pathlib import Path

path = Path("exports/orders-2026-08.csv")
total = 0.0
rows = 0
with path.open(newline="", encoding="utf-8-sig") as handle:
    for row in csv.DictReader(handle):
        total += float(row["amount"])
        rows += 1

print(f"rows={rows} total={total:,.2f}")
```

What it asks for, and nothing else:

- **Files** -- read `exports/orders-2026-08.csv`. No writes.
- **No process access.** It does not start anything.
- **No network access.** Nothing leaves this machine.

Approve it in the task tray and I will report the totals. Deny it and I will instead give you a
one-liner you can run yourself.""",
                "stats": _stats(prompt_tokens=52, output_tokens=203, tokens_per_second=46.8),
            },
        ],
    },
    {
        "id": "demo-attention-cost",
        "title": "Why attention scales quadratically",
        "timestamp": "2026-08-09T13:10:00Z",
        "messages": [
            {
                "id": "m-60",
                "role": "user",
                "content": "Why is transformer attention described as quadratic, and does FlashAttention actually change that?",
            },
            {
                "id": "m-61",
                "role": "assistant",
                "thoughts": """Two separate claims are getting conflated in the question, and the useful answer
separates them: the arithmetic cost and the memory cost.

The quadratic term comes from the score matrix -- every one of n queries is compared against
every one of n keys, so there are n^2 scores. That is a property of the operation itself.

FlashAttention is a memory/IO optimisation. It tiles the computation and recomputes what it
needs instead of writing the full n x n matrix out to HBM. So peak memory drops from O(n^2) to
O(n), and it runs faster because it moves less data -- but the number of multiply-accumulates
is unchanged. I should be explicit that this is not an asymptotic improvement in FLOPs, since
that is the exact misconception being asked about.

Things that do change the asymptotics are a different class: sliding-window, sparse, and
linear-attention variants, which compute fewer than n^2 scores by construction.""",
                "content": """Two different costs get folded into the word "quadratic", and they behave differently.

**The arithmetic.** Attention compares every query against every key, so for a sequence of
length *n* it forms an *n x n* matrix of scores:

```python
scores  = Q @ K.T        # (n, d) @ (d, n) -> (n, n)
weights = softmax(scores)
output  = weights @ V    # (n, n) @ (n, d) -> (n, d)
```

That is `n^2 * d` multiply-accumulates. Double the context, quadruple the work. It follows from
the definition of the operation, not from the implementation.

**The memory.** A naive implementation also *stores* that `n x n` matrix. At 32k tokens that is
roughly a billion entries per head, per layer -- which is what actually puts a ceiling on
context length in practice.

FlashAttention attacks the second cost, not the first. It tiles the computation and keeps
running softmax statistics so the full matrix is never written out, dropping peak memory from
O(n^2) to O(n). It is also genuinely faster, because attention is memory-bandwidth bound and it
moves far less data. But the multiply-accumulate count is **identical** -- it is an exact
algorithm, not an approximation.

If you want the arithmetic itself to stop being quadratic, you need a different operation:
sliding-window attention (each token sees a fixed span), sparse patterns, or the
linear-attention family. Those change the asymptotics, and they trade away some of the
modelling quality that full attention buys.""",
                "sources": [
                    "Vaswani et al., *Attention Is All You Need* (2017) -- the scaled dot-product definition.",
                    "Dao et al., *FlashAttention* (2022) -- exact attention, IO-aware tiling.",
                    "Beltagy et al., *Longformer* (2020) -- sliding-window and global attention patterns.",
                ],
                "stats": _stats(prompt_tokens=46, output_tokens=341, tokens_per_second=43.9),
            },
        ],
    },
    {
        "id": "demo-pg-index",
        "title": "Postgres skips my composite index",
        "timestamp": "2026-08-09T10:05:00Z",
        "messages": [
            {
                "id": "m-70",
                "role": "user",
                "content": "I have an index on `(tenant_id, created_at)` but Postgres sequential-scans when I filter on `created_at` alone. Why?",
            },
            {
                "id": "m-71",
                "role": "assistant",
                "content": """Because a composite B-tree is sorted by its **leading** column first. The index is
ordered by `tenant_id`, and only within each `tenant_id` by `created_at` -- so rows for a given
date are scattered across the whole index. There is no cheap way in, and a full index scan is
usually worse than reading the table.

Which queries that index can serve:

| Predicate | Usable? | Why |
|---|---|---|
| `tenant_id = $1` | Yes | Leading column constrained |
| `tenant_id = $1 AND created_at > $2` | Yes | Leading column, then a range |
| `created_at > $2` | No | Leading column unconstrained |
| `tenant_id = $1 ORDER BY created_at` | Yes | Index order matches the sort |

Two fixes, depending on what you actually run:

```sql
-- If the date-only query is a real access path, give it its own index.
CREATE INDEX CONCURRENTLY orders_created_at_idx ON orders (created_at);

-- If every query is tenant-scoped anyway, the existing index is right and the
-- date-only query is the thing to fix.
```

One caveat before you add anything: a sequential scan is not automatically a bug. If the planner
estimates the predicate matches a large fraction of the table, a seq scan genuinely is cheaper
than an index scan plus heap fetches. Confirm which situation you are in:

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT ...;
```

Compare the estimated rows against the actual rows. If they are far apart, the fix is `ANALYZE`
or better statistics, not another index.""",
                "stats": _stats(prompt_tokens=48, output_tokens=298, tokens_per_second=45.4),
            },
        ],
    },
    {
        "id": "demo-gpu-backend",
        "title": "Vulkan or CPU for local inference",
        "timestamp": "2026-08-08T15:40:00Z",
        "messages": [
            {
                "id": "m-80",
                "role": "user",
                "content": "The GPU backend setting offers auto, vulkan, and cpu. When would I ever pin it instead of leaving it on auto?",
            },
            {
                "id": "m-81",
                "role": "assistant",
                "content": """Leave it on **auto** unless you have a specific reason -- auto tries Vulkan and falls
back to the CPU build if Vulkan cannot launch on this machine, which is the behaviour you want
almost always.

Pin it when the fallback is hiding something from you:

- **Pin `vulkan`** when you are diagnosing performance. On auto, a driver problem silently
  degrades you to CPU and looks like "the model is just slow". Pinned, it fails loudly and you
  find out the real reason.
- **Pin `cpu`** when the GPU is contended -- something else is already holding the VRAM -- or
  when you are reproducing a result and want the same path every time.

The practical tell: if tokens/sec on a small model is in the low single digits, you are on CPU
whether or not you chose to be. Every response footer in Cortex shows its own tokens/sec, so you
can check that directly rather than guessing.""",
                "stats": _stats(prompt_tokens=44, output_tokens=189, tokens_per_second=47.1),
            },
        ],
    },
    {
        "id": "demo-release-notes",
        "title": "Changelog into release notes",
        "timestamp": "2026-08-08T08:20:00Z",
        "messages": [
            {
                "id": "m-90",
                "role": "user",
                "content": "Rewrite these three commit subjects as release notes for non-engineers: `fix: debounce library search`, `feat: per-chat generation params`, `perf: virtualize message list`.",
            },
            {
                "id": "m-91",
                "role": "assistant",
                "content": """**Search feels immediate.** Typing in the chat library no longer stutters while it
filters -- results settle as you type instead of fighting you for the keyboard.

**Per-conversation model settings.** Temperature, top-p, and context length can now be changed
for a single conversation without touching your defaults. Useful when one thread wants precise
answers and another wants range.

**Long conversations stay fast.** Transcripts now render only what is on screen, so a thread
with a thousand messages scrolls as smoothly as one with ten.

Two notes on the rewrite: I dropped the `fix:`/`feat:`/`perf:` prefixes, which mean something to
your commit history and nothing to a reader, and I led each item with the outcome rather than the
mechanism. If these are going in an in-app "what's new" panel, cut the second sentence of each --
the first sentence carries it.""",
                "stats": _stats(prompt_tokens=61, output_tokens=187, tokens_per_second=46.5),
            },
        ],
    },
]

CHATS = BASE_CHATS + EXTRA_CHATS

MEMORIES = [
    "Prefers Python for data work and dislikes unnecessary dependencies.",
    "Works on a small internal tools team of four engineers.",
    "Wants direct answers with the caveats stated, not hedged ones.",
    "Runs Postgres 16 in production and SQLite for local development.",
    "Ships on Windows first; the team's build machines are all Windows 11.",
]

# The filed library: groups in sidebar order, then everything else ungrouped.
GROUPS = [
    (
        "grp-engineering",
        "Engineering",
        [
            "demo-orders-total",
            "demo-streaming-csv",
            "demo-sqlite-locked",
            "demo-pg-index",
            "demo-apache-regex",
        ],
    ),
    ("grp-research", "Research", ["demo-attention-cost", "demo-embeddings"]),
    ("grp-decisions", "Decisions", ["demo-rust-go", "demo-gpu-backend"]),
]

# --- Staged local execution --------------------------------------------------

APPROVAL_SOURCE = '''import csv
from pathlib import Path

path = Path("exports/orders-2026-08.csv")
total = 0.0
rows = 0
with path.open(newline="", encoding="utf-8-sig") as handle:
    for row in csv.DictReader(handle):
        total += float(row["amount"])
        rows += 1

print(f"rows={rows} total={total:,.2f}")
'''

COMPLETED_SOURCE = '''from pathlib import Path

for path in sorted(Path("assets/icons").glob("*.png")):
    print(f"{path.name:28} {path.stat().st_size:>7,} bytes")
'''


def _digest(source: str) -> str:
    return sha256(source.encode("utf-8")).hexdigest()


def _code_payload(source: str, intent: str, **capabilities: bool) -> dict:
    return {
        "schema_version": "code.execution.v1",
        "language": "python",
        "source": source,
        "intent_summary": intent,
        "source_digest": _digest(source),
        "capabilities": {
            "filesystem": capabilities.get("filesystem", False),
            "process": capabilities.get("process", False),
            "network": capabilities.get("network", False),
        },
    }


def stage_execution_tasks(coordinator: DurableFakeCoordinator) -> None:
    """Put one task in front of the approval gate and one behind it.

    Staged on demand rather than at startup: the tray is a fixed overlay, so
    every other capture would otherwise have it sitting on top.
    """
    repository = coordinator.repository
    owner = repository.installation_principal_id
    if repository.get_job("code-orders-total", owner=owner) is not None:
        return  # already staged

    # Finished earlier: approved, ran, returned output.
    repository.create_job(
        job_id="code-icon-audit",
        owner=owner,
        request_id="request-code-icon-audit",
        profile="code.exec.v1",
        payload=_code_payload(
            COMPLETED_SOURCE,
            "List every icon asset with its file size.",
            filesystem=True,
        ),
    )
    repository.request_approval(
        "code-icon-audit",
        owner=owner,
        scope_digest=_digest(COMPLETED_SOURCE),
        reason="Read the icon assets directory.",
        ttl_seconds=300.0,
    )
    repository.decide_approval("code-icon-audit", owner=owner, decision="approved")
    repository.transition(
        "code-icon-audit",
        status="succeeded",
        event="execution.completed",
        phase="completed",
        data={"message": "Local code execution finished."},
        result={
            "stdout": (
                "cortex-16.png                  1,204 bytes\n"
                "cortex-32.png                  2,918 bytes\n"
                "cortex-48.png                  5,471 bytes\n"
                "cortex-256.png                38,602 bytes"
            ),
            "stderr": "",
            "value": 4,
            "duration_ms": 412,
        },
    )

    # Waiting on the user right now.
    repository.create_job(
        job_id="code-orders-total",
        owner=owner,
        request_id="request-code-orders-total",
        profile="code.exec.v1",
        payload=_code_payload(
            APPROVAL_SOURCE,
            "Total the amount column in exports/orders-2026-08.csv.",
            filesystem=True,
        ),
    )
    repository.request_approval(
        "code-orders-total",
        owner=owner,
        scope_digest=_digest(APPROVAL_SOURCE),
        reason="Read exports/orders-2026-08.csv and total one column.",
        ttl_seconds=300.0,
    )


def build_showcase_app(*, frontend_dist: Path, execution_dir: Path):
    """Create the API with a fixed, fully staged workspace."""
    chats = InMemoryChatRepository(_with_message_timestamps(CHATS))
    for group_id, name, members in GROUPS:
        chats.create_group(group_id, name)
        for thread_id in members:
            chats.set_chat_group(thread_id, group_id)

    memories = InMemoryMemoryRepository(list(MEMORIES))

    settings = InMemorySettingsRepository(
        CortexSettings(
            appearance=AppearanceSettings(theme="dark"),
            models=ModelSettings(chat="qwen3:8b", title=None),
            generation=GenerationSettings(
                temperature=0.7,
                top_p=0.9,
                top_k=40,
                repeat_penalty=1.1,
                num_ctx=8192,
                system_instructions=SYSTEM_INSTRUCTIONS,
            ),
            execution=ExecutionSettings(automatic_compute=True, code_execution_enabled=True),
            memory=MemorySettings(enabled=True),
            translation=TranslationSettings(enabled=True, target_language="Japanese"),
        )
    )

    state = FakeOllamaState(installed_models=set(INSTALLED_MODELS))
    dependencies = BackendDependencies(
        settings=settings,
        chats=chats,
        memories=memories,
        models=ModelService(FakeOllamaGateway(state)),
        generation=GenerationService(
            history_loader=lambda thread_id: (chats.get_chat(thread_id) or {}).get("messages", []),
            memory_loader=memories.get_memos,
            engine_factory=lambda snapshot: FakeGenerationEngine(state),
        ),
        attachments=ChatAttachmentService(),
    )

    coordinator = DurableFakeCoordinator(
        ExecutionRepository(execution_dir / "execution.sqlite", execution_dir / "artifacts")
    )

    app = create_app(
        dependencies,
        preview=True,
        serve_frontend=True,
        frontend_dist=frontend_dist,
        readiness_check=lambda: True,
        execution_coordinator=coordinator,
    )

    # The SPA fallback is GET-only, so a POST route registered afterwards still
    # wins the match. The capture driver calls this immediately before the
    # task-tray shots.
    @app.post("/showcase/execution/stage", include_in_schema=False)
    def stage() -> dict:
        stage_execution_tasks(coordinator)
        return {"staged": True}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument(
        "--frontend-dist",
        type=Path,
        default=ROOT / "frontend" / "dist",
        help="Built frontend bundle to serve.",
    )
    args = parser.parse_args()

    if not (args.frontend_dist / "index.html").is_file():
        raise SystemExit(
            f"No built frontend at {args.frontend_dist}. Run: npm run build --prefix frontend"
        )

    execution_dir = Path(tempfile.mkdtemp(prefix="cortex-showcase-"))
    try:
        app = build_showcase_app(
            frontend_dist=args.frontend_dist, execution_dir=execution_dir
        )
        # The bootstrap token is the first line the server prints. There is
        # only ever one valid at a time -- issuing another rotates it.
        print(app.state.session_manager.bootstrap_token, flush=True)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        shutil.rmtree(execution_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
