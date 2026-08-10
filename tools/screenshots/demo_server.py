"""Serve Cortex with a fixed, staged workspace for documentation screenshots.

Everything here is deterministic on purpose. No model is contacted and nothing
is generated at capture time, so re-running the capture produces the same
pixels: same chats, same wording, same token counts, same ordering. The UI in
the resulting images is the real application; only the conversation content is
staged fixture data.

    python tools/screenshots/demo_server.py --port 8799

Prints the bootstrap token on the first line of stdout so the capture script
can authenticate.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from cortex_backend.api import BackendDependencies, create_app  # noqa: E402
from cortex_backend.core.settings import (  # noqa: E402
    AppearanceSettings,
    CortexSettings,
    GenerationSettings,
    ModelSettings,
)
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

# A fixed local inventory so the model picker and Models panel read the same
# way in every capture, regardless of what is installed on the machine.
INSTALLED_MODELS = ["qwen3:8b", "granite4.1:8b", "nemotron-3-nano:4b", "translategemma:4b"]


def _stats(prompt_tokens: int, output_tokens: int, tokens_per_second: float) -> dict:
    eval_ms = round(output_tokens / tokens_per_second * 1000, 1)
    return {
        "prompt_eval_count": prompt_tokens,
        "eval_count": output_tokens,
        "prompt_eval_duration_ms": 180.0,
        "eval_duration_ms": eval_ms,
        "total_duration_ms": round(eval_ms + 180.0, 1),
        "tokens_per_second": tokens_per_second,
    }


CHATS: list[dict] = [
    {
        "id": "demo-streaming-csv",
        "title": "Reading a 4 GB CSV without exhausting memory",
        "timestamp": "2026-08-09T16:42:00Z",
        "messages": [
            {
                "id": "m-1",
                "role": "user",
                "content": "I need to total one column of a 4 GB CSV in Python, but loading it keeps exhausting memory. What's the right approach?",
            },
            {
                "id": "m-2",
                "role": "assistant",
                "content": """Read it as a **stream** rather than materialising the whole file. `csv.DictReader` already
yields one row at a time, so the only thing holding memory is the row you are currently looking at.

```python
import csv

def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)

total = 0.0
for row in read_rows("orders.csv"):
    total += float(row["amount"])

print(f"{total:,.2f}")
```

A few things worth knowing:

- **Keep the generator lazy.** Wrapping it in `list(...)` undoes the whole benefit.
- **`newline=""` is not optional.** Without it, the `csv` module mishandles quoted fields
  that contain line breaks.
- **Only parse what you need.** `float(row["amount"])` per row is cheap; building a `dict`
  of every column for later use is not.

If you would rather stay in pandas, `pd.read_csv(path, chunksize=100_000)` gives you an
iterator of frames and keeps peak memory bounded to roughly one chunk.""",
                "stats": _stats(prompt_tokens=38, output_tokens=214, tokens_per_second=47.3),
            },
            {
                "id": "m-3",
                "role": "user",
                "content": "Does that still work if the export has a BOM at the start?",
            },
            {
                "id": "m-4",
                "role": "assistant",
                "content": """Not quite -- with plain `utf-8` the BOM ends up glued to the first header name, so
`row["amount"]` still works but `row["order_id"]` raises `KeyError`. The actual key is
`"\\ufefforder_id"`.

Switch the codec to `utf-8-sig`, which consumes the BOM if present and is harmless if not:

```python
with open(path, newline="", encoding="utf-8-sig") as handle:
    yield from csv.DictReader(handle)
```

That is the one-line fix. It is worth defaulting to `utf-8-sig` for anything exported from
Excel, which writes the BOM routinely.""",
                "stats": _stats(prompt_tokens=272, output_tokens=131, tokens_per_second=46.1),
            },
        ],
    },
    {
        "id": "demo-sqlite-locked",
        "title": "SQLite migration fails with database is locked",
        "timestamp": "2026-08-09T14:05:00Z",
        "messages": [
            {
                "id": "m-10",
                "role": "user",
                "content": "My migration script intermittently fails with `sqlite3.OperationalError: database is locked`. It only happens when the app is running.",
            },
            {
                "id": "m-11",
                "role": "assistant",
                "content": """That error means another connection holds a write lock past your busy timeout. SQLite
allows many readers but only one writer, and the default timeout is 5 seconds.

Two changes usually settle it:

1. **Enable WAL mode** so readers stop blocking the writer:

   ```sql
   PRAGMA journal_mode = WAL;
   ```

2. **Raise the busy timeout** on the migration connection so it waits instead of failing
   immediately:

   ```python
   connection = sqlite3.connect(db_path, timeout=30.0)
   connection.execute("PRAGMA busy_timeout = 30000")
   ```

If it still fails, the usual culprit is a long-lived transaction left open by the running
app -- a connection that ran `BEGIN` and has not committed. Look for a code path that opens
a transaction and returns early on an error without rolling back.""",
                "stats": _stats(prompt_tokens=44, output_tokens=186, tokens_per_second=45.8),
            },
        ],
    },
    {
        "id": "demo-embeddings",
        "title": "What a vector embedding actually is",
        "timestamp": "2026-08-09T11:20:00Z",
        "messages": [
            {
                "id": "m-20",
                "role": "user",
                "content": "Explain vector embeddings without the usual hand-waving. What is actually being stored?",
            },
            {
                "id": "m-21",
                "role": "assistant",
                "thoughts": """The user is explicitly asking to skip the vague explanation, so I should avoid the
"words become numbers that capture meaning" framing and be concrete about what the array
literally is.

Concrete points worth making: it is a fixed-length array of floats; the length is a
property of the model, not the input; position in the space is what carries information,
not any individual number; similarity is measured by angle, which is why cosine similarity
shows up everywhere.

I should be careful not to claim individual dimensions are interpretable -- they generally
are not, and that is a common overstatement.""",
                "content": """A fixed-length array of floating point numbers. That is the whole storage format --
for `nomic-embed-text` it is 768 of them, and it is 768 whether you embed one word or three
paragraphs. The length is a property of the model, not the input.

What makes it useful is **where** the array lands, not what any single number says. The model
is trained so that inputs used in similar contexts land near each other, so position encodes
relationships:

- "invoice" and "receipt" land close together
- "invoice" and "trombone" land far apart
- direction matters more than magnitude, which is why similarity is nearly always measured
  as the cosine of the angle between two vectors rather than the distance between them

One caveat worth stating plainly: individual dimensions are **not** interpretable. There is no
"formality axis" at index 214. Meaning is distributed across the whole array, which is why you
can compare two embeddings but cannot read one.""",
                "stats": _stats(prompt_tokens=41, output_tokens=228, tokens_per_second=44.6),
            },
        ],
    },
    {
        "id": "demo-apache-regex",
        "title": "Regex for Apache combined log format",
        "timestamp": "2026-08-08T18:30:00Z",
        "messages": [
            {
                "id": "m-30",
                "role": "user",
                "content": "Give me a regex that parses the Apache combined log format into named groups.",
            },
            {
                "id": "m-31",
                "role": "assistant",
                "content": """```python
import re

LOG_LINE = re.compile(
    r'(?P<host>\\S+) \\S+ (?P<user>\\S+) \\[(?P<time>[^\\]]+)\\] '
    r'"(?P<method>[A-Z]+) (?P<path>\\S+) (?P<protocol>[^"]+)" '
    r'(?P<status>\\d{3}) (?P<size>\\d+|-) '
    r'"(?P<referer>[^"]*)" "(?P<agent>[^"]*)"'
)

match = LOG_LINE.match(line)
if match:
    record = match.groupdict()
```

Two details that trip people up:

- `size` is `-` rather than `0` when the response had no body, so treat it as a string and
  convert deliberately.
- The bracketed timestamp contains a space before the timezone offset, which is why the
  pattern matches "anything that is not a closing bracket" instead of `\\S+`.""",
                "stats": _stats(prompt_tokens=33, output_tokens=172, tokens_per_second=48.0),
            },
        ],
    },
    {
        "id": "demo-rust-go",
        "title": "Rust or Go for a small internal CLI",
        "timestamp": "2026-08-08T09:15:00Z",
        "messages": [
            {
                "id": "m-40",
                "role": "user",
                "content": "Small internal CLI, team of four, none of us know either language well. Rust or Go?",
            },
            {
                "id": "m-41",
                "role": "assistant",
                "content": """For that specific situation, **Go** -- and the deciding factor is the team, not the language.

| | Go | Rust |
|---|---|---|
| Time to first useful tool | Days | Weeks |
| Learning curve for four newcomers | Gentle | Steep at the borrow checker |
| Single static binary | Yes | Yes |
| Cross-compilation | Trivial (`GOOS`/`GOARCH`) | Workable, more setup |
| Runtime safety guarantees | GC, data races possible | Strong compile-time guarantees |

Rust earns its learning curve when you need predictable latency without a garbage collector,
tight memory control, or the compiler to rule out data races in real concurrent work. An
internal CLI usually needs none of those.

The honest counterpoint: if this tool is likely to grow into something long-lived and
performance-sensitive, paying the Rust cost once -- while the codebase is still four files --
is cheaper than migrating later.""",
                "stats": _stats(prompt_tokens=39, output_tokens=196, tokens_per_second=45.2),
            },
        ],
    },
]

MEMORIES = [
    "Prefers Python for data work and dislikes unnecessary dependencies.",
    "Works on a small internal tools team of four engineers.",
    "Wants direct answers with the caveats stated, not hedged ones.",
]

GROUPS = [
    ("grp-python", "Python help", ["demo-streaming-csv", "demo-sqlite-locked"]),
]


def _with_message_timestamps(chats: list[dict]) -> list[dict]:
    """Stamp every message with a fixed time derived from its chat.

    The transcript renders a per-message time next to the copy/regenerate/fork
    controls, so messages left unstamped would show a footer with the time
    missing. Offsets are derived, not chosen, so this stays reproducible.
    """
    stamped: list[dict] = []
    for chat in chats:
        base = datetime.strptime(chat["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        messages = [
            {**message, "timestamp": (base + timedelta(minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")}
            for index, message in enumerate(chat["messages"])
        ]
        stamped.append({**chat, "messages": messages})
    return stamped


def build_demo_app(*, frontend_dist: Path):
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
            generation=GenerationSettings(temperature=0.7, num_ctx=8192),
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

    return create_app(
        dependencies,
        preview=True,
        serve_frontend=True,
        frontend_dist=frontend_dist,
        readiness_check=lambda: True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
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

    app = build_demo_app(frontend_dist=args.frontend_dist)
    print(app.state.session_manager.bootstrap_token, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
