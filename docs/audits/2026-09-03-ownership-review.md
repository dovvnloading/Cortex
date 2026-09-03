# Cortex ownership review: direction, defects, and refactor plan

- Date: 2026-09-03
- Snapshot: `codex/qa-reliability-pass-2` at `7288a343` (210 commits ahead of `main`)
- Method: full quality gates run locally, then six independent subsystem reviews
  (API, services/repositories, execution, runtime/launcher/packaging, frontend,
  tests/docs/tooling), with every high-severity claim re-verified against the code.
- Relationship to `findings.md`: that audit is closed (67 of 68 fixed, 1 refreshed).
  Nothing here repeats it. This document is the next pass, written from the
  position of a sole owner deciding what the project is and where it goes.

---

## 1. Verdict

Cortex is in better shape than most single-maintainer projects of its size. The
runtime seam, the session model, the settings store, the GGUF download path, the
llama.cpp process manager, the artifact publish path, and the approval binding
for code execution are all careful, tested work. The quality gates are green.

The project's problem is not quality. It is **shape**. Three things are out of
proportion:

1. **Half the execution package does not run.** Roughly 7,400 of 15,600 lines
   under `backend/cortex_backend/execution/` are only reachable from spikes,
   phase tests, or a signed-worker path that nothing launches. The 25 ADRs, the
   4,700-line `tools/execution_spikes/`, and about 20 `test_phase2_*` files
   exist to support that dormant half. Meanwhile the shipped sandbox is a tiny
   Python dialect (no `def`, `while`, `import`, or method calls) that the README
   describes as "approval-gated Python".
2. **The core is under-invested relative to the periphery.** The chat database,
   which holds the only data a user actually cares about, runs without WAL and
   without a backup, while the settings database has two validated backup
   generations and corrupt-primary recovery. A completed llama.cpp turn leaks a
   spinning thread. A mid-stream llama.cpp error is persisted as a successful
   blank answer.
3. **Four overlapping roadmaps and no release.** There are four planning
   documents (140 KB), one of which declares itself normative in contradiction
   of `AGENTS.md`, and none of which is current. There is no release workflow,
   no tag-to-version relationship, and five hard-coded copies of `0.1.0`.

The direction is therefore: **shrink to what runs, harden what users touch,
and ship a versioned build.** Everything below serves that.

---

## 2. Evidence baseline

All commands run from the repository root on this Windows host.

| Gate | Result |
| --- | --- |
| `python -m pytest -q` | 762 passed, 1 skipped, 70 s |
| `python -m ruff check ...` | clean |
| `python tools/generate_contracts.py --check` | clean |
| `npm run typecheck` / `npm run lint` | clean |
| `npm test -- --run` | 246 passed, **1 failed** (timeout under load; passes alone in 1.1 s) |
| `npm audit --omit=dev` | 0 vulnerabilities |

Environment findings that are themselves defects:

- The local Python environment has FastAPI 0.141.1, uvicorn 0.52.1, and
  cryptography 50.0.0 installed. All three are **outside the pins** in
  `pyproject.toml` and `requirements.txt`. Local tests and CI tests therefore run
  against different dependency versions. There is no Python lockfile.
- The failing frontend test (`App.test.tsx:113`) waits up to 5,000 ms inside a
  test whose own budget is 5,000 ms. Under full-suite load the lazy chat route
  takes longer than 5 s to settle, so the test can only fail by timeout.
  `vitest` reported 233 s of environment setup for 30 files, which is the actual
  cause of the load.

---

## 3. Direction

### 3.1 What Cortex is

A Windows-native, local-first chat workspace over two local runtimes (Ollama and
managed llama.cpp), with durable threads, permanent memory, per-chat generation
control, and a small approval-gated compute tool. That is a coherent, useful
product. It is what the README shows in its screenshots, and it is what the
last two months of commits actually touched.

### 3.2 What Cortex is not, yet

It is not a coding harness. `AGENTS.md` says that harness is a goal, and
`LOCAL_CODING_AGENT_HARNESS_STANDARD.md` lays out six stages of which zero are
started. The dormant half of the execution package was built for a signed,
attested, AppContainer-launched worker that the maintainer cannot produce.
`AGENTS.md` itself names this pattern: "a gate nobody can satisfy is
indistinguishable from cancelling the work."

### 3.3 The three theses

**Thesis 1: the chat core is the product. Make it excellent before widening it.**
Every defect in section 4 marked P1 is in the path a user hits on an ordinary
turn. Fix those, give the chat database the same durability the settings
database already has, and make the two runtimes indistinguishable in failure
behaviour.

**Thesis 2: the execution boundary should be honest and small.** Keep the three
profiles that run (`scratch.auto.v1`, `code.exec.v1`, `recipe.image.v1`).
Delete the signed-worker, broker, attestation, release-gate, and native-launcher
machinery from the import path. Keep one idea from it: create-suspended, attach
Job Object, then resume. State the boundary in one paragraph: spawned child,
Job Object, scrubbed environment, AST allow-list, per-job workspace, user
approval bound to a source digest, no restricted token. That is a defensible
boundary described plainly, which is exactly what `AGENTS.md` asks for.

**Thesis 3: one roadmap, one version, one release path.** Replace the four
planning documents with a single one-page "what's next" list. Derive the
version from one place. Add a tag-triggered workflow that uploads the package
CI already builds and then throws away.

---

## 4. Fix now

Severity: P1 = user-visible on ordinary use or data at risk. P2 = correctness
under a plausible condition. P3 = wrong but bounded. Confidence reflects
whether the failure was traced end to end in code (high), traced with one
assumed condition (medium), or inferred (low).

### 4.1 P1

| # | Defect | Location | Confidence |
| --- | --- | --- | --- |
| 1 | **Every completed llama.cpp chat turn leaks a spinning thread.** The reader thread pushes into a `Queue(maxsize=1)` and retries `put` while the cancellation event is clear. On normal completion the consumer breaks at `[DONE]` and stops draining; the event is never set on success, so the reader's `finally` loops on `put(("done", ...))` forever, waking every 50 ms. Five turns leave five live `llama-chat-reader` threads. | `backend/cortex_backend/llamacpp/chat_client.py:232-277` | High (reproduced) |
| 2 | **A llama.cpp error after headers is persisted as a successful empty answer.** The stream loop reads only `choices`, `usage`, and `timings` from each chunk. An `error` chunk, or an `event: error` line, is dropped, the loop runs to EOF, and the route persists `result.response` with no empty-content check. The user sees a blank assistant turn marked succeeded and it enters history. The Ollama path raises correctly. | `llamacpp/chat_client.py:278-300`, `api/routes.py:1976-1990` | High |
| 3 | **The chat database has no WAL, no backup, and no corruption recovery.** `synchronous=NORMAL` in rollback-journal mode is the one combination SQLite documents as unsafe on power loss. The settings store (`sqlite_settings.py`) and the execution store (`execution/repository.py:161`, WAL) are both better protected than the transcripts. | `repositories/legacy_storage.py:101-105` | High |
| 4 | **Approve-then-Stop strands a code job in `cancelling` forever.** `cancel()` on an approved-but-unleased job transitions to `cancelling`; `_run_code` then returns from the `status in {"cancelled","cancelling"}` guard with no terminal transition. The lease is released in `finally`, so lease recovery never sees it, and startup recovery relaunches it into the same early return. The tray keeps `can_cancel=true`. The scratch path handles this correctly. | `execution/local_runtime.py:978-991`, compare `:1224-1226` | High |
| 5 | **The Job Object is attached after the child is already running.** `process.start()` precedes `_WindowsProcessJob(...)`. The child sends `ready` and executes user code without waiting, so under scheduler delay user code runs before memory, CPU, and process limits exist. The dormant `native_win32.py:457-548` already implements create-suspended, attach, resume. The image worker has no Job Object at all. | `execution/local_runtime.py:442-449`, `:215-225` | Medium-high |
| 6 | **A malformed SSE frame causes an infinite reconnect loop.** `JSON.parse` inside `emit` is unguarded; a throw rejects the whole stream, the hook treats it as transient, reconnects with `Last-Event-ID` at the same cursor, and receives the same frame. The reader is never cancelled on throw. | `frontend/src/api/client.ts:341-358`, `hooks/useGenerationStream.ts:283-304` | High |
| 7 | **Local and CI dependency versions differ, and there is no lockfile.** See section 2. Any FastAPI or Starlette behavioural change lands on one side only. | `pyproject.toml:11-21`, `requirements*.txt` | High |

### 4.2 P2

| # | Defect | Location | Confidence |
| --- | --- | --- | --- |
| 8 | Chat-title generation runs inside the uncancellable commit window with no timeout on the Ollama path, so a hung title call blocks Stop and shutdown. | `api/routes.py:1963`, `:2036-2044`, `api/jobs.py:167-172`, `:609-615` | Medium-high |
| 9 | Retry after a post-admission stream failure posts a second identical user message instead of regenerating the failed turn. | `frontend/src/features/chat/ChatPage.tsx:378-380`, `:541-553` | Medium-high |
| 10 | Reconnect backoff never resets; a long generation with several successful reconnects eventually waits up to 30 s per drop. | `hooks/useGenerationStream.ts:206`, `:281`, `:303`, `:324` | High |
| 11 | GGUF download failures lose their user-facing text. `GGUFDownloadError` carries no `user_message`, so the job runner records "Job failed. Please try again." for every refusal (overwrite, disk space, not-a-GGUF). | `llamacpp/download.py:67`, `api/jobs.py:651-655` | High |
| 12 | `os.link` in the download promote step makes the models directory NTFS-only; on exFAT or SMB it raises a plain `OSError`, unmapped, and the multi-GB staging file is deleted. | `llamacpp/download.py:283-292` | High |
| 13 | Launch failures never trip the crash-loop guard, and any early exit marks Vulkan "known bad" for 24 h regardless of cause (corrupt file, bad context size). | `llamacpp/server_manager.py:806-828`, `:1126-1132` | Medium |
| 14 | `/generations` returns 500 on a chat-revision race that `/regenerations` correctly maps to 409. | `api/routes.py:1314-1346` vs `:1551-1552` | High (path), medium (frequency) |
| 15 | Execution SSE route blocks the event loop with synchronous SQLite every 10 ms and ends silently after 6 s idle while a job waits for approval (which emits no events). Mitigated only because no UI consumer exists. | `api/routes.py:1050-1120` | High |
| 16 | `prepare()` runs blocking SQLite on the loop thread while holding the registry lock, stalling every worker publish and cancel. | `api/jobs.py:369-386`, `api/routes.py:2081` | High (mechanism) |
| 17 | Duplicate-request path re-reads without the approvals join, so a retried POST for a pending job reports `approval_state: not_required`. | `execution/repository.py:352-359`, `local_runtime.py:665-674` | Medium |
| 18 | Untrusted-data fences (`BEGIN/END UNTRUSTED ...`) are not escaped in the payload; an attachment can close the fence and inject a fake user question. Lands in the user role, not system, so bounded. | `services/llm.py:367-382`, `:403-409`, `:418-425` | High |
| 19 | `_select_history` rebuilds and re-measures a full prompt per stored message and the thread is loaded four times per turn. Long threads pay tens of MB of string work per turn. | `services/llm.py:752-777`, `Cortex_Preview.py:152`, `api/routes.py:1893`, `:1948`, `:2033` | High |
| 20 | Execution tasks poll at 1 Hz and `/system` at 0.5 Hz whenever a GGUF model is selected, with no visibility gate and no idle backoff, on a machine that is also running a model. | `frontend/src/app/App.tsx:344-378` | High |
| 21 | Binary fetcher has no byte ceiling, no free-space check, and no redirect host policy; the pinned SHA catches tampering only after the disk is filled. `download.py` already has the pattern. | `llamacpp/binary_fetcher.py:239-246` | High |
| 22 | Frontend test flake: inner wait equals test budget. | `frontend/src/app/App.test.tsx:113-152`, `vitest.config.ts` | High (reproduced) |

### 4.3 P3

| # | Defect | Location |
| --- | --- | --- |
| 23 | `TrustedHostMiddleware` never matches `::1` because Starlette splits on the first colon; the session manager parses it correctly, so the two guards disagree. | `api/app.py:279-282` |
| 24 | Chat revision is `COUNT(messages)`, so regenerate and rename do not move it; ABA across regenerations. Masked by one-active-job-per-kind. | `repositories/legacy_storage.py:536-541`, `services/chat.py:14-16` |
| 25 | Wrong status codes: approval-decision repository errors map to 404; attachment count violations return 409 not 422; missing `confirm` on memory clear returns 409; artifact download can return a bare non-JSON 500; shutdown sets `shutting_down` before the callback can fail. | `api/routes.py:1017-1021`, `:517-518`, `:613-621`, `:913-916`, `:279-289` |
| 26 | `POST /chats/{id}/messages` and `/generations` with a client `thread_id` create a chat with an arbitrary unvalidated id. | `api/routes.py:505-511`, `:1861-1863`, `api/schemas.py:672` |
| 27 | Job registry owner is `session_id` while execution owner is the installation principal; a re-exchanged session cannot see or cancel an in-flight generation but is still blocked by it. | `api/routes.py:2636-2638`, `:2793-2799` |
| 28 | `--api-key` is passed to `llama-server` on the command line, visible in Task Manager; the pinned build supports `LLAMA_API_KEY`. | `llamacpp/server_manager.py:1085-1094` |
| 29 | SQLite and in-memory `replace_message` diverge on attachments (SQLite writes NULL, memory preserves). | `repositories/legacy_storage.py:648-655` vs `repositories/chats.py:996-997` |
| 30 | `generation.py` retries on `TypeError`, the exact hazard its own comment rejects. | `services/generation.py:255-261`, `:297-301`, `:350-353` |
| 31 | `.code_workspaces/<job>` is never cleaned; recovery reuses a crashed run's workspace with `exist_ok=True`. | `execution/local_runtime.py:711`, `:1072-1078` |
| 32 | Every spawned worker re-imports `cortex_backend.execution.__init__` (411 lines of re-exports) and therefore cryptography, pydantic, Pillow, and ctypes, which is why a 15 s startup grace exists. | `execution/__init__.py`, `local_runtime.py:79-82` |
| 33 | `window.confirm` runs inside the stream finalizer, hanging the pending bubble until the native dialog closes. | `features/chat/ChatPage.tsx:253` |
| 34 | `node` prop from react-markdown is spread onto `<a>` and `<table>`, producing React warnings. | `features/markdown/SafeMarkdown.tsx:17-21`, `:75-80` |
| 35 | Focus indicators are suppressed at six places in the stylesheet with background-only replacements, and removed outright on the sidebar search and palette input. | `frontend/src/styles/tokens.css:231`, `:244`, `:330`, `:371`, `:517`, `:537`, `:627`, `:893` |
| 36 | `test_api_contract.py:118` spawns `"python"` rather than `sys.executable`. | `tests/test_api_contract.py:118` |

---

## 5. Strengthen

### 5.1 Data durability parity

The settings store is the model. Give the chat store the same properties:

- `PRAGMA journal_mode = WAL` on open (already done for the execution store).
- A validated backup on a schedule or on clean shutdown, using `VACUUM INTO`,
  with the two-generation rotate-only-after-validate logic already written in
  `sqlite_settings.py:87-167`.
- Corrupt-primary quarantine and recovery on startup, with a test that
  replaces the primary with garbage and asserts recovery from backup.

### 5.2 Runtime failure parity

Make the two runtimes fail the same way. The llama.cpp adapter should raise a
typed error on an `error` chunk, on EOF without `[DONE]`, and on an empty
accumulated content. Each `ChatClient` should raise `RuntimeFailure(backend,
kind)` so `llm.py:100-103` stops sniffing attributes. Add `user_message` to
`GGUFDownloadError` and `LlamaCppError` so the job registry relays safe text.

### 5.3 Dependency reproducibility

Generate a lockfile (`uv lock` or `pip-compile --generate-hashes`) and install
from it in CI and in `scripts/check.ps1`. Widen the pins to what is actually
tested, or narrow the local environment to match. Add `dependabot.yml` for pip,
npm, and GitHub Actions. Frontend has 21 minor updates pending, none urgent.

### 5.4 Test suite structure

- Add `tests/conftest.py` with `allowed_hosts`, `app`, `client`, and
  `auth_headers` fixtures. `ALLOWED_HOSTS` is copy-pasted in nine files and the
  `create_app` + `TestClient` + exchange dance is rebuilt in eighteen. There is
  exactly one `@pytest.fixture` in 20,000 lines.
- Register `slow`, `process`, and `windows` markers. Have the `quick` tier run
  `-m "not slow"`. Add `pytest-timeout` so a hung spawned worker fails in
  seconds instead of at the 30-minute job limit.
- Replace `time.sleep(0.03)` synchronization with events in
  `test_phase1_execution.py`, `test_llamacpp_server_manager.py`, and
  `test_execution_cleanup.py`. `findings.md` F-063 already recorded two
  full-suite-only failures of this class.
- Fix the vitest flake: raise `testTimeout` to 15 s in `vitest.config.ts` and
  investigate the 233 s of environment setup (jsdom per file; `happy-dom` or
  a shared environment would likely halve the suite).
- Rename `test_phase*` and `test_stage5_*` files to what they test. A file
  named after a roadmap phase tells the next reader nothing.
- Add direct tests for `repositories/memories.py` (none) and
  `execution/attachment_staging.py` (thin).
- Frontend: add a `client.ts` test for malformed and partial SSE frames, and an
  axe pass in Playwright.

### 5.5 CI shape

- Add `concurrency: { group: ${{ github.ref }}, cancel-in-progress: true }`.
  Stacked pushes currently burn about two serial hours each.
- Gate `heavy` on `paths:` (launcher, packaging, execution, llamacpp) plus
  `workflow_dispatch`. It runs on every PR today.
- Have `heavy` upload `dist/Cortex` as an artifact and launch the windowed
  path once, not only `--headless`. WebView2 and pythonnet breakage in the
  package is currently invisible.
- Add a tag-triggered release workflow that builds, uploads, and drafts a
  GitHub Release. The main executable has no Authenticode signature and no
  VERSIONINFO resource; both belong in that workflow.

### 5.6 Defence in depth for the WebView

`index.html` has no CSP. A WebView2 surface rendering model output should carry
`default-src 'self'` at minimum. `SafeMarkdown` is sound on its own, but this
costs nothing.

---

## 6. Separation of concerns and refactor map

### 6.1 Backend API (`api/routes.py`, 3,066 lines, 53 endpoints as closures)

Every handler is a closure inside `build_router()`, so none is importable and
the orchestration cannot be tested without FastAPI. Split into routers composed
by `build_router`:

| Router | Endpoints |
| --- | --- |
| `routers/session.py` | health, session, system, diagnostics (8) |
| `routers/chats.py` | chats, groups, forks (11 + fork) |
| `routers/settings_memory.py` | settings, memories (6) |
| `routers/models.py` | list, pulls, rescan, HF list, GGUF download (5) |
| `routers/generation.py` | generations, regenerations, legacy `/jobs` (10) |
| `routers/execution.py` | execution + chat attachments (13) |

Move `_start_generation_job` (315 lines), `_automatic_compute_observation`,
`_queue_code_proposal`, and `_code_execution_observations` into
`services/generation_orchestrator.py` taking `(deps, jobs, coordinator, owner)`
instead of `Request`. Move the `_execution_*` presenters into
`api/presenters/execution.py`. Replace the five hand-written error-code tables
and one inline dict with a single `{code: (status, message)}` mapping.

Delete: `POST /jobs/generation` (no caller, bypasses revision checks and
persistence), `POST /chats/{id}/messages` (tests only), `/execution/preview/fake`
(always 404 in production), `ApiClient.streamExecution` (no consumer), and the
`getattr(request.app.state, "x", None)` fallbacks for attributes `create_app`
always sets.

Duplicates to collapse: regeneration rules (`:1494-1512` and `:1908-1924`),
attachment validation (`:2824-2870` and `:2872-2900`), Last-Event-ID parsing
(three copies), and model-progress-to-sink mapping (two copies).

### 6.2 Services and repositories

`services/llm.py` (1,421 lines) does asset loading, prompt assembly, token
budgeting, runtime calls and error classification, three output parsers, the
repair loop, titling, and translation, and hands proposals back through mutable
`last_code_*` attributes that `generation.py` reads with `getattr`. Split by
purity:

- `prompting.py`: assets, templates, and one `fence()` helper that escapes or
  rejects the delimiter (fixes defect 18).
- `context_budget.py`: pure token and fit walks with per-message token caching
  (fixes defect 19).
- `response_parser.py`: memory, code, and thinking parsers returning a
  `ParsedResponse` dataclass (removes the mutable side channel).
- `synthesis.py`: orchestration only.

Make `GenerationEngine` honest: declare `fit_history`, `generate`,
`generate_chat_title`, and `set_status_callback` on the Protocol and delete the
`getattr` and `TypeError` probing.

`repositories/legacy_storage.py` (1,437 lines) is misnamed. `DatabaseManager`
and `PermanentMemoryManager` are the production chat and memo stores; only the
JSON migration is legacy. Rename to `sqlite_chat_store.py` and
`json_memo_store.py`, keep migration as a module function, and raise typed
exceptions instead of having `chats.py` match on message substrings
(`chats.py:674-680`, `:694-697`, `:751-754`).

Delete dead code: `VectorDatabaseManager` (`:842-982`, unreferenced, and unsafe
if revived: a single connection shared across threads with every error
swallowed), `ShortTermMemory`, `MemoryManager` (`:1220-1437`),
`_close_connection`, `clear_all_data`, `AppPaths.vector_database`,
`SQLiteSettingsRepository.restore_backup` (no caller, overwrites the primary
without setting it aside), and `services/memory_commands.apply_memory_command`
(the route implements its own policy).

`services/models.py` inventory is N+1 (`show` per model). Batch or cache.

### 6.3 Execution package (15,600 lines, about 48% dormant)

| Module group | Lines | Status |
| --- | --- | --- |
| repository, models, lifecycle, cleanup | 2,032 | wired |
| local_runtime, code_execution, scratch_compute | 3,154 | wired |
| recipe_coordinator (coordinator half) | ~450 | wired |
| recipe_provider, recipes (ImageTransformPlan) | ~680 | wired |
| artifact_boundary, attachment_staging | ~720 | wired |
| qualification (`build_execution_lifecycle` only) | ~80 | wired |
| coordinator, fake | 340 | test and showcase only |
| broker, native_broker, native_launcher, native_win32, native_recipe_attempt | 2,854 | dormant |
| bundle_installer, manifest, worker_provenance, worker_release, release_gate, release_attestation | 2,427 | dormant |
| worker_protocol, worker_runtime, resource_accounting | 1,408 | dormant |
| `RecipeWorkerClient` and readers in recipe_coordinator | ~350 | dormant |
| `__init__.py` re-export of everything | 411 | harmful (defect 32) |

Consolidation:

1. Move the dormant modules out of the import path (delete, or park under
   `tools/experimental/` with a README saying why). Remove
   `packaging/recipe_worker/`, `build_recipe_worker.ps1`,
   `tools/sign_recipe_worker.py`, the native probes in
   `tools/execution_spikes/` (keep the three fast deterministic corpora), the
   `test_phase2_*` files that test only the dormant modules, and the
   `heavy` CI steps that build and qualify the signed worker.
2. Fold `native_win32.py`'s create-suspended, attach-job, resume sequence into a
   single `_ChildProcessAttempt` used by all three profiles. The three attempt
   classes in `local_runtime.py:176-541` are about 85% identical, and
   `_run_scratch`, `_run_code`, and `RecipeExecutionCoordinator._run` are the
   same lease, transition, attempt, finish template with three hand-copied
   failure paths. That divergence is how defects 4 and 17 happened. One
   template plus three short profile bodies fixes defect 5 and gives the image
   worker a Job Object for free.
3. Collapse `lifecycle.py` and `qualification.py` into one `runtime.py` with
   `build_execution_lifecycle(repo, enabled: bool)`. Drop the profile string.
4. Replace the 411-line `__init__.py` with the five names `routes.py` uses.
5. Write the honest boundary paragraph (section 3.3) into the README in place
   of the current numbered list, and correct the README's description of what
   the sandbox language is.

Expected result: about 15,600 lines to 6,500 or 7,000, one worker-launch path,
one job-run template, and every remaining line reachable from the API.

`code_execution_enabled` and `automatic_compute` default to `True`
(`core/settings.py:114-115`). Per-run approval makes the first defensible, but
`AGENTS.md` says a capability "arrives as a deliberate, visible mode the user
turns on." Consider defaulting `code_execution_enabled` to `False` with a
first-use prompt.

### 6.4 Launcher and composition root

`main.py` (575 lines) is `launcher/main.py` living at the root: port
reservation, redacting diagnostics, uvicorn wiring, window liveness monitor,
headless loop, and the full orchestration in `_run_web`. Move those into
`launcher/ports.py`, `launcher/diagnostics.py`, and `launcher/runtime.py` and
leave argparse plus `main()` at the root.

`Cortex_Preview.py` is the composition root plus a legacy CLI that runs uvicorn
on a fixed port with no instance lock, no reserved socket, no handoff secret,
and opens the system browser. Move `build_preview_app` to
`cortex_backend/bootstrap.py`, delete the CLI, and update the four references
(`AGENTS.md`, `check.ps1`, `quality.yml`, `Cortex.pyproj`).

Version: five copies of `0.1.0` (`pyproject.toml:7`, `package.json:4`,
`main.py:49`, `api/app.py:229`, `launcher/frontend.py:313,377`) while git tags
say `v0.95.7` and `v1.0.0`. Add `cortex_backend.__version__` from package
metadata, import it everywhere, and have `generate_contracts.py --check` fail
if `package.json` drifts.

`launcher/frontend.py` copies the whole source tree, copies a cached
`node_modules` into it, builds, and copies back. A lock, `npm ci` when the lock
digest changes, and `vite build --outDir <staging>` with the existing atomic
swap gives the same guarantee with a fraction of the I/O.

### 6.5 Frontend

Phase 1 of the existing refactor plan moved state into stores but left every
API-calling action in `App.tsx` (848 lines), so the prop drilling it targeted
persists: `AppShell` takes 19 props, `ChatPage` 15, `SettingsPanel` 16.
`theme` is a hand-maintained mirror of `settings.appearance.theme` re-set at
four sites.

Decomposition:

- `app/useSession.ts` (credentials scrub, exchange, rebootstrap, epoch).
  `App.tsx` becomes the session gate and route switch, about 120 lines.
- `app/useWorkspaceLoader.ts` and a `useSystemStore` for `system`,
  `executionTasks`, and `llamacppStatus`.
- Derive `theme` from the settings store; `useTheme()` owns the `matchMedia`
  effect.
- `features/shell/useChatLibraryActions.ts` (rename, delete, groups, move);
  `AppShell` props drop from 19 to about 3.
- `features/execution/useExecutionTasks.ts` with visibility-gated polling
  (fixes defect 20).
- `features/models/useModelJobs.ts`, `features/settings/useMemory.ts`.
- `ChatPage`: extract `useChatDocument`, `useComposerDrafts` (the scoped-map
  plus ref pattern is the densest code in the file), and
  `useGenerationController`. `ChatPage` becomes about 200 lines of layout.
- Keep stores pure; feature hooks own API calls and receive `api` from a small
  context instead of props.

Dead code: `features/shell/SystemStatusCard.tsx`, `features/shell/LocalSetup.tsx`,
`hooks/useRafBatchedText.ts` have no non-test importers.

Stack: `@base-ui/react` plus `react-virtuoso` are justified. `cmdk` is
borderline; a Base UI Dialog plus a 40-line filter removes one dependency and
one styling idiom. Three overlay idioms coexist today (Base UI, cmdk, hand-rolled
listbox and row menu). Finish the `LocalModelMenu` move to `shared/ui/Select`.

Add `noUnusedLocals`, `noUncheckedIndexedAccess`, and type-checked ESLint
(`no-floating-promises`, `no-misused-promises`) plus `jsx-a11y`. Remove
`"types": ["vitest/globals"]` from the app tsconfig. Split `react-markdown`
plus `rehype-highlight` into their own chunk for WebView2 cold start.

---

## 7. Hygiene

| Item | Finding | Action |
| --- | --- | --- |
| `Chat_LLM/Chat_LLM/__pycache__/*.pyc` | 19 tracked bytecode files from the deleted Qt app; the entire tracked content of `Chat_LLM/` | `git rm -r Chat_LLM` |
| `Chat_LLM.sln`, `Cortex.pyproj` | Tracked VS project files the changelog says were removed | Delete, or move under an ignored IDE path |
| `.gitignore` (uncommitted) | Adds `docs/`, but 30 docs are tracked and the README links into them. Future `git add docs/...` silently no-ops; two docs are already invisible to `git status`. | Revert the line. If some docs are private, move those to an ignored `docs/private/` instead. |
| `findings.md` | 72 KB branch-specific triage log at repo root | Move to `docs/audits/2026-08-30-reliability-audit.md`; keep the verification tables |
| `Change_Log.md` | Last entry 2026-07-20; 339 commits since; `CONTRIBUTING.md` requires updates | Either generate from Conventional Commits at release time or delete and use GitHub Releases |
| `Desktop-Quick-Setup-Guide.md` | Duplicates README, Ollama-only, recommends a different model | Fold into README or delete |
| `docs/` roadmaps | Four overlapping plans; `LOCAL_CODING_AGENT_HARNESS_STANDARD.md:28` declares itself normative against `AGENTS.md:3-7`; `FRONTEND_HARNESS_REFACTOR_PLAN.md` says "not implemented" for phases that shipped; `OPEN_SOURCE_EXECUTION_PLAN.md` cites 345 tests (now 762); `REMAINING_RELIABILITY_FIXES.md` points at fixed items | Strike the normative sentence. Move all four plus `UI_MODERNIZATION_AUDIT.md` and `docs/adr/*-evidence.md` under `docs/archive/`. Write one `docs/NEXT.md`. |
| `docs/adr/` | 25 ADRs all numbered `0001` | Renumber, or archive with the dormant code they describe |
| README | Says "26-shot set" (6 captures exist); says chat model "doubles as the title model" (separate `title` field exists); never mentions translation, which is a live settings section; describes the sandbox as Python | Correct all four |
| Branches | 63 unmerged local branches; this branch is 210 commits ahead of `main` | Merge this branch. Delete merged and abandoned branches. |
| `.github` | No `dependabot.yml`, `CODEOWNERS`, release workflow; issue templates ask for "Smartphone / iPhone6" | Add the first three; rewrite templates for a Windows desktop app |
| `tools/screenshots/showcase_server.py` | Second `create_app` composition that duplicates `Cortex_Preview.py` wiring; `capture.ps1` writes to an empty `docs/images/` while README images live in `.github/images/` | Build from the shared bootstrap; point capture at `.github/images/` |
| `tools/generate_contracts.py` | Sound, but only for the default app; routes under non-default flags would be silently missing. Tests never diff the live spec against the checked-in file (CI does). | Add that assertion to `test_contract_generation.py` |

---

## 8. Expansion: what earns its place

The bar: a feature earns its place if it makes the chat core more useful for a
person running local models, uses infrastructure that already exists, and does
not add a new trust boundary. Listed in the order I would build them.

1. **Chat database durability** (section 5.1). Not a feature, but the first
   thing a user would want after losing a thread.
2. **Transcript export** (Markdown and JSON). Already in the frontend plan as
   6.8, small, and the data model supports it. Export is the cheapest form of
   backup a user can understand.
3. **Model-aware context indicator.** The backend already computes the token
   budget and trims history silently (`llm.py:543-868`). Show the user what
   fraction of the context the thread occupies and when history is being
   dropped. This turns an invisible failure into a visible one and costs one
   field on the generation snapshot.
4. **Search across message bodies.** Sidebar search is title-only. SQLite FTS5
   over the existing messages table is a schema migration and one endpoint.
5. **Prompt templates.** Optional in the frontend plan. A handful of saved
   system-instruction presets stored in the settings document. No new store.
6. **Memory review surface.** Model-proposed memories are correctly no longer
   auto-persisted, but the confirmation flow is the only place the user sees
   them. A "proposed, not saved" list in the memory panel closes the loop.
7. **Attachment text extraction for PDF.** Attachments already handle text and
   images. PDF text extraction with the existing size bounds is the most
   requested next type and needs no new trust boundary.

What not to build now:

- The coding harness stages in `LOCAL_CODING_AGENT_HARNESS_STANDARD.md`. Not
  until the execution package is consolidated and the honest boundary is
  written down. Building workspace edits and a terminal on top of the current
  15,600 lines would double the dormant surface.
- Semantic vector memory. The dormant `VectorDatabaseManager` should be deleted,
  not revived. If retrieval is ever wanted, FTS5 (item 4) covers most of the
  value with none of the embedding-model dependency.
- A plugin or MCP surface. New authority, new trust boundary, and no user
  asking for it.
- Multi-window or multi-profile. The instance lock and session model are
  single-window by design and correctly so.

---

## 9. Sequenced plan

Each milestone is independently mergeable and leaves the gates green.

### M1: Merge and stabilise (days)

- Merge `codex/qa-reliability-pass-2` into `main`. It is 210 commits of
  verified fixes and nothing is behind it.
- Fix defects 1, 2, 6, 10, 22 (llama.cpp reader leak and error chunk, SSE
  parse guard, backoff reset, vitest timeout). All are under 30 lines each.
- Generate the Python lockfile and reconcile the local environment (defect 7).
- Hygiene: remove `Chat_LLM/`, the VS project files, and the stale guide;
  revert the `docs/` ignore; move `findings.md`.
- Add CI `concurrency` and `paths:` gating.

### M2: Core durability (one to two weeks)

- WAL, validated backup, and corruption recovery for the chat store (defect 3).
- Typed runtime errors and `user_message` on download and llama.cpp errors
  (defects 11, 12, 13, section 5.2).
- Title generation outside the commit window with a timeout (defect 8).
- Retry-after-failure becomes regenerate (defect 9).
- Visibility-gated polling (defect 20).
- `conftest.py`, markers, `pytest-timeout`, sleep-to-event replacement.

### M3: Execution consolidation (two to three weeks)

- Delete the dormant half (section 6.3, steps 1, 3, 4).
- One `_ChildProcessAttempt` with create-suspended, attach, resume (defects 4,
  5, 17, 31, 32).
- Fix the execution SSE route to be async and to heartbeat (defect 15).
- Write the honest boundary paragraph and correct the README.
- Archive the phase ADRs and spike probes with the code they describe.

### M4: API and service decomposition (two to three weeks)

- Router split and orchestrator extraction (section 6.1).
- `llm.py` split by purity and `legacy_storage.py` rename (section 6.2).
- Global exception handler and single error table (defect 25).
- Fix defects 14, 16, 18, 19, 24, 26, 27.

### M5: Frontend decomposition (one to two weeks)

- Hook extraction per section 6.5; `App.tsx` to about 120 lines,
  `ChatPage.tsx` to about 200.
- Focus indicators, `node` prop, `window.confirm` (defects 33, 34, 35).
- Type-checked ESLint and stricter tsconfig.
- Replace `FRONTEND_HARNESS_REFACTOR_PLAN.md` with `docs/NEXT.md`.

### M6: Release (days)

- Single version source, VERSIONINFO, tag-triggered workflow with artifact
  upload and a windowed smoke test.
- First tagged release from `main`.

### M7: Expansion (ongoing, one item at a time)

Items 2 through 7 of section 8, in that order, each behind the gates.

---

## 10. What is genuinely well done

This section exists so the plan above is read as proportion, not dismissal.

- `api/jobs.py`: the reservation and commit protocol, request-fingerprint
  idempotency, and the shutdown split between grace and committed-wait are
  correct and documented.
- `api/security.py`: constant-time compares, hashed token storage, sliding
  expiry capped by absolute lifetime, bounded cleanup.
- `repositories/sqlite_settings.py`: compare-and-swap with 409, two validated
  backup generations, corrupt-primary quarantine with rollback, atomic
  one-time adoption.
- `llamacpp/server_manager.py`: readiness attests child identity via
  authenticated `/props` and canonical model path, the child binds its own
  port, kill-on-close Job Object with handle hygiene, two-lock design with
  documented order, interruptible acquisition, bounded teardown.
- `llamacpp/download.py`: every redirect hop validated after DNS resolution,
  non-global hosts rejected, free space checked per chunk, structural GGUF
  header parse, hidden staging name, refuses to clobber.
- `llamacpp/binary_fetcher.py`: hashes the whole extracted tree, forces a full
  hash at the launch boundary.
- `execution/repository.py`: terminal-state immutability under
  `BEGIN IMMEDIATE`, artifact publish with exclusive temp, fsync, `os.replace`,
  directory fsync, read-side re-verification of size, nlink, reparse, and
  SHA-256; crash-resumable quarantine tombstones.
- Approval binding: scope digest over source plus capabilities, re-checked
  with `hmac.compare_digest` after the lease, source re-hashed in the tray
  before Approve is enabled. No TOCTOU found on approval to execution.
- Network capability: DNS-pinned dialing with per-redirect re-validation.
- `services/attachments.py`: magic-byte sniffing, decode bounds checked before
  `verify()`, filename display-only, resolve re-binds artifact to job to
  profile and re-hashes.
- Memory trust boundary: proposals never auto-persist, `clear` requires a
  confirmation intent, memos rendered in the user role as fenced data.
- Frontend: idempotency-key retention with 4xx invalidation, cursor-based
  resume with claim-release semantics, generation-counter guards for stale
  responses, `SettingsPanel` three-way merge, sanitize-then-highlight ordering,
  the Playwright fixture that aborts on any unmocked request, and the generated
  contract used verbatim with no hand-duplicated API types.
- `packaging/build_recipe_worker.ps1`: a model of stage, verify, promote,
  rollback with native exit-code checks. Reuse the pattern for the main
  package's release workflow even after the worker itself is retired.
