# Cortex QA pass: stacked debt, over-engineering, bugs, and brittle areas

- **Date:** 2026-09-03
- **Snapshot:** `chore/drop-dependabot-version-updates` at `71147e5e` (main + 1)
- **Method:** independent read of every top-level surface (API, services,
  repositories, execution, llama.cpp runtime, launcher, frontend, tests,
  tooling, CI, packaging, docs), then **every finding below re-verified** by
  running code, measuring, or a second independent grep/AST pass. The
  verification method for each item is recorded in §9.
- **Relationship to `docs/audits/2026-09-03-ownership-review.md`:** that
  document's fixed items were re-checked and are **not** repeated here (see the
  end of §9). Everything in §§3-8 was confirmed against the current tree.

---

## 1. Verdict

The code is careful. Failure paths are handled, comments explain intent, and
the gates are green. The debt is not sloppiness — it is **accumulation**: a
large abandoned execution ambition that was never unwound, scaffolding classes
from the original prototype that were never deleted, and a set of "keep the old
adapter working" seams for adapters that do not exist.

Three shapes recur:

1. **Structure kept alive for tests and history, not for the running program.**
   A 653-line lazy-import barrel with zero production consumers; 2,944 lines of
   spikes no gate runs; 357 lines of dead classes; a dead settings field
   propagated through the public OpenAPI contract.
2. **Hot paths that re-do bounded work at high frequency.** The execution SSE
   endpoint runs blocking SQLite on the event loop 100×/s; the frontend store
   rewrites the whole generation object twice per SSE frame, defeating the rAF
   batcher built to prevent exactly that; history selection rebuilds and
   re-measures a full prompt per stored message.
3. **Invariants held by convention rather than by a gate.** 543 hand-synced
   barrel entries with no test; six hand-maintained `0.1.0` version strings
   against `v1.0.0` tags; a `py.typed` marker with no Python type checker; a
   local check script that omits a gate CI enforces.

**47 findings**: 9 correctness, 10 brittleness, 7 over-engineering, 7 dead
code, 6 performance, 8 process. Nothing here is a break in the sandbox — the
containment boundary (AST allow-list, out-of-process worker, Windows Job Object
with memory/CPU limits, hold-until-job-attached handshake) held up under
probing; see the rejected-claims table in §9.

---

## 2. Evidence baseline

All commands run from the repository root on this Windows host, this session.

| Gate | Command | Result |
| --- | --- | --- |
| Backend tests | `python -m pytest -q` | **816 passed, 1 skipped**, 72.3 s |
| Python lint | `python -m ruff check backend tests tools main.py Cortex_Preview.py` | clean |
| Contract drift | `python tools/generate_contracts.py --check` | clean |
| Frontend unit | `npm test -- --run` | **252 passed** (30 files), 36.2 s wall / **178.9 s environment setup** |
| Frontend e2e | `npx playwright test e2e/chat.spec.ts` | **not run** — Chromium is not installed on this host (`npx playwright install` needed). No e2e claim is made below. |

**Environment caveat that is itself a finding (QA-43).** The interpreter here
is **Python 3.14.0** with packages in a user site directory, not a venv, and
**ruff 0.15.17** is installed while `pyproject.toml` pins `ruff==0.16.5`. The
lint result above therefore came from a different linter than CI runs. CI
resolves its lock for **Python 3.11**.

| Package | Installed | Declared |
| --- | --- | --- |
| ruff | 0.15.17 | `==0.16.5` **(drift)** |
| fastapi | 0.141.1 | `>=0.139,<0.142` |
| starlette | 1.6.0 | `>=0.40,<2` |
| uvicorn | 0.52.4 | `>=0.51,<0.53` |
| pydantic | 2.13.5 | `>=2.13,<2.14` |
| cryptography | 50.0.1 | `>=49,<51` |
| Pillow | 12.3.0 | `>=12.3,<12.4` |

---

## 3. Correctness defects

### QA-01 — Settings database uses the pragma combination the chat database was fixed away from · **High**

`repositories/sqlite_settings.py:176` sets `PRAGMA synchronous = NORMAL`, and
that file **never sets `journal_mode`**. The database therefore runs in
rollback-journal mode with reduced fsync.

`repositories/legacy_storage.py:289-294` states the rule in the repository's
own words: WAL + `synchronous=NORMAL` "is SQLite's documented safe-and-fast
combination … but cannot corrupt the database file, **which rollback-journal
mode does not guarantee under the same pragma**." The settings store is exactly
the case that comment warns about.

Measured at runtime:

| Database | `journal_mode` | `synchronous` | Safe? |
| --- | --- | --- | --- |
| `settings.sqlite3` | `delete` | `1` (NORMAL) | **no** |
| chat database | `wal` | `1` (NORMAL) | yes |
| `execution.sqlite3` | `wal` | `2` (FULL) | yes |

Blast radius is bounded by the two validated backup generations and
corrupt-primary recovery this store already has, so a power loss costs a
restore, not the workspace. **Fix: one line** — add `PRAGMA journal_mode = WAL`
to `_ensure_schema`, mirroring `legacy_storage._create_tables`.

### QA-02 — `range(0)` is rejected by the code validator · **Medium**

`execution/code_execution.py:351` and `:359` guard with
`if not _constant_range_bound(node.iter)`. `_constant_range_bound` returns the
`len()` of the range, so a legitimate empty range returns `0`, which is falsy,
and the program is rejected as `bounded_range_required` — an error code that
does not describe what is actually wrong.

Verified by calling `validate_code_source` directly over 11 crafted programs:

```
rejected  for i in range(0): ...        -> bounded_range_required   (WRONG)
rejected  n = 10; for i in range(n)     -> bounded_range_required   (correct)
ACCEPTED  for i in range(3): ...                                    (correct)
```

**Fix:** compare against `None` (`if _constant_range_bound(...) is None`) in
both `visit_For` and `visit_comprehension`. The same call is also made twice in
`visit_For` (line 351, then again at 353).

### QA-03 — The SSE rAF batcher is defeated by the store it writes into · **Medium**

`hooks/useGenerationStream.ts:145-175` builds `createRafBatchedFlusher`
specifically so "a fast token stream doesn't trigger a store update (and every
subscriber's re-render) per SSE event". The same event handler then performs
**two unbatched store writes per event**:

- `setGenerationCursor(job.jobId, cursor)` — `stores/useChatStore.ts:134`
  builds a new `generation` object whenever `eventId >= lastEventId`, which the
  caller has already guaranteed. Always a new reference.
- `setStatusText(job.jobId, data.message)` — `useChatStore.ts:140` builds a new
  `generation` object with no equality check. Every `content_delta` carries
  `message: "Response content available."` (`api/routes.py:2076`, via
  `JobProgressSink.publish_progress`, which merges `{"message": message}` into
  every payload), so the value is *identical* on every frame and a new object is
  created anyway.

`features/chat/ChatPage.tsx:94` subscribes to the whole `state.generation`
object with default `Object.is` equality. A new reference per frame means the
824-line `ChatPage` re-renders on every SSE frame regardless of the batcher —
one frame per 80 characters of answer (`api/routes.py:2427`, `_chunks`).

**Fix:** return `state` unchanged when the value did not move — `eventId >
lastEventId` in `setGenerationCursor`, `text !== statusText` in `setStatusText`.
Two lines; no API change.

### QA-04 — Execution SSE runs blocking SQLite on the event loop at 100 Hz · **Medium**

`api/routes.py:1169-1194`. The stream body calls `repository.events(job_id, …)`
and `repository.get_job(job_id, …)` **synchronously** — no `asyncio.to_thread`
— then `await asyncio.sleep(0.01)`. Each `ExecutionRepository.connect()` opens
a fresh SQLite connection and issues two PRAGMAs before the query
(`execution/repository.py:126-145`).

Measured on this host, 300 iterations against a real repository:

```
events() + get_job() : 3.334 ms per poll
-> ~33% of one event-loop second per open execution stream
```

That is one stream. Because the calls are not off-loaded, the cost is paid on
the same loop that serves the generation SSE stream and every other request.

Compare `api/jobs.py:543-574`, which polls **in-memory** at 25 ms for the
generation stream. The execution stream polls **disk** at 10 ms.

**Fix:** wrap both repository calls in `asyncio.to_thread` and raise the sleep
to the 25 ms the generation stream already uses. (The number is from one
machine; the shape of the finding — synchronous disk I/O on the loop at 100 Hz
— does not depend on the machine.)

### QA-05 — Execution SSE drops the connection after ~6 s of silence · **Low-Medium**

`api/routes.py:1187`: `if idle_rounds >= 600: return`, with `sleep(0.01)` — a
**6-second** idle cap. A job parked in an approval-pending state emits no events
by design, so its stream closes almost immediately and the client must reconnect
(or fall back to the 1 s `/execution/tasks` poll, QA-36). Either the cap should
be minutes, or the endpoint should emit an SSE comment heartbeat. Neither
exists.

### QA-06 — Two `_SAFE_PROFILE` regexes with different limits · **Low**

```
execution/repository.py:37           ^[a-z][a-z0-9._-]{0,99}$
execution/resource_accounting.py:20  ^[a-z][a-z0-9._-]{0,63}$
```

A 70-character profile name is storable but not accountable. Neither module
imports the constant from the other. One constant, one home.

### QA-07 — 13 load-bearing `assert`s in shipped code · **Low**

`recipes.py:260,266,272,317`; `recipe_coordinator.py:460,479`;
`repository.py:349,569,594`; `server_manager.py:348,571,839,1122`. Several are
load-bearing rather than documentation — `repository.py:349 assert row is not
None`, `recipes.py:317 assert plan.tolerance is not None`. Under `python -O`
they vanish and execution continues on `None`.

Currently safe **only** because `packaging/Cortex.spec:56` sets `optimize=0`.
That is an implicit contract between a packaging flag and 13 runtime
invariants, with nothing recording the dependency.

### QA-08 — Deprecated Starlette status constant, 10 call sites · **Low**

The installed Starlette (1.6.0, inside the declared `<2` range) warns on
`HTTP_422_UNPROCESSABLE_ENTITY`. The live pytest run emitted the warning from
`api/routes.py:581` and `:1914`; the constant appears at 10 sites in
`routes.py`. Rename to `HTTP_422_UNPROCESSABLE_CONTENT`.

Related: `starlette>=0.40,<2` spans a major-version boundary (0.4x → 1.x). That
range is why a deprecation landed silently.

### QA-09 — Asset path arithmetic breaks the declared install layout · **Low**

`services/llm.py:46-50` resolves prompt assets via
`Path(__file__).resolve().parents[3] / "assets"`. That works only from the
source tree. `pyproject.toml` declares `cortex_backend` as an installable
package (`[tool.setuptools.packages.find] where = ["backend"]`) that ships
`py.typed`; installed into site-packages, `parents[3]` points outside the
distribution. The same pattern appears in `api/app.py:258` for `frontend_dist`.

---

## 4. Brittle areas

### QA-10 — 543 hand-synced entries, no gate, no production consumer · **Medium**

`execution/__init__.py` is **653 lines** maintaining the *same* 181 symbols in
**three** hand-written places:

| List | Count |
| --- | --- |
| `if TYPE_CHECKING:` import block | 181 |
| `__all__` | 181 |
| `_LAZY_SUBMODULES` | 181 |

They are in sync today (verified by AST diff — zero symbols missing from any
list, no duplicates). **Nothing enforces that.** A symbol added to `__all__` but
not to `_LAZY_SUBMODULES` raises `AttributeError` at first access, at runtime,
in whichever code path touches it first.

And the barrel has **zero production consumers**. An exhaustive grep for
`from cortex_backend.execution import`, `import cortex_backend.execution`, and
`execution.<Symbol>` across `backend/`, `main.py`, `Cortex_Preview.py`, and
`packaging/` returns nothing — every production import goes straight to the
submodule (`from cortex_backend.execution.repository import …`). The only
consumers are 8 test files and
`tools/execution_spikes/artifact_security_review.py`.

653 lines of machinery whose stated purpose is to keep worker entrypoints from
importing 20 submodules, serving only callers that are not worker entrypoints.

### QA-11 — URL mutation inside a render-phase state initializer · **Medium**

`app/App.tsx:84` — `useState(readLauncherCredentials)`. That function calls
`window.history.replaceState` (`App.tsx:75`) to strip the bootstrap/handoff
credentials, i.e. a side effect during render, and it is **not idempotent**: the
second call sees a URL the first call already scrubbed.

`main.tsx` wraps the app in `StrictMode`. Measured with a throwaway probe
against this project's React 19 and testing-library (probe deleted after the
run; `git status` clean):

```
initializer calls = 2      retained value = first call's result
```

So it works today, and it works because React retains the first invocation's
result — an implementation detail of the very dev check designed to surface
impure initializers. **Fix:** move the scrub into an effect, or memoise the read
at module scope so a second call is a no-op.

### QA-12 — Route capability wiring by `getattr` string lookup · **Medium**

`api/routes.py:2769-2846` — five resolvers (`_execution_runtime`,
`_fake_execution_coordinator`, `_recipe_coordinator`, `_scratch_coordinator`,
`_code_coordinator`) that gate routes on
`callable(getattr(coordinator, "start_scratch", None))`,
`getattr(coordinator, "code_execution_available", False)`, and similar. There is
no `Protocol` for a coordinator. Rename `start_code` and every affected route
silently becomes a 404 reading "Local code execution is unavailable" —
indistinguishable from the intended disabled state. With no Python type checker
in the project (QA-13), nothing catches it before runtime.

### QA-13 — `py.typed` shipped, no Python type checker anywhere · **Medium**

`backend/cortex_backend/py.typed` exists and `pyproject.toml` ships it as
package data, advertising the package as typed. There is **no mypy or pyright**
in `pyproject.toml`, `requirements*.txt`, `scripts/check.ps1`,
`.github/workflows/quality.yml`, `CONTRIBUTING.md`, or `AGENTS.md`. A stale
`.mypy_cache/` at the root is the only trace of one ever running.

The frontend has `strict: true` TypeScript with a `tsc` gate in CI. The backend
— heavily annotated, with `Protocol` classes it never checks — has none. The
duck-typed seams in QA-12 and QA-22 are exactly what a type checker would pin.

### QA-14 — Eleven string assertions against a PowerShell file no gate runs · **Medium**

`tests/test_pinned_wasmtime_smoke_script.py` reads
`tools/execution_spikes/run_pinned_wasmtime_smoke.ps1` as text and asserts on 11
exact substrings, e.g.:

```python
assert "$VenvPython = Join-Path $VenvRoot \"Scripts\\python.exe\"" in source
assert "StartsWith($venvRootFull + [IO.Path]::DirectorySeparatorChar" in source
```

Any whitespace change, rename, or reformat fails the suite with no behavioural
change. It verifies nothing that runs. And:

- `run_pinned_wasmtime_smoke.ps1` is **not invoked** by `quality.yml` or
  `check.ps1`.
- **`wasmtime` is not a dependency** — it appears nowhere in `pyproject.toml`,
  `requirements.txt`, or either lockfile.

Eleven assertions guarding a pin for a runtime the project does not use.

### QA-15 — A guard test that checks the wrong file and the wrong string · **Low**

`tests/test_chat_correctness.py:300-303`:

```python
source = (Path(__file__).parents[1] / "main.py").read_text(encoding="utf-8")
self.assertNotIn("VectorDatabaseManager()", source)
```

Two problems. The composition root is `Cortex_Preview.py`, not `main.py` —
`main.py` only imports `build_preview_app` from it. And the assertion is a
literal substring: `VectorDatabaseManager(db_path)` or
`VectorDatabaseManager(app_paths=paths)` — the two forms its own `__init__`
accepts — pass unnoticed.

### QA-16 — A permanently green tautology · **Low**

`tests/test_core_foundation.py:206-209` asserts that
`(REPOSITORY_ROOT / "Chat_LLM" / "Chat_LLM").glob("*.py")` is empty. That
directory no longer exists, and `Path.glob` on a missing directory yields
nothing. The test can never fail again.

### QA-17 — `reuseExistingServer: true` in CI · **Low**

`frontend/playwright.config.ts:15` sets it unconditionally. The conventional
guard is `!process.env.CI`. As written, a stale listener on 4174 in CI is
silently adopted instead of failing.

### QA-18 — Vite dev server reaches the whole repository · **Low**

`frontend/vite.config.ts:33` — `fs: { allow: [".."] }` puts the repository root
inside the dev server's `/@fs/` reach. Loopback-bound and dev-only, so the
exposure is narrow, but the allowance is far broader than the one import it
presumably enables (`contracts/cortex-api.ts`), which could be allow-listed
directly.

### QA-19 — Test globals and Node globals typed into app source · **Low**

- `frontend/eslint.config.js:15` applies `{...globals.browser, ...globals.node,
  ...globals.vitest}` to **all** `**/*.{ts,tsx}` — so `process`, `require`,
  `__dirname`, `describe`, `it` count as defined inside browser components.
- `frontend/tsconfig.app.json` sets `"types": ["vitest/globals", "vite/client"]`
  with `"include": ["src"]`, compiling tests and app in one program with test
  globals ambient.

Neither is a bug today; both remove a guard rail that would catch a Node-only
API leaking into the browser bundle.

---

## 5. Over-engineering

### QA-20 — `build_router()` is 1,581 lines · **High debt**

`api/routes.py:243-1823`: one function containing **53 route decorators** and
**65 nested functions**, closing over `session_bearer`, `require_session`, and
`dependencies`. The file is 3,194 lines with 30 further module-level helpers.

The closure buys three shared names. It costs: no route can be tested,
imported, or reviewed in isolation; every endpoint diff touches the same
function; and the file is the largest merge-conflict surface in the repository.
Splitting by resource (`chats`, `settings`, `models`, `execution`,
`generations`, `jobs`) with `require_session` as a module-level dependency is a
mechanical change.

### QA-21 — 36 lines to probe a seam the repository owns · **Medium**

`services/generation.py:26-62` — `_call_with_optional_kwargs`, of which ~30
lines are a docstring justifying `inspect.signature().bind()` over
`try/except TypeError`. The reasoning is correct. It is applied to exactly two
call sites (`engine.translate_text`, `engine.generate_chat_title`) on a seam
with **two implementations, both in this repository**: `SynthesisAgent`
(`services/llm.py`) and `FakeGenerationEngine` (`testing/fake_ollama.py`).
Giving the fake the current signature removes the need for the probe.

### QA-22 — Runtime capability probing for adapters that do not exist · **Medium**

`services/generation.py:160-300` decides, at runtime and per turn, whether the
engine supports:

- `fit_history` → gates `host_observations` into `observation_kwargs`
  ("Older engines neither accept nor render them")
- `fit_attachments_to_context` ("Engines that do not offer it (the narrower
  fakes in the test suite, and any older adapter)")
- `set_status_callback`
- `generate_chat_title`
- structured history in `generate()`

`grep -rn "def fit_history\b"` returns **one** definition, `services/llm.py:688`.
There is no "older adapter". The branches exist so partial test doubles can be
passed as engines — production code carrying test-shaped conditionals.
Declaring `GenerationEngine` as the real contract and completing the fake
deletes the probing.

### QA-23 — Hand-rolled control-character scan · **Low**

`frontend/src/api/client.ts:73-80` — `cleanValidationText` iterates code points
via `Array.from` + `codePointAt` to replace C0/C1 controls with spaces. A
single regex replace over the two control-character ranges (U+0000-U+001F and
U+007F-U+009F) does the same in one line, with identical surrogate behaviour.

### QA-24 — Cancellation built as a hand-rolled producer/consumer · **Medium**

`llamacpp/chat_client.py:198-330` — `_chat_abortable` exists so Stop releases
llama-server's slot promptly. It does so with a reader `Thread`, a
`queue.Queue(maxsize=1)`, a `reader_done` `Event`, and **two** 50 ms poll loops
(producer retries `put(timeout=0.05)` on `Full`; consumer retries
`get(timeout=0.05)` on `Empty`), plus a `finally`-ordering comment explaining
why `reader_done` must be set before the `with` block closes the response.

It is correct — the earlier producer-spin leak is fixed — but the file's own
header states that Cortex "never actually streams tokens from the model runtime
itself". So an entire streaming consumer exists purely as a cancellation
mechanism for an adapter whose result is immediately reassembled into a
non-streamed shape (`synthetic_payload`, line 320). Closing the response from
the cancellation watcher, or using `httpx`'s own cancellation, expresses the
same intent with far less concurrency surface.

### QA-25 — The polling effect written twice · **Low**

`app/App.tsx:344-364` and `:371-398` are the same ~20-line pattern (immediate
refresh, `setInterval` gated on `document.visibilityState`, a
`visibilitychange` listener that refreshes on return, a cleanup clearing both),
differing only in interval and callback. One `usePolling(fn, ms, enabled)` hook
removes ~35 lines and one class of cleanup mistake.

### QA-26 — Documentation mass around the dormant half · **Medium**

| Path | Size |
| --- | --- |
| `docs/adr/` (25 files) | 247 KB |
| `docs/audits/` | 112 KB |
| `docs/archive/` (superseded plans) | 98 KB |
| `docs/` total | **495 KB** |
| Root `*.md` | 64 KB |

`execution/` is **15,908 of 33,704** backend lines (47%), and `test_phase*.py`
is **6,798 of 21,556** test lines (32%, 32 of 75 files). The ADRs and phase
tests document a signed/attested/AppContainer worker path; §6 shows how much of
its supporting tooling no gate runs.

---

## 6. Dead code and stacked debt

### QA-27 — 357 lines of dead classes in the live chat store · **High shave**

`repositories/legacy_storage.py` (1,616 lines) holds five classes. Three are
dead:

| Class | Lines | Production refs | Test refs |
| --- | --- | --- | --- |
| `DatabaseManager` | 937 | live | many |
| `PermanentMemoryManager` | 235 | live | many |
| `VectorDatabaseManager` | **141** | **0** | 1 (a guard asserting it is never constructed) |
| `ShortTermMemory` | **107** | **0** | **0** |
| `MemoryManager` | **109** | **0** | **0** |

`ShortTermMemory` and `MemoryManager` have **zero references anywhere in the
repository**, tests included. They are original prototype scaffolding, still
carrying the old docstring style (`Args:` blocks) and trailing whitespace the
rest of the file does not use. `VectorDatabaseManager` is dead by policy — its
only mention is the (broken, QA-15) test asserting it is never instantiated.

**Shave: 357 lines**, plus the QA-15 test.

### QA-28 — A dead settings field carried through the public contract · **Medium shave**

`core/settings.py:125-131` — `SuggestionSettings`, docstring:
"Cortex no longer generates or renders follow-up suggestion prompts."

It is nonetheless still:

- a required field of `CortexSettings` (`settings.py:148`),
- exported from `core/__init__.py:22,43`,
- mapped in `repositories/legacy_settings.py:43-44,51`,
- present in `contracts/openapi.json` and `contracts/cortex-api.ts:160,543`,
- seeded into 5 e2e specs and 2 frontend unit tests.

`grep -rn "\.suggestions"` across `backend/`, `main.py`, `Cortex_Preview.py`,
and `frontend/src` returns **zero consumers**. A dead feature that every client
of the API still has to model.

### QA-29 — 2,944 lines of spikes no gate runs · **High shave**

`tools/execution_spikes/` is 4,633 lines. CI (`quality.yml`) and `check.ps1`
between them invoke **four** scripts. The rest:

| Unreferenced by any gate | Lines |
| --- | --- |
| `phase0_probe.py` | 849 |
| `appcontainer_smoke.py` | 707 |
| `recipe_sandbox_qualification.py` | 367 |
| `recipe_parser_fuzz.py` | 264 |
| `native_launcher_qualification.py` | 263 |
| `assemblyscript_qualification.py` | 255 |
| `cancellation_corpus.py` | 130 |
| `run_pinned_wasmtime_smoke.ps1` | 109 |
| **Total** | **2,944** |

Several are cited as evidence in ADRs, so this is a judgement call rather than a
delete — but "evidence recorded in an ADR" and "verified on every change" are
different guarantees, and today the repository only holds the first.

### QA-30 — An undocumented second entry point · **Medium**

`Cortex_Preview.py:236-252` defines its own `main()` with `argparse`, plus
`_open_preview_browser` (19 lines) and `_preview_browser_url` (4 lines). It runs
`uvicorn.run(...)` on port 8765 directly, bypassing everything `main.py` does:
`InstanceLock`, `ensure_frontend`, the handoff secret, WebView2, signal
handling, startup diagnostics. Because it passes no `handoff_secret`, the
frontend's session-reconnect path cannot work in this mode.

`README.md` documents only `python main.py`. Nothing references
`Cortex_Preview.main` — CI lints and compiles the file, no more. The module is
the production composition root; the CLI attached to it is vestigial.
**Shave: ~40 lines**, and the name should follow (QA-33).

### QA-31 — `purge_expired` superseded but retained · **Low**

`execution/repository.py:1337-1347`. `ExecutionCleanupSupervisor` — now
correctly wired at `api/app.py:242` — calls `repository.cleanup_expired(...)`
(`repository.py:1236-1335`), not `purge_expired`. The older method's only
callers are 4 tests and one spike.

### QA-32 — Stale `Chat_LLM` references · **Low**

`Chat_LLM/` no longer exists. Still referenced by:

- `pyproject.toml:39` — `extend-exclude = ["Chat_LLM"]` (ruff excluding nothing)
- `tests/test_core_foundation.py:120` — in `forbidden_roots`
- `tests/test_core_foundation.py:207` — the tautology of QA-16

`forbidden_roots` also still guards `PySide6`/`PyQt5`/`PyQt6`/`main_window`. The
Qt boundary check is worth keeping; the dead-path entries are not.

### QA-33 — Names that no longer describe their role · **Medium**

| Name | What it actually is |
| --- | --- |
| `repositories/legacy_storage.py` | the **current, only** chat + permanent-memory store (1,616 lines, WAL, backups, schema v4) |
| `Cortex_Preview.py` | the **production composition root** for both packaged and source launches |
| `create_app(preview=True)` / `app.state.preview` | the normal shipped mode; `preview=False` disables execution routes |
| `DurableFakeCoordinator` | imported by `api/app.py` and referenced from live route resolvers |

Every one of these reads as "not the real thing" to a new reader (or an agent
following `AGENTS.md`), which is exactly backwards. Renaming is mechanical and
removes a standing source of misreading.

---

## 7. Performance

### QA-34 — History selection rebuilds a whole prompt per stored message · **Medium**

`services/llm.py:766-810`, `_select_history`. For **every** message in the
thread it constructs `candidate = [message, *selected]`, renders the entire
selection with `_format_history_messages`, builds a complete synthesis prompt
(system prompt, memories, attachments, observations) via
`PromptTemplate.build_synthesis_prompt`, then token-estimates every part. There
is a deliberate no-early-exit comment (candidate sizes are non-monotonic), so
the walk continues past the point the budget is full.

Measured on this host (400-char messages, `fit_history` only):

| Messages | `num_ctx` 8 192 | 32 768 | 131 072 |
| --- | --- | --- | --- |
| 100 | 2.4 ms | 2.0 ms | 3.6 ms |
| 400 | 11.4 ms | 25.2 ms | 31.7 ms |
| 1 000 | 59.4 ms | 96.9 ms | 242.8 ms |
| 2 000 | 146.8 ms | 271.2 ms | **792.8 ms** |

It runs in a worker thread, so it does not block the loop — but it is pure
per-turn CPU and garbage that grows with thread length, on a product whose
threads only get longer. A cached per-message token estimate plus an incremental
running total makes it linear with a small constant.

### QA-35 — Up to five full-thread loads per generation turn · **Medium**

Inside `_start_generation_job` (`api/routes.py:1874-2210`),
`deps.chats.get_chat` is called at lines **1909, 2001, 2046, 2146, 2183** — five
times per turn. `legacy_storage.load_chat` (`:712-744`) has **no pagination**: it
selects every message row for the thread and `json.loads` the `sources`,
`attachments`, and `generation_stats_json` columns for each. `GET /chats/{id}` —
which the frontend calls again on every generation completion via
`reconcileChat` — does the same. The last two calls in `runner()` are
back-to-back and could share one read.

### QA-36 — Unconditional 1 s poll for the life of the app · **Low-Medium**

`app/App.tsx:353` polls `/execution/tasks` every second whenever
`system.execution_preview_available` — the shipped default — with no "stop when
idle" condition and no backoff. Each tick is a SQLite query against the
execution database even when there has never been a task. The visibility gate
(good) only pauses it while the window is hidden. The companion `/system` poll
at 2 s (`:386`) has the same shape.

### QA-37 — `jobs.events()` rescans the retained list every 25 ms · **Low**

`api/jobs.py:543-574`. Each tick takes `self._lock` and evaluates
`[e for e in record.events if e.sequence > cursor]` over a list holding up to
`DEFAULT_MAX_EVENT_COUNT = 256` events — roughly 10 k comparisons per second per
open stream, under the registry lock, usually to find nothing. The list is
sorted by `sequence`; `bisect` or a stored index makes it O(log n).

### QA-38 — sessionStorage write per SSE frame · **Low**

`hooks/useGenerationStream.ts:222` calls
`persistActiveJob({...job, lastEventId: cursor})` inside the event handler — a
`JSON.stringify` plus a synchronous `sessionStorage.setItem` on **every** frame,
i.e. once per 80 characters of answer, while the token text itself is carefully
rAF-batched right beside it. Persisting on a coarser trigger (the rAF flush, or
every Nth event) preserves the resume guarantee at a fraction of the cost.

### QA-39 — jsdom environment dominates the frontend test run · **Low**

`vitest.config.ts` sets `environment: "jsdom"` globally for all 30 files.
Measured this session: **178.9 s of environment setup** behind a 36.2 s
wall-clock run. Files that need no DOM (`lib/navigation`, `lib/composerDraft`,
`stores/useChatStore`) could opt into the `node` environment via
`environmentMatchGlobs` or a per-file pragma.

---

## 8. Process, gates, and versioning

### QA-40 — The local check script omits a gate CI enforces · **Medium**

`scripts/check.ps1` states it runs "the fast gates in
`.github/workflows/quality.yml`". It does not run the **lockfile drift check**
(`quality.yml:60-69`: `uv pip compile` for both locks, then
`git diff --exit-code`). Edit `pyproject.toml`, run `./scripts/check.ps1 -Tier
full`, see green, push, fail CI. Adding three lines closes the gap.

Two smaller notes on the same surfaces:

- `quality.yml:63-68`: under `pwsh`, PowerShell 7.4's
  `$PSNativeCommandUseErrorActionPreference` combined with GitHub's
  `$ErrorActionPreference = 'stop'` makes the failing `git diff --exit-code`
  throw *before* the `if ($LASTEXITCODE -ne 0) { throw "…" }` can print the
  helpful regeneration message. The step still fails; the guidance is lost.
- `check.ps1` invokes bare `npm`, while `README.md:266` warns to use `npm.cmd`
  "when PowerShell execution policy blocks the `npm.ps1` shim" — so the script
  can fail on the machine configuration its own README documents.

### QA-41 — Version identity is broken · **Medium**

Six hand-maintained declarations of `0.1.0`:

| Site |
| --- |
| `pyproject.toml:7` |
| `main.py:49` (`CORTEX_VERSION`) |
| `backend/cortex_backend/api/app.py:229` (FastAPI `version`) |
| `backend/cortex_backend/launcher/frontend.py:313` and `:377` (two defaults) |
| `frontend/package.json:4` |

plus generated `contracts/openapi.json:3435`.

Meanwhile the repository carries tags **`v0.95.7`** (2025-10-17) and
**`v1.0.0`** (2026-01-20, on a commit titled "Update index.html"), both
ancestors of HEAD. So a `v1.0.0` build reports `0.1.0` from its own API. There is
**no release workflow** — `.github/workflows/` contains only `quality.yml` — and
no tag-to-version relationship of any kind.

### QA-42 — Two unlinked sources of dependency truth · **Low-Medium**

`requirements.txt` duplicates `[project.dependencies]` verbatim (10 lines);
`requirements-dev.txt` duplicates `[project.optional-dependencies].dev`. They
match today. **Nothing checks that.** CI's drift gate compiles the locks from
`pyproject.toml` only, so a pin edited in one file and not the other passes every
gate — and `requirements.txt` is the file `README.md:214` tells users to
install.

### QA-43 — No reproducible development environment · **Medium**

Established in §2: this host runs **Python 3.14** with packages in a user site
directory, and **ruff 0.15.17** against a `ruff==0.16.5` pin that landed in
`3049bede`. There is no venv bootstrap script, no `check.ps1` step asserting
installed versions match the lock, and no "install the dev lock" step in
`CONTRIBUTING.md`. The pinned-lint gate is only as good as whatever the
contributor last installed.

### QA-44 — Stale and duplicated root documentation · **Low**

- `Change_Log.md` (32.6 KB) was last modified **2026-07-20**; **311 commits**
  have landed since. Its newest entry predates the current architecture.
- `Desktop-Quick-Setup-Guide.md` (2.5 KB, same date) restates README's setup and
  **contradicts it**: the guide says `ollama pull nemotron-3-nano:4b`,
  `README.md:211` says `ollama pull qwen3:8b`.

### QA-45 — Nothing exercises the real frontend↔backend pair · **Medium**

Every Playwright spec intercepts `**/api/v1/**` with `page.route(...)` and serves
hand-written JSON (`e2e/chat.spec.ts:6-40` and equivalents). The suite runs
against `npm run dev` with no backend. The API contract between the two halves is
therefore verified only by generated TypeScript types — real serialization, real
status codes, real SSE framing, and real session exchange are never executed
together. The fixtures have already drifted: they still seed `suggestions`
(QA-28), a field with no consumer on either side.

### QA-46 — Demo fakes ship inside the production app module · **Low**

`api/app.py:37-41` imports `FakeGenerationEngine`, `FakeOllamaGateway`, and
`FakeOllamaState` from `cortex_backend.testing.fake_ollama` at **module scope**,
for `build_demo_dependencies()` (`:63-87`), which `create_app` calls whenever
`dependencies` is `None` (`:236`). The packaged Windows build therefore carries a
306-line fake model gateway, and a mis-wired `create_app` silently produces a
working app backed by a fake instead of failing. Production wiring
(`Cortex_Preview.build_preview_app`) always passes real dependencies, so this is
a packaging/blast-radius concern, not a live defect.

### QA-47 — Silent exception swallowing concentrated in one module · **Low**

`execution/local_runtime.py` has **34** `except Exception` handlers, many
`except Exception: pass` with no logging (`:123, 128, 133, 150, 156, 160, 168,
172, 249, 254, 372, 377, 491, …`). Most are legitimate best-effort teardown
(`_stop_process`'s join/terminate/kill ladder). The consequence is that a
*systematic* worker failure — a launch path that always throws — is invisible:
no log line, no metric, just a worker that never produces a result. A single
`logger.debug(..., exc_info=True)` in the teardown handlers costs nothing and
makes the difference between one flaky teardown and a broken path observable.

---

## 9. Cross-check ledger

Every finding above was verified by a second method before being written down.
Claims that did not survive verification are listed after the table.

| ID | How it was verified |
| --- | --- |
| QA-01 | Instantiated all three repositories and read back `PRAGMA journal_mode` / `PRAGMA synchronous`; compared against the rationale comment at `legacy_storage.py:289-294` |
| QA-02 | Executed `validate_code_source` over 11 crafted programs; recorded accept/reject per case |
| QA-03 | Read the store reducers and the single `ChatPage` subscriber; traced `message` into every `content_delta` through `JobProgressSink.publish_progress` |
| QA-04 | Benchmarked `events()` + `get_job()` 300× against a real `ExecutionRepository`; confirmed no `to_thread` in the async generator |
| QA-05 | Arithmetic on `idle_rounds >= 600` × `sleep(0.01)`; confirmed no heartbeat emission in the stream body |
| QA-06 | Direct grep of both `_SAFE_PROFILE` definitions |
| QA-07 | Grep for `^\s+assert ` across `backend/`, `main.py`, `Cortex_Preview.py`; read `optimize=0` at `Cortex.spec:56` |
| QA-08 | Deprecation warnings emitted by the live pytest run; grep of all 10 call sites |
| QA-09 | Read `parents[3]` against the package layout declared in `pyproject.toml` |
| QA-10 | AST script diffing all three lists (181/181/181, zero gaps, zero duplicates); exhaustive grep for every barrel-import form across production paths → zero |
| QA-11 | Wrote a throwaway React 19 + StrictMode probe with a non-idempotent initializer; measured `calls=2, retained=first`; probe file deleted (`git status` clean) |
| QA-12 | Read all five resolvers; confirmed no `Protocol`; cross-referenced with QA-13 |
| QA-13 | Grep for `mypy` across `pyproject.toml`, both requirements files, `check.ps1`, `quality.yml`, `CONTRIBUTING.md`, `AGENTS.md` → zero; confirmed `py.typed` present |
| QA-14 | Counted assertions (11); confirmed the script is absent from both gate files; grep for `wasmtime` in pins and locks → zero |
| QA-15 | Read the test; confirmed the composition root is `Cortex_Preview.py:113-115`; confirmed the assertion is a literal substring |
| QA-16 | `ls -d Chat_LLM` → does not exist; `Path.glob` on a missing directory yields nothing |
| QA-17 | Read `playwright.config.ts:15` |
| QA-18 | Read `vite.config.ts:33` |
| QA-19 | Read `eslint.config.js:15` and `tsconfig.app.json` `types`/`include` |
| QA-20 | AST: `build_router` spans 243-1823 (1,581 lines), 53 `@router.*` decorators, 65 nested functions |
| QA-21 | AST span = 36 lines; grep → exactly 2 call sites, both in `generation.py` |
| QA-22 | `grep -rn "def fit_history\b"` → 1 definition; enumerated all `engine_factory` uses (1 production, 1 fake) |
| QA-23 | Read `cleanValidationText` |
| QA-24 | Read `_chat_abortable` end to end against the module docstring's "never actually streams" claim |
| QA-25 | Diffed the two effects in `App.tsx` |
| QA-26 | `wc -c` over `docs/`; `wc -l` over `execution/`, `backend/`, and `tests/test_phase*` |
| QA-27 | AST class spans; per-class grep across production and test trees separately |
| QA-28 | Grep for `suggestion` across backend, contracts, `frontend/src`, and `frontend/e2e`; `.suggestions` consumer grep → zero |
| QA-29 | Scripted check of each spike filename against `quality.yml` + `check.ps1`; summed line counts |
| QA-30 | AST spans; grep for every `Cortex_Preview` reference; compared with README's documented commands |
| QA-31 | Read `ExecutionCleanupSupervisor.run_once` → calls `cleanup_expired`; grep for `purge_expired` callers |
| QA-32 | `ls -d Chat_LLM`; grep across `pyproject.toml` and tests |
| QA-33 | Traced each name to its live consumers (`Cortex_Preview.py:113-115`, `api/app.py:33,242`, the route resolvers) |
| QA-34 | Benchmarked `SynthesisAgent.fit_history` across 4 thread lengths × 3 context sizes |
| QA-35 | Line-located all 5 `get_chat` calls within the function span; read `load_chat` for pagination |
| QA-36 | Read both effects and their enabling conditions |
| QA-37 | Read `jobs.events`; confirmed `DEFAULT_MAX_EVENT_COUNT = 256` |
| QA-38 | Read the SSE handler; cross-referenced `_chunks(size=80)` for the frame rate |
| QA-39 | Measured from the live `npm test -- --run` output |
| QA-40 | Read `quality.yml:60-69` and all of `check.ps1` side by side; checked README's own `npm.cmd` note |
| QA-41 | Counted every `0.1.0` literal per file; `git tag` + `git log -1` per tag + `merge-base --is-ancestor`; `ls .github/workflows/` |
| QA-42 | Compared `requirements.txt` against `[project.dependencies]`; confirmed the CI gate reads `pyproject.toml` only |
| QA-43 | `importlib.metadata.version` for 12 packages vs pins; `python -V`; grep for a venv/bootstrap step |
| QA-44 | `git log -1` per file; `git rev-list --count`; diffed the two `ollama pull` lines |
| QA-45 | Read every `page.route` in `e2e/`; compared the `/api/v1/system` mock against `schemas.py:108-120` |
| QA-46 | Read `api/app.py:37-41,63-87,236`; `wc -l testing/fake_ollama.py` |
| QA-47 | Counted `except Exception` per file; read the `local_runtime.py` cluster for logging |

### Claims tested and **rejected** — deliberately not reported as findings

| Hypothesis | Why it was dropped |
| --- | --- |
| `execution/__init__.py`'s three lists are out of sync | AST diff: 181/181/181, no gaps, no duplicates. Reported as *unguarded*, not *broken*. |
| The code sandbox is escapable via unbounded builtins over `range` | `sorted(range(10**7))`, `list(range(10**8))`, and `((10**1000)**1000)**1000` all pass the AST validator, and `sys.settrace` cannot interrupt C-level builtins — **but** `local_runtime.py:466-473` attaches a Windows Job Object (memory, CPU, active-process limits) *before* releasing the child from its `go` handshake, and the parent enforces a wall-clock deadline. Contained. |
| `TrustedHostMiddleware` is bypassable via the `"["` allow-list entry | `api/app.py:283` adds `"["` to work around Starlette's naive `split(":")[0]`. Any bracketed host matches that middleware — but every route independently calls `SessionManager.validate_request_context`, which parses with `urlsplit` and checks the real hostname. Defence in depth intact. |
| The frontend SPA fallback allows path traversal | `api/app.py:317-320` resolves the candidate and checks `is_relative_to(dist)`; `resolve()` also collapses symlinks. Correct. |
| StrictMode breaks the launcher handoff | Measured: React calls the initializer twice but retains the first result. Downgraded to a latent brittleness (QA-11). |
| `_select_history` is O(n²) in thread length | It is O(N × kept), and `kept` is capped by the context budget. Measured and reported with real numbers instead (QA-34). |

### Prior-audit items re-checked and confirmed **already fixed** — not repeated

- Chat database now runs `journal_mode = WAL` with two validated backup
  generations (`legacy_storage.py:274-276, 294`).
- Chat-title generation is bounded (`CHAT_TITLE_TIMEOUT_SECONDS = 20`,
  `routes.py:172`).
- The llama.cpp reader thread no longer spins after the consumer exits
  (`reader_done` Event, `chat_client.py:218`).
- `README.md:113-114` describes the sandbox accurately as "a small, restricted
  subset of Python (no imports, no `def`/`class`/`while` …)".
- `vitest` `testTimeout` raised to 15 s, with a comment explaining the inner 5 s
  wait it must clear.
- Python lockfiles exist (`requirements*.lock.txt`) with a CI drift gate.
- Execution retention cleanup has a production caller
  (`ExecutionCleanupSupervisor`, wired at `api/app.py:242`).
- The bootstrap token is delivered in a URL fragment and is not printed.
- `Chat_LLM/` and its tracked bytecode are gone from the tree.

---

## 10. Suggested shave order

Ordered by (value ÷ risk), not by severity. Each row is independently
shippable.

| # | Action | Findings | Removes / changes | Risk |
| --- | --- | --- | --- | --- |
| 1 | `PRAGMA journal_mode = WAL` in the settings store | QA-01 | 1 line | none |
| 2 | Change-check the two generation-store reducers | QA-03 | 2 lines | none |
| 3 | `to_thread` the execution SSE repository calls; sleep 25 ms; raise the idle cap | QA-04, QA-05 | ~6 lines | low |
| 4 | Delete `ShortTermMemory`, `MemoryManager`, `VectorDatabaseManager` + the broken guard test | QA-27, QA-15 | **−361 lines** | none |
| 5 | Delete the `execution` barrel (or reduce it to the ~8 names tests use) | QA-10 | **−620 lines** | low — 9 call sites |
| 6 | Delete `Cortex_Preview.main()` and its browser helpers; rename the module | QA-30, QA-33 | **−40 lines** | low |
| 7 | Remove `SuggestionSettings`; regenerate contracts; drop it from 7 fixtures | QA-28 | contract shrinks | low |
| 8 | Add the lockfile-drift step to `check.ps1`; add a dev-env version assertion | QA-40, QA-43 | ~10 lines | none |
| 9 | Single `__version__`; a release workflow; reconcile the tags | QA-41 | 6 sites → 1 | low |
| 10 | Add mypy to dev deps + `check.ps1` + CI; type the coordinator seam | QA-13, QA-12 | new gate | medium — expect a first-run backlog |
| 11 | Delete `test_pinned_wasmtime_smoke_script.py` and the wasmtime script; delete the QA-16 tautology | QA-14, QA-16 | **−148 lines** | none |
| 12 | Fix `range(0)`; unify `_SAFE_PROFILE`; rename the 422 constant | QA-02, QA-06, QA-08 | ~14 lines | none |
| 13 | Memoise token estimates in `_select_history` | QA-34 | ~15 lines | medium — needs prompt-shape tests |
| 14 | Collapse the 5 per-turn `get_chat` calls; extract `usePolling` | QA-35, QA-25 | ~40 lines | medium |
| 15 | Split `build_router()` by resource | QA-20 | 1,581 lines → ~6 modules | medium — mechanical, large diff |
| 16 | Decide the execution package's fate: keep and gate the spikes, or remove the dormant half with its ADRs and phase tests | QA-26, QA-29 | up to **−10,000 lines** | high — the project-shape decision, already framed as M3 in `docs/NEXT.md` |

Rows 1-9 and 11-12 together remove roughly **1,200 lines** and close three gate
gaps without changing a single behaviour a user can observe.
