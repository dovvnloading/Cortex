# Cortex repository audit findings

- Audit date: 2026-08-30
- Snapshot: `codex/qa-reliability-pass-2` at `463d0f9bd126b12821de6628469d56dfee752c11`
- Scope: backend APIs and persistence, model runtimes and downloads, React UI, launcher, packaging, generated contracts, CI, and representative tests.

This is a triage inventory, not a claim that every item is exploitable in the default configuration. Priority combines impact, likelihood, and how close the issue is to a trust or data-loss boundary. Reproduction qualifiers are stated where a race, local tampering, a custom configuration, or an unusual browser environment is required.

## Executive summary

| Tier | Count | Meaning |
| --- | ---: | --- |
| Priority 1 | 13 | Address first: demonstrated data-loss, integrity, privacy, or major workflow failures |
| Priority 2 | 41 | Important correctness, durability, security-hardening, accessibility, and operability issues |
| Priority 3 | 14 | Lower-impact bugs, resource leaks, test gaps, and maintainability concerns |
| **Total** | **68** | Deduplicated findings |

The most consequential themes are:

- backup creation can destroy the only recoverable settings or memory copy;
- settings updates have no compare-and-swap boundary and can lose concurrent changes;
- llama.cpp launch readiness does not establish process identity, so a loopback port race can receive private prompts;
- GGUF and packaged-runtime integrity checks have downgrade, overwrite, stale-build, and cache weaknesses;
- artifact retention is not scheduled, while the available purge method is unsafe to schedule as written;
- several frontend retry/stream/storage paths convert ambiguous or failed operations into duplicate work or false success;
- one quick-gate run was not clean: two backend tests failed in the full suite, although both passed alone and under immediate repetition.

## Priority 1 — address first

### F-001 — llama.cpp port race can send private prompts to an unrelated local service

**Status: fixed and verified** (`dcc82a13`).

**Confidence: high. Area: privacy/integrity.** `backend/cortex_backend/llamacpp/server_manager.py:295-298,519-524,721-736,777-782` chooses a free port with bind-then-close, starts the child later, and accepts any HTTP 200 from `/health`. `backend/cortex_backend/llamacpp/chat_client.py:72-85,110-116` then trusts that origin for full chat bodies.

A safe loopback reproduction held the selected port with an unrelated HTTP server while a disposable fake child remained alive. Cortex entered `ready`, posted `PRIVATE_PROMPT` to the unrelated service, and accepted its response. Exploitation requires another local process to win a narrow bind race, but the consequence is disclosure and substitution of model output. Reserve/pass an already-bound socket where supported, and make readiness attest the expected child/protocol instance rather than only status 200.

### F-002 — recipe-worker build can sign and report a stale executable after a failed build

**Status: fixed and verified** (`31266f3c`).

**Confidence: high. Area: build/supply chain.** `packaging/build_recipe_worker.ps1:23-30,66-76` suppresses cleanup errors, does not check PyInstaller's native exit code, and only verifies that an executable already exists. On this host, PowerShell's `$PSNativeCommandUseErrorActionPreference` is false, so `$ErrorActionPreference = 'Stop'` does not make a nonzero native exit terminate the script.

If a stale output is locked, cleanup can leave it behind; PyInstaller can then fail; the script can still announce success and sign the old tree. Build into a fresh unique directory, check every native exit code, validate build identity/version, and atomically promote only the new verified output.

### F-003 — opening a corrupt settings database overwrites its valid backup before validation

**Status: fixed and verified** (`be0e36a3`).

**Confidence: high. Area: data loss.** `backend/cortex_backend/repositories/sqlite_settings.py:53-61,203-210` calls `_create_backup()` before opening or validating the primary database and blindly copies primary over `.bak`.

Reproduction: create a valid backup, replace the primary with `b"corrupt-primary"`, then construct the repository. Initialization fails and the backup becomes the corrupt bytes too. Validate/open the primary before rotating recovery copies, use multiple generations, and test corrupt-primary/valid-backup startup.

### F-004 — memory recovery can lose both primary and backup on the next failed save

**Status: fixed and verified** (`e5e206e2`).

**Confidence: high. Area: data loss.** `backend/cortex_backend/repositories/legacy_storage.py:937-952` can recover from a good backup, but `_save_memos()` at `:972-974` first copies the still-corrupt primary over that backup before replacing primary.

Reproduction recovered `['first']` from backup, forced `os.replace` to fail during the next add, and left both files containing `{corrupt}`; a fresh manager loaded no memories. `tests/test_persistence.py:140-150` verifies fallback only, not the subsequent failure window. Once recovery occurs, first restore a validated primary atomically; never rotate an invalid source over the last good copy.

### F-005 — concurrent settings writes fail or silently lose updates

**Status: fixed and verified** (`3629fa5a`).

**Confidence: high. Area: data integrity/concurrency.** `backend/cortex_backend/api/routes.py:525-537` performs load → increment revision → unconditional save. `backend/cortex_backend/repositories/sqlite_settings.py:362-385` has no compare-and-swap or lock spanning that operation, and backup I/O is also unlocked.

Two stale snapshots both saved revision 1, with the second reverting the first change. A forced concurrent pair of API PUTs produced one repository-backed 500 and one 200; only one change survived. The reproduction did not isolate which of the unlocked repository/backup operations raised the 500. Require an expected revision, commit updates transactionally, return 409 on conflicts, and add concurrent API/repository tests.

### F-006 — model-produced memory can become persistent system-role prompt injection

**Status: fixed and verified** (`a52da282`).

**Confidence: high. Area: model trust boundary.** `backend/cortex_backend/services/llm.py:1245-1250,1331-1371` accepts arbitrary short model-produced additions, `backend/cortex_backend/api/routes.py:1975-1981` auto-persists them, and `backend/cortex_backend/services/llm.py:367-384` interpolates them verbatim into a later system message.

A focused reproduction confirmed that the parser accepted `Ignore all prior instructions...` and prompt construction inserted it verbatim with `role="system"`; route inspection confirms that accepted additions are automatically persisted. Memory being enabled is contextual consent to remember facts, not authority for model output to write future instructions. Bind memories to explicit user-originated facts, separate/delimit them as untrusted data, and require confirmation for model-proposed additions.

### F-007 — GGUF redirects can downgrade HTTPS and reach arbitrary/private HTTP targets

**Status: fixed and verified** (`88c92d6c`).

**Confidence: high. Area: network/model integrity.** `backend/cortex_backend/llamacpp/download.py:65-92` validates only the initial URL; `:178` enables automatic redirects without validating every hop and the final URL.

An HTTPS origin redirecting to HTTP was accepted and its body saved. This enables transport downgrade and, for a user-supplied URL or hostile upstream, an unauthenticated GET to a loopback/private target; the reproduction did not demonstrate disclosure of a private response back to the remote origin. Validate every redirect manually, reject HTTPS→HTTP, define and enforce a host/IP policy on the initial URL and every redirect after DNS resolution, and cap redirect count.

### F-008 — GGUF download is unbounded, minimally validated, and overwrites an existing model

**Status: fixed and verified** (`f20f76ba`).

**Confidence: high. Area: data loss/resource exhaustion.** `backend/cortex_backend/llamacpp/download.py:180-214` has no total-byte/free-space ceiling, accepts any body beginning with `GGUF`, and unconditionally replaces an existing same-name destination.

`b"GGUF-not-a-real-model"` was accepted. A chunked source can fill disk, while malformed content can replace a working local model and fail only at launch. Enforce configured and advertised sizes, reserve disk headroom, structurally validate GGUF metadata, download to a unique staging file, and require an explicit overwrite policy with rollback.

### F-009 — inherited Vite configuration can bake a remote API endpoint into a production package

**Status: fixed and verified** (`e1adcf96`).

**Confidence: high. Area: local-first privacy boundary.** `frontend/src/api/client.ts:68-75,95-105` accepts `VITE_API_BASE_URL` without restricting it to relative/loopback URLs. `backend/cortex_backend/launcher/frontend.py:145-153,248-251` inherits the build environment, and `packaging/build_windows.ps1:18-24` packages the resulting assets without inspecting the baked target.

An accidental `VITE_API_BASE_URL=https://...` environment value or `.env.production` can disclose the bootstrap token, chats, settings, and model requests to a remote endpoint that permits the app origin through CORS. Production builds should reject non-relative/non-loopback targets and record the resolved API target in a verified manifest.

### F-010 — “Retry last message” creates a new idempotency key

**Status: fixed and verified** (`6c986165`).

**Confidence: high. Area: duplicate work/data.** `frontend/src/features/chat/ChatPage.tsx:277-287` creates a new request ID on every `startGeneration()` call; Retry at `:441-445` invokes it anew.

If the original POST was admitted but its response was lost, retry creates another backend job and, from `/chat/new`, can create a second thread. Retain and reuse the admission key for ambiguous failures until status/replay proves the original request was not accepted.

### F-011 — model-job SSE EOF and cancellation are reported as success

**Status: fixed and verified** (`43f81455`).

**Confidence: high. Area: workflow correctness.** `frontend/src/api/client.ts:246-260` treats any stream EOF as normal. `frontend/src/app/App.tsx:385-422` treats a return without `kind:"error"` as successful, refreshes inventory, and can display “Model operation completed”; it does not treat terminal `state: cancelled` as failure/cancellation.

A transient disconnect can clear busy state and announce success for pull/rescan while backend work continues or has incomplete state. The GGUF-download caller suppresses that generic success toast, but then reports a generic failure because terminal completion data is absent even though work may continue. Require a terminal event, recover authoritative job status after EOF, and distinguish succeeded, failed, and cancelled.

### F-013 — Settings keeps a stale whole-document draft that can revert newer updates

**Status: fixed and verified** (`234c0fa8`).

**Confidence: high. Area: data integrity.** `frontend/src/features/settings/SettingsPanel.tsx:66,102-112` initializes draft state once and later submits the entire document. Updates made while Settings remains mounted—such as GGUF auto-selection or command-palette model/theme changes in `frontend/src/app/App.tsx:435-480,498-518`—do not resynchronize that draft.

Reproduction: open Settings with no chat model, download and auto-select a GGUF, then Save; the stale draft submits `chat: null` and reverts selection. Combine field-level patches with backend revision checks, or explicitly merge/resynchronize and surface conflicts.

### F-014 — expired execution artifacts and chat attachments are never purged in production

**Status: fixed and verified** (`e87a6fa1`, `cfddbf76`).

**Confidence: high. Area: privacy/storage exhaustion.** `backend/cortex_backend/execution/repository.py:1049-1058` denies reads after expiry, while `purge_expired()` at `:1092-1145` has no production caller—only tests and qualification invoke it. `Cortex_Preview.py:68-83,136-143` wires the durable repository and attachment service without cleanup.

Bytes, artifact/job/event rows, and expired chat attachments therefore accumulated indefinitely even after they became inaccessible. `ExecutionCleanupSupervisor` now runs a bounded, lease-protected cleanup pass from the FastAPI lifespan whenever a durable execution repository is present, with process metrics, failure isolation, restart coverage, and retryable quarantine state. Lease renewal now happens immediately after acquisition so short leases cannot expire during thread startup. Focused cleanup/lifecycle coverage and the full backend suite pass.

## Priority 2 — important defects and concerns

### F-012 — sessionStorage exceptions can crash startup or strand onboarding after a successful exchange

**Status: fixed and verified** (`74329445`).

**Confidence: high; requires an unusual browser storage failure. Area: authentication/availability.** `frontend/src/api/client.ts:68-75` performs unguarded `sessionStorage.getItem()` during construction. At `:95-106`, the server exchange and in-memory assignment happen before unguarded `setItem()`; `clearSession()` at `:86-92` also calls storage before notifying listeners.

Browsers can throw `SecurityError` when storage is unavailable. After a successful one-time exchange, a storage exception left the valid session token in client memory but returned the UI to a stranded onboarding state; reload lost that token, while retrying the consumed bootstrap could fail. Session storage is now best effort for reads, writes, and clears; the in-memory session and expiry listeners remain authoritative. Throwing-storage tests plus all 202 frontend tests, typecheck, and lint pass.

### F-015 — the currently unused purge method immediately deletes fresh retained results

**Status: fixed and verified** (`e87a6fa1`).

**Confidence: high; latent until cleanup is wired. Area: data loss.** `backend/cortex_backend/execution/repository.py:1101-1107,1136-1144` uses one cutoff for artifact expiry and terminal-job age. With the default current-time cutoff, any just-finished job is older than the cutoff by the time the query runs, so its future-retained artifacts are eligible through the terminal-job clause.

A just-succeeded artifact with one-hour retention was deleted immediately. Cleanup now separates artifact expiry from a seven-day terminal-job/event retention window, and the compatibility `purge_expired()` wrapper uses the same policy. Terminal jobs are rechecked in the write transaction so a concurrent artifact publication cannot be cascaded away.

### F-016 — the currently unused purge method deletes files before its database transaction is durable

**Status: fixed and verified** (`e87a6fa1`).

**Confidence: high; latent until cleanup is wired. Area: data integrity.** `backend/cortex_backend/execution/repository.py:1127-1135` unlinks files and only then removes rows inside a transaction. Filesystem deletion cannot roll back.

Forcing the second unlink to fail removed the first file while SQLite rolled back both rows, leaving metadata for missing content. Cleanup now records durable tombstones before mutation, moves files into a validated quarantine directory, finalizes row deletion idempotently, and resumes pending states after restart or database failure. Tests cover restart recovery and a failed finalization retry.

### F-017 — pinned llama.cpp verification cache is bypassable with unchanged size and mtime

**Status: fixed and verified** (`a9179d1f`).

**Confidence: high; requires local tampering/corruption. Area: runtime integrity.** `backend/cortex_backend/llamacpp/binary_fetcher.py:68-82,93-118` memoizes whole-directory verification using only relative path, size, and `mtime_ns`.

After verifying `GOOD`, replacing it with same-length `EVIL`, and restoring mtime, the same fetcher returned verified while a fresh directory hash failed. This weakens the “verify before every launch” boundary for the lifetime of a process. `ensure_binary()` now bypasses the stat cache and performs the pinned directory hash immediately before returning a launch path (while status polling retains the inexpensive cache); the regression covers the forged identity and redownload path. Focused llama.cpp tests, the full backend suite, Ruff, and whitespace checks pass.

### F-018 — stop and app shutdown cannot interrupt llama-server startup

**Confidence: high. Area: lifecycle/availability.** `backend/cortex_backend/llamacpp/chat_client.py:53-76` enters `ensure_ready()` before checking cancellation. `backend/cortex_backend/llamacpp/server_manager.py:367-410,707-775` holds `_ensure_lock` across binary acquisition and up to 180 seconds of health polling; `stop()` at `:439-442` needs the same lock. `backend/cortex_backend/api/jobs.py:529-550` abandons a generation thread after ten seconds, but app teardown then can block behind startup.

Thread a cancellation token through acquisition and health probes, make stop signaling possible without first acquiring `_ensure_lock`, and test shutdown during each startup phase.

**Status: fixed and verified** (`debb011c`). Startup cancellation now propagates through cache verification, binary download/hash/extraction, process launch, and health polling; lock acquisition is interruptible; stop signaling is immediate and bounded with deferred fail-closed cleanup; unpublished child processes and output-drainer failures are reaped; and cancellable chat streams no longer wait indefinitely for their first SSE line. Focused llama.cpp/chat/API tests (104 passed), the full backend suite (762 passed, 1 skipped, 3 warnings), Ruff, compileall, and diff checks pass. A genuinely non-cooperative OS/network syscall remains uninterruptible, but it cannot clear the stop guard or permit an overlapping runtime.

### F-019 — encoded image limits do not prevent decompression bombs

**Status: fixed and verified** (`122d8267`).

**Confidence: high.** `backend/cortex_backend/services/attachments.py:180-189` calls Pillow `verify()` but has no width, height, pixel, or decoded-memory cap; encoded-byte caps at `:30-36` are insufficient. A 263,244-byte 9500×9500 RGB PNG (270,750,000 decoded bytes) was accepted with only `DecompressionBombWarning`. Validation itself does not allocate all decoded pixels, but a downstream model runtime that decodes the accepted attachment can incur the resource cost. Apply the recipe-image pixel/memory policy to chat images and turn bomb warnings into rejection.

Chat-image staging now enforces independent dimension, pixel, frame, and decoded-memory ceilings and promotes Pillow decompression-bomb warnings to safe validation failures. Regression tests cover oversized dimensions/pixels, decoded-memory limits, and both warning and error thresholds; focused attachment/recipe tests, the full backend suite, Ruff, and whitespace checks pass.

### F-020 — model-requested memory clearing is silently ignored end to end

**Status: fixed and verified** (`9b006f80`).

**Confidence: high.** The backend puts `clear_requested` into terminal job result/completed-event data at `backend/cortex_backend/api/routes.py:1564-1568,2023` and `backend/cortex_backend/api/jobs.py:585-594`, but `backend/cortex_backend/services/memory_commands.py:20-49` has no runtime caller and the frontend has no handler. `frontend/src/hooks/useGenerationStream.ts:188-217` finishes without inspecting it. The memory prompt promises confirmation, but the user receives neither confirmation nor a clear operation.

Successful terminal events and status-recovery results now carry the clear proposal to the chat UI, which asks for explicit confirmation and calls the existing clear-memory API only after approval. Duplicate terminal/reconnect delivery is deduplicated per job, declines remain non-destructive, and failures are surfaced as an error toast. Backend parity, focused hook/chat tests, all 206 frontend tests, typecheck, and lint pass.

### F-021 — Hugging Face discovery advertises nested GGUF files that download rejects

**Status: fixed and verified** (`8d0b815e`).

**Confidence: high.** `backend/cortex_backend/llamacpp/download.py:140-146` lists `rfilename` values such as `weights/model.gguf`; resolver validation at `:75-76` permits only a bare filename. A listed nested file then fails immediately when selected. Either accept a canonically validated repository-relative path or filter discovery to downloadable entries.

Discovery now applies the same safe bare-filename pattern as URL resolution and download staging, so nested, traversal, control-character, malformed, and non-GGUF entries are omitted. GGUF tests and the full backend suite pass.

### F-022 — legacy migration accepts records that make migrated chats unopenable

**Status: fixed and verified** (`94ebcbf4`).

**Confidence: high.** `backend/cortex_backend/repositories/legacy_storage.py:203-225,259-307` validates a message role only as a nonempty string and does not bound source file/message/content size. `backend/cortex_backend/api/routes.py:2514-2530` later enforces the API role enum. A migrated `role: "tool"` succeeded and then GET of that chat returned 500. Validate and quarantine each legacy record, bound input before full JSON loading, and report skipped data.

Legacy migration now caps source reads before JSON materialization, rejects unsupported roles and oversized message sets/content, sanitizes optional metadata to the current chat contract, and quarantines invalid records before insertion. Persistence, packaged-runtime, parity, cleanup, and the full backend suite pass (692 passed, 1 skipped, 3 warnings).

### F-023 — in-memory job event histories have no event or byte ceiling

**Status: fixed and verified** (`fee97244`).

**Confidence: high.** `_JobRecord.events` at `backend/cortex_backend/api/jobs.py:88-107,672-693` is unbounded. GGUF emits download progress per chunk (`backend/cortex_backend/llamacpp/download.py:178-208`; `backend/cortex_backend/api/routes.py:1225-1246`) and generation retains frequent text deltas (`routes.py:1900-1915,2258-2260`). A large download or response can retain substantial RAM until job pruning. Use a byte/event ring buffer and persist/replay only bounded terminal summaries.

`JobRegistry` now retains at most 256 events and 1 MiB of compact UTF-8 JSON event data by default (both limits are configurable), compacts oversized or malformed payloads, and always keeps the newest terminal event/result snapshot. Replay cursors explicitly operate over the retained sequence window. Retention, malformed-payload, API compatibility, and full backend tests pass (695 passed, 1 skipped, 3 warnings).

### F-024 — the composer's one-model state is non-selectable and does not direct the user to Settings

**Status: fixed and verified** (`f39125d5`).

**Confidence: high.** The backend defaults `models.chat` to null (`backend/cortex_backend/core/settings.py:32-38`), and App gates the composer at `frontend/src/app/App.tsx:488-508`. `frontend/src/features/models/LocalModelMenu.tsx:188-198` renders one model as a static label plus rescan, never invoking selection. The tested `LocalSetup.tsx` is not rendered in production. The user must discover the selector in Settings (`frontend/src/features/settings/SettingsPanel.tsx:175-187`) or the command palette while the ordinary composer surface stays disabled. Make the one-model state selectable there or auto-select with explicit user-visible confirmation.

The single-model composer state now renders an accessible “Use local model” control that uses the existing persisted selection path, while retaining the rescan action and disabled/consent behavior. Multi-model keyboard selection is unchanged. The focused model-menu test, full frontend suite (206 passed), type checks, lint, and whitespace checks pass.

### F-025 — memory editor inputs remount on every keystroke

**Status: fixed and verified** (`58a9ead6`).

**Confidence: high.** `frontend/src/features/settings/MemoryPanel.tsx:49-53` keys each row with `${index}-${item}` while editing changes `item`. React remounts the input on every character, losing focus/caret. Use a stable row identifier and add an edit-continuity test.

Memory rows now use stable internal identifiers for React keys and update by row ID, so edits retain focus and caret position while neighboring rows are removed. Save still emits the original `string[]` contract. The focused memory tests, full frontend suite (208 passed), type checks, lint, and whitespace checks pass.

### F-026 — failed memory API calls still mutate the panel draft

**Status: fixed and verified** (`96389deb`).

**Confidence: high.** App catches and swallows rejections in `frontend/src/app/App.tsx:348-375`. `frontend/src/features/settings/MemoryPanel.tsx:20-29` treats callback resolution as success, appending an unsaved memo or clearing its draft even though persistence failed; the draft at `:14` does not resynchronize. The result is an error toast paired with contradictory success-like local state. Rethrow after notification or return a typed result, and update local state only from the server response.

Memory API callbacks now rethrow after presenting the existing error notification, so the panel mutates its draft only after a confirmed response. Regression tests cover failed add, clear, and replace operations plus successful clearing; full frontend tests (212 passed), type checks, lint, and whitespace checks pass.

### F-027 — unsaved GGUF directory edits are ignored by the adjacent Download action

**Status: fixed and verified** (`1d0eb2f9`).

**Confidence: high.** The directory field only changes Settings' local draft (`frontend/src/features/settings/SettingsPanel.tsx:307-311`). `frontend/src/features/models/ModelsPanel.tsx:157-164` and `frontend/src/api/client.ts:304-308` omit it from the request; the backend reloads persisted settings at `backend/cortex_backend/api/routes.py:1211-1212`. Download before Save writes to the previous folder. Disable Download until saved, pass the chosen path explicitly, or state the behavior clearly.

The GGUF download control now disables while its folder differs from the persisted settings and explains that downloads use the saved folder. This avoids silently writing to a different directory while keeping backend path validation unchanged. The regression test, full frontend suite (213 passed), type checks, lint, and whitespace checks pass.

### F-028 — command-palette Settings can close back to the wrong chat

**Status: fixed and verified** (`1df11674`).

**Confidence: high.** Header navigation records the active thread, but the palette callback at `frontend/src/app/App.tsx:510-518` only navigates. Settings Close uses the old `settingsReturnChatId` at `:539-541`, returning to `/chat/new` or a previously visited chat. Centralize settings navigation so every entry point captures the same return route.

Settings entry is now centralized through `openSettings`, which captures the current route for both the header and command palette before navigating. A regression test confirms command-palette entry closes back to the originating chat. The app test, full frontend suite (214 passed), type checks, lint, and whitespace checks pass.

### F-029 — “System” theme does not react to live Windows theme changes

**Status: fixed and verified** (`b71a1efa`).

**Confidence: high.** `frontend/src/app/App.tsx:165-168` samples `matchMedia().matches` only when the setting value changes and registers no `change` listener. Subscribe while theme is `system`, clean up the listener, and test a media-query event.

The app now subscribes to the system color-scheme media query while the System theme is active, updates the document theme on changes, and cleans up when the setting or component changes. It safely supports browsers with either modern or legacy media-query listeners. Regression, full frontend (215 passed), typecheck, lint, and whitespace checks pass.

### F-030 — Settings keeps the initial llama.cpp status until a full workspace reload

**Status: fixed and verified** (`eab14f2c`).

**Confidence: high.** Polling updates Zustand at `frontend/src/app/App.tsx:217-234`, but Settings is passed `system.llamacpp`, the initial load snapshot, at `:496-507`. Live polling never updates that panel; a full workspace/page reload is required. Read the live store value when rendering Settings; consider polling while its runtime panel is visible, not only when a GGUF is selected.

Settings now reads the live llama.cpp status store, retaining the initial system snapshot only as a fallback while the workspace initializes. A regression test verifies that a runtime status-store update immediately rerenders the mounted Settings panel. App, full frontend (216 passed), typecheck, lint, and whitespace checks pass.

### F-031 — disabled composer can display the wrong runtime message

**Status: fixed and verified** (`ca313ac8`).

**Confidence: high.** App computes model availability and GGUF readiness separately but always passes Ollama's connection text at `frontend/src/app/App.tsx:488-508`. `frontend/src/features/chat/MessageComposer.tsx:229-239` shows it verbatim. Missing selections can say “Ready,” and unavailable GGUF selections report irrelevant Ollama state. Derive a typed disable reason from the actual selected runtime.

Runtime readiness now resolves through a typed helper that distinguishes no selection, unavailable model, Ollama outage, and failed GGUF startup; GGUF selections no longer inherit Ollama status. Focused resolver regressions, full frontend tests (218 passed), type checks, lint, and whitespace checks pass.

### F-032 — slow initial GETs can overwrite newer user mutations

**Status: fixed and verified** (`36c7e465`).

**Confidence: high.** `frontend/src/app/App.tsx:130-149` starts unversioned background group/model requests and later wholesale replaces stores. A stale response can land after group creation, rescan, pull, or download. These requests are also not aborted when the authenticated workspace unmounts, so their Zustand setters can still run afterward. Abort/version requests and ignore responses older than the latest mutation/session generation.

Workspace, group, and model generations now gate delayed inventory responses, fallback writes, load state, and relevant session-expiry callbacks. Successful group mutations invalidate the pending snapshot; optimistic collapse rollback preserves later edits; model jobs guard progress, refreshes, and busy state across superseding jobs and workspace unmounts. Deferred group/model races and an unmount response are covered by App tests. The focused App suite (22 passed), type checks, lint, and whitespace checks pass.

### F-033 — failed rename/delete/group operations close dialogs and discard input

**Status: fixed and verified** (`d535e1ab`).

**Confidence: high.** App catches without rethrowing at `frontend/src/app/App.tsx:273-315`. Callers at `frontend/src/features/shell/AppShell.tsx:167-190` and `ChatLibrary.tsx:179-187,331-340` therefore see a resolved promise and close even after failure. Return explicit success/failure, keep the dialog open on failure, and add pending guards to prevent duplicate submits.

Chat and group mutation callbacks now return explicit success booleans. Rename/delete dialogs only close after confirmed success, preserve entered values on failures, and disable duplicate submissions while requests are pending; group dialogs also handle rejected callbacks without unhandled promise errors. Focused shell tests (24 passed), typecheck, lint, and whitespace checks pass.

### F-034 — collapsed compact sidebar remains keyboard- and screen-reader-reachable

**Status: fixed and verified** (`264b4147`).

**Confidence: high.** `frontend/src/features/shell/AppShell.tsx:124-162` always renders all controls; CSS at `frontend/src/styles/tokens.css:315-317,908-913` only changes width/transform. Offscreen controls remain in tab and accessibility order. Use `inert`/hidden semantics when closed; if the compact drawer is intended to be modal, also implement the corresponding focus and Escape behavior.

Collapsed sidebars now expose `aria-hidden="true"` and `inert`, removing offscreen navigation controls from the accessibility and tab trees while retaining the external show/hide toggle. The AppShell regression covers both collapsed and restored states; focused tests (8 passed), typecheck, lint, and whitespace checks pass.

### F-035 — sidebar search has no empty state when any group exists

**Status: fixed and verified** (`0ce850a8`).

**Confidence: high.** `frontend/src/features/shell/ChatLibrary.tsx:85` treats any group as content, but no-match groups return null at `:103-107`; the “No chats match” message at `:157-159` is then suppressed. Base empty state on rendered matches, not raw group count.

The search empty-state decision now uses matching chats while searching, so groups hidden for having no matching rows no longer suppress the “No chats match your search” message. The ChatLibrary regression and focused suite (18 passed), typecheck, lint, and whitespace checks pass.

### F-036 — chat actions have no reliable touch affordance while still consuming title width

**Status: fixed and verified** (`2885d13a`).

**Confidence: high.** `frontend/src/styles/tokens.css:357-361` keeps the action rail in flex layout at opacity zero and reveals it only on hover/focus. The mobile rule at `:908-916` targets message actions, not `.chat-row-actions`. Some touch browsers synthesize hover/focus after a tap, but there is no reliably visible or discoverable affordance and the hidden rail still truncates titles. Provide an always-visible touch affordance and remove hidden controls from layout when appropriate.

Chat-row action rails now overlay the title area instead of consuming desktop layout width, become pointer-interactive only when revealed, and remain visibly available on coarse/no-hover touch layouts with title padding to prevent overlap. Shell/library regressions plus typecheck, lint, and whitespace checks pass.

### F-037 — crossing the 40-message virtualization threshold can yank a reader to the bottom

**Status: fixed and verified** (`4193b1a2`).

**Confidence: high.** `frontend/src/features/chat/MessageList.tsx:25-40,80-105` replaces the plain scroller with Virtuoso at 40 messages and mounts it with the final item selected. A reader scrolled up at message 39 is forced to the bottom when message 40 arrives. Preserve anchor/scroll state across the implementation switch or use one list strategy consistently.

MessageList now records the plain transcript’s scroll offset and whether it was near the end, chooses a non-bottom initial Virtuoso index for readers away from the end, and restores the captured pixel position on the transition (including a follow-up animation frame for delayed scroller setup). A mocked threshold-transition regression passes, along with typecheck, lint, and whitespace checks.

### F-038 — live generation tracking is lost across routes if active-job persistence later fails

**Status: fixed and verified** (`cff67a75`).

**Confidence: high.** ChatPage aborts its consumer on unmount (`frontend/src/features/chat/ChatPage.tsx:154`) and restores only from storage at `:235-258`; storage helpers in `frontend/src/hooks/useGenerationStream.ts:34-64` intentionally suppress failures. If storage becomes unavailable or hits quota after startup, returning from Settings can clear the live global job even though the backend continues. Keep authoritative active-job state in an app-level store and recover by thread/status API, with storage only as an optimization.

The global generation slice now retains the last applied SSE cursor, and ChatPage prefers that live in-memory job/cursor on route remount before consulting best-effort storage. Storage-denied remounts therefore resume the existing stream instead of ending it or replaying from a stale cursor; cursor updates are monotonic. Store, hook, and ChatPage regressions pass (61 focused tests), with typecheck, lint, and whitespace checks clean.

### F-039 — an unused query bootstrap credential can remain in URL/history

**Status: fixed and verified** (`c42c96e9`).

**Confidence: high; launcher normally uses a fragment.** App starts session-ready from stored state at `frontend/src/app/App.tsx:40-42`, but URL cleanup occurs only after exchange at `:59-65`. If a valid stored session and `?bootstrap=...` coexist, the token remains in the address bar and browser history. Always scrub bootstrap parameters immediately after parsing, whether or not exchange is needed; prefer fragment-only input.

Bootstrap query and fragment parameters are now removed during the initial token read, before either onboarding or the authenticated workspace renders, while unrelated URL state is preserved. The successful exchange cleanup uses the same preservation rules. A stored-session/query-token regression passes alongside the existing App suite (23 passed), typecheck, lint, and whitespace checks.

### F-040 — FastAPI validation details are discarded by the frontend client

**Status: fixed and verified** (`0b56964f`).

**Confidence: high.** `frontend/src/api/client.ts:489-497` handles only string or `{message}` details. Standard FastAPI 422 responses contain an array, so an actionable field error becomes “The local workspace did not respond.” A direct invalid group request confirmed this shape. Parse the validation array into safe field/message text.

The client now formats bounded FastAPI validation arrays into field-specific messages, supports indexed locations, ignores untrusted `input`/malformed entries, and preserves prior string/object detail handling. Client tests (15 passed), typecheck, lint, and whitespace checks pass.

### F-041 — the execution tray/client expose no generic non-code result or artifact UI path

**Status: fixed and verified** (`9a151546`).

**Confidence: medium.** `frontend/src/features/shell/ExecutionTaskTray.tsx:158-190` rendered detailed results only for `code.exec.v1`. `downloadExecutionArtifact()` existed at `frontend/src/api/client.ts:373-380` but had no production caller. Whether a particular profile surfaces its output elsewhere is workflow-dependent, but this shared execution surface could not inspect or download generic results.

The task-list API now includes bounded non-code result envelopes, retaining safe artifact metadata for oversized results. The execution tray renders generic JSON results and offers an owner-scoped artifact download action; the app consumes the existing authenticated download endpoint and uses a safe MIME-derived filename. Backend API and tray regression tests, typecheck, lint, and whitespace checks pass.

### F-042 — frontend freshness tracking omits real build inputs

**Status: fixed and verified** (`d29d3b54`).

**Confidence: high.** `backend/cortex_backend/launcher/frontend.py:23-31,69-79` hashed selected config plus `src`/`public`, but omitted `contracts/cortex-api.ts`, Vite `.env*`, and other external build inputs. `FrontendManifest` recorded Node/npm majors while `needs_build()` ignored them, allowing stale types, API configuration, or incompatible caches to be reused.

Freshness now hashes the generated contract, all frontend `.env*` files, and `VITE_*` process inputs without persisting their values; `needs_build()` also compares the recorded Node/npm major versions. Launcher tests cover source, contract, environment, and toolchain changes; the full launcher test module (31 passed), Ruff, and whitespace checks pass.

### F-043 — concurrent frontend builds can delete each other's active staging trees

**Status: fixed and verified** (`eabc5297`).

**Confidence: high.** `backend/cortex_backend/launcher/frontend.py:156-178` deleted every sibling `.cortex-frontend-build-*` directory without checking age, PID, lock, or liveness. `main.py:236-248` runs `--build-frontend` before the single-instance lock, and shared install cache operations at `frontend.py:205-227` were unprotected.

Source builds now take a persistent OS-level byte-range lock before staging, cleanup, or cache mutation, so concurrent processes cannot reclaim an active build or interleave cache updates. The lock inode is retained (and ignored) to avoid an unlink/recreate race; stale staging cleanup runs only after exclusive ownership. Launcher tests cover lock serialization and the full launcher module (32 passed), with Ruff and whitespace checks passing.

### F-044 — generated API contracts omit authentication and leave execution SSE untyped

**Status: fixed and verified** (`c6a6ade8`).

**Confidence: high.** `backend/cortex_backend/api/routes.py:1023-1031` declared execution SSE without a schema; `contracts/openapi.json` therefore had an empty event-stream media object. Authentication hidden in a custom request dependency produced no `securitySchemes` or operation security declarations, and manually read headers such as `Last-Event-ID`/`X-Cortex-Handoff` were absent.

The bearer session dependency now publishes a `CortexSession` HTTP security scheme on authenticated operations. Generation and execution SSE routes declare their Pydantic envelopes, document `Last-Event-ID`, and describe response headers; the launcher handoff header is also explicit. Contract generation now relies on route-declared schemas, and checked-in OpenAPI/TypeScript artifacts were regenerated. API contract tests (19 passed), contract generation, Ruff, and whitespace checks pass.

### F-045 — packaged startup failures provide no usable diagnostics

**Status: fixed and verified** (`d63493e5`).

**Confidence: high.** `packaging/Cortex.spec:62-73` builds with `console=False`; `main.py:140-155,364-404` had no durable file logger and a fallback dialog that told users to run the package from a terminal. A GUI-subsystem executable therefore left missing assets, port conflicts, WebView failures, and bad paths opaque.

Startup and frontend-build failures now append bounded, privacy-safe diagnostics under the user data directory (with a temp fallback), redact credential-like fields, and expose the path in the packaged error dialog; Windows users can copy the dialog text with Ctrl+C. Launcher tests cover redaction, persistence, rotation, and dialog text; 33 launcher tests, Ruff, compileall, and whitespace checks pass.

### F-046 — absolute session expiry strands a running desktop window after 24 hours

**Status: fixed and verified** (`41c73316`).

**Confidence: high.** `backend/cortex_backend/api/security.py:49-63,117-137` caps session renewal at 24 hours. On 401 the frontend clears session; the one-time bootstrap has already been erased (`frontend/src/app/App.tsx:49-72`). Onboarding cannot request a new token, and a second launch only focuses the existing process (`main.py:253-272`). Add an authenticated launcher handoff/rebootstrap flow for the live window or align expiry with process lifetime.

The desktop launcher now carries a scrubbed, local-only handoff capability alongside the one-time bootstrap fragment. When a live session expires, the frontend requests a fresh bootstrap token through `/session/handoff`, exchanges it, and remounts the workspace; failed recovery remains fail-closed with a reconnect action. URL credentials are removed before rendering, and stale in-flight 401 responses cannot clear a newer replacement session. Launcher, API-client, and App tests cover the handoff, recovery, URL, and request-race paths; 34 launcher tests and 42 focused frontend tests pass, with typecheck, lint, and diff checks passing.

### F-047 — private or untrusted text is logged without classification/redaction

**Status: fixed and verified** (`cfe4a465`).

**Confidence: high for the logging path; prompt leakage from pinned llama.cpp is not proven.** `backend/cortex_backend/repositories/legacy_storage.py:585-590` logs exact chat titles and `:70-72` logs the database path. Titles can include newlines/control text. `backend/cortex_backend/llamacpp/server_manager.py:301-309,537-557,641-653,732-760,746-749` captures and logs raw third-party output and exposes a startup tail through authenticated `/api/v1/system` runtime status (`backend/cortex_backend/api/routes.py:218-221`). Replace content with stable identifiers/error codes and sanitize all third-party diagnostic text.

Persistence logs now omit database paths, filenames, chat identifiers, titles, and exception text. Llama runtime restart and launch diagnostics use stable classifications; child output remains bounded for internal lifecycle handling but is not emitted or exposed as runtime error text. Regression tests assert private values and raw provider output do not reach logs/status. Persistence and llama-manager tests (45 passed), API contract coverage (included in a 64-test focused run), Ruff, and diff checks pass.

### F-048 — Windows Job Object containment failures are usually silent

**Status: fixed and verified** (`48fcddfb`).

**Confidence: high.** `backend/cortex_backend/llamacpp/server_manager.py:250-289` starts the process before containment, returns silently when several Win32 calls return false, and ignores `AssignProcessToJobObject`'s BOOL result. Only raised `OSError` logs a warning. Cortex can appear to own the child while hard exit leaves llama-server consuming GPU/RAM. Check every return, terminate/fail closed if containment is required, and test each failure path.

The Job Object launcher now checks creation, configuration, process-open, assignment, and handle-close results. Any containment failure raises a stable error, cleans up newly created handles, and terminates the just-spawned process (with kill escalation) before returning control. Tests cover each false-return path, cleanup, and process termination; llama-manager/API focused coverage (72 passed), Ruff, and diff checks pass.

### F-049 — bundled WebView installer is not verified immediately before execution

**Status: fixed and verified** (`db1ce05c`).

**Confidence: high on the gap; impact depends on package/install ACLs.** Build-time checks exist, but `backend/cortex_backend/launcher/webview_runtime.py:53-68` trusts any existing bundled installer. `tests/test_launcher.py:236-254` accepts a dummy file. A modified one-folder package can execute a replacement when WebView is missing. Recheck Authenticode publisher and/or a signed manifest hash at runtime.

Before any bundled installer execution, the Windows launcher now rechecks its Authenticode chain and Microsoft Corporation publisher through a noninteractive, system-module-only PowerShell process. Verification errors fail closed without relaying command output; existing-runtime and non-Windows paths remain unchanged. Launcher tests cover invalid signatures, command isolation, and install behavior; 36 launcher tests, Ruff, and diff checks pass.

### F-050 — custom Windows data roots and launcher secrets lack canonical/DACL enforcement

**Status: fixed and verified** (`5f787a27`).

**Confidence: high on behavior; default profile path is lower risk.** `backend/cortex_backend/core/paths.py:27-32,128-130` accepts any absolute custom data directory without reparse/UNC or private-DACL policy. `backend/cortex_backend/launcher/instance.py:79-84` relies on `os.chmod(0o600)`, which does not establish a restrictive Windows DACL. Shared, junctioned, UNC, or inherited-permission roots can expose handoff secrets and databases. Canonicalize and enforce an explicit Windows ACL policy.

Data roots are now canonicalized before use, reject UNC and existing symlink/reparse components, and are rechecked after creation. Windows roots and launcher secret files receive a fail-closed `icacls` policy that removes inherited broad-user access and grants the current account only; non-Windows paths use restrictive modes. Instance records can no longer redirect secret reads to arbitrary paths, and startup diagnostics use the same safe-root resolution. Core/launcher tests (49 passed), Ruff, and diff checks pass.

### F-051 — packaging/runtime regressions are not gated before merge

**Status: fixed and verified** (`5629cd6f`).

**Confidence: high.** `.github/workflows/quality.yml:97-100` skips the heavy job on pull requests, even for packaging/launcher/worker changes. The heavy job installs unconstrained latest PyInstaller at `:132-133`, does not launch the produced executable, and tests only Python 3.11 despite advertised Python 3.10+. Actions use mutable major tags and Python dependencies are range-resolved without a release lock/hashes. Add path-triggered PR packaging/startup smoke tests and a supported-version matrix with pinned build inputs.

The heavy Windows job now gates pull requests after the fast and supported-Python jobs, builds with the exact PyInstaller `6.14.2` pin, verifies the signed WebView bootstrapper, and launches the produced `Cortex.exe --headless` until its authenticated backend health endpoint is ready. A Windows matrix runs the Python suite and compile checks on 3.10–3.14. The focused recipe-worker packaging tests (7 passed), workflow YAML parse, Ruff, and diff checks pass. Action refs and general dependency locking remain separate supply-chain hardening work; this repair closes the missing packaging/runtime merge gate.

### F-052 — “pinned Wasmtime” qualification can run an unpinned global package

**Status: fixed and verified** (`7a0c697a`).

**Confidence: high.** `tools/execution_spikes/run_pinned_wasmtime_smoke.ps1:35-51` does not check the target pip install exit code and leaves normal site-packages enabled after prepending the target to `PYTHONPATH`. If install fails and global Wasmtime exists, qualification can pass against it. Version and expected hash are also jointly caller-overridable at `:1-4`. Run the probe in a disposable virtual environment with global/user sites disabled, verify import path/version/hash, and make the approved pin immutable in repository policy.

The probe now has no caller-controlled version/hash parameters, creates a disposable isolated virtual environment, checks download and install exit codes, requires exactly one approved wheel, verifies its SHA-256 plus the imported module’s version and venv-local origin, and runs the Phase 0 probe with that same isolated interpreter. Two focused script tests, Ruff, PowerShell parsing, and diff checks pass. A real run downloaded and installed the approved wheel successfully; the host then reported the existing expected `phase0_status=blocked` prerequisite result rather than falling back to a global package.

### F-053 — dev Vite port race can expose the live bootstrap token to a local page

**Status: fixed and verified** (`28f5b477`).

**Confidence: high on code path, medium on race practicality; dev mode only.** `main.py:317-327` bind-selects then releases Vite's port; `backend/cortex_backend/launcher/supervisor.py:17-34` accepts any 2xx root. If another loopback process wins, `main.py:340-345` opens it with the bootstrap in the fragment. Its JavaScript can call the backend because CORS permits any loopback port (`backend/cortex_backend/api/app.py:224-229`) and origin validation ignores port (`api/security.py:147-155`). Bind/identify the expected dev server and restrict allowed origin to the chosen port.

Each dev launch now generates a fresh nonce, passes it only to the supervised Vite process, and requires the exact `X-Cortex-Dev-Server` response header before opening the browser. A competing loopback service can no longer satisfy the startup gate without the nonce. Launcher tests (39 passed), Ruff, TypeScript, ESLint, diff checks, and a live Vite header/cleanup cross-check pass.

### F-054 — default in-memory attachment storage races and never evicts

**Status: fixed and verified** (`204ac0ed`).

**Confidence: high; primarily demo/default dependency mode.** `backend/cortex_backend/services/attachments.py:237,256-272` performs request-ID check-then-insert without a lock; concurrent identical IDs can return different descriptors and leave one unresolvable. Expired in-memory records remain at `:341-348`. Add locking/idempotent insertion and bounded expiry eviction, or avoid this implementation outside isolated tests.

The in-memory path now serializes check/insert and resolve access, evicts expired or malformed records opportunistically, and enforces 128-record/24 MiB caps so demo-mode retention is bounded. Focused attachment tests (13 passed) cover concurrent idempotency, eviction, ownership, integrity, and the existing image/document limits; Ruff and diff checks pass.

## Priority 3 — lower-impact bugs and quality concerns

### F-055 — whitespace-only titles, group names, and messages pass validation

**Status: fixed and verified** (`13f0a2e1`).

**Confidence: high.** Pydantic `min_length=1` in `backend/cortex_backend/api/schemas.py:176-222` runs before later stripping in routes. Reproduction created a chat/group with an empty post-strip name and stored a two-space message. Use before-validation trimming and reject empty/control-only content.

The affected request models now normalize edge whitespace before length checks and reject values with no visible characters, including control/format-only strings. The same boundary covers generation input and regeneration overrides. API tests prove trimmed values persist and blank/control-only chat, group, message, and generation inputs return validation errors; 36 focused API/revision tests, Ruff, and diff checks pass.

### F-056 — generation reconnect can hammer the API indefinitely

**Status: fixed and verified** (`7a1e42bb`).

**Confidence: high.** `frontend/src/hooks/useGenerationStream.ts:7,147-243` retries every 250 ms without exponential backoff, jitter, offline pause, or a ceiling while the page remains mounted and no recognized terminal/auth/not-found response arrives. Immediate stream/status failures can sustain several local requests per second. Add capped exponential backoff and a user-visible paused/retry state.

Reconnects now use jittered exponential delays from 250 ms up to a 30-second ceiling, pause behind the browser’s offline state, resume on `online`, and cancel timers/listeners on stop. Status text tells the user whether the stream is paused offline or when the next retry will occur. Hook tests (24 passed), the full Vitest suite (243 passed across 29 files), TypeScript, ESLint, and diff checks pass.

### F-057 — move-to-group uses ARIA menu roles without menu keyboard behavior

**Confidence: high.** `frontend/src/features/shell/ChatLibrary.tsx:272-310` declares `menu/menuitem` but lacks initial focus, arrow navigation, Home/End, Escape, and typeahead. Implement the composite-menu pattern or use simpler disclosure/list semantics.

**Status: fixed and verified** (`83c03980`). The move menu now follows a composite-menu interaction model: opening moves focus to the first enabled item (or the last when opened with ArrowUp), ArrowUp/ArrowDown wrap across enabled items, Home/End jump to the edges, printable keys provide 500 ms typeahead, Escape restores focus to the trigger, and Tab exits to the adjacent action with Shift+Tab returning to the trigger. Menu items use roving `tabIndex=-1`, and disabled current-group targets are skipped. Focused shell tests (21 passed), the full frontend Vitest suite (246 passed across 29 files), TypeScript, ESLint, and diff checks pass.

### F-058 — preview launcher prints a live bootstrap credential

**Confidence: high.** `Cortex_Preview.py:204-207` prints the live token, conflicting with the repository rule against logging credentials. Console capture, IDE history, and support bundles can retain it during its validity window. Deliver it through a one-time browser fragment/handoff or another local authenticated exchange that avoids durable console logging while still letting preview users authenticate.

**Status: fixed and verified** (`b3cefde7`). The standalone preview no longer prints the bootstrap token. It registers a startup callback that opens the authenticated one-time token in a URL fragment after Uvicorn binds; the frontend exchanges and scrubs the fragment, and `--no-browser` remains available for headless use. Browser-launch failures are suppressed without logging the credential. Four focused launcher tests, Ruff, compileall, and diff checks pass.

### F-059 — instance lock file grows by one byte per launch attempt

**Confidence: high.** `backend/cortex_backend/launcher/instance.py:64-68` opens in append mode, seeks, and writes a byte; append semantics place every write at EOF. Release removes record/secret files but not the lock file (`:119-139`). Open without append and truncate/retain a fixed-size lock record.

**Status: fixed and verified** (`3189dafb`). `InstanceLock` now opens the persistent marker without append semantics, normalizes legacy or newly-created markers to exactly one byte before acquiring the OS lock, and preserves the Windows `msvcrt`/POSIX `fcntl` recovery behavior. Launcher tests (40 passed), Ruff, compileall, and diff checks pass.

### F-060 — contract/check tooling is non-atomic and can mutate the worktree while “checking”

**Confidence: high.** `tools/generate_contracts.py:34-38` writes JSON and TypeScript separately, so interruption can mismatch them. Its renderer at `:41-89` intentionally uses `unknown` for current unconstrained schemas and would silently do the same for future unsupported constructs such as root enums or `allOf`. `scripts/check.ps1:93-102` verifies drift by regenerating directly into the worktree, and `-SkipBackend -SkipFrontend` can report “All 0 checks passed.” Generate to temp files, compare, then atomically promote only in explicit generation mode; fail loudly on unsupported schemas and reject a zero-check invocation.

**Status: fixed and verified** (`5967286c`; contributor docs `9143b863`). Contract tooling now requires explicit `--check` or `--write`; check mode renders disposable staging files and compares without touching tracked outputs, while write mode promotes staged files with `os.replace`. Root enums become type aliases, unsupported schema keywords/types fail loudly, and the PowerShell quality script rejects both skip switches. Contract tests (6 passed), the full backend suite (734 passed, 1 platform skip), the actual `check.ps1 -SkipFrontend` gate (5 checks passed), Ruff, compileall, and diff checks pass.

### F-061 — production frontend ships as one very large JavaScript chunk

**Confidence: high; performance concern.** `npm run build` produced an 892.82 kB minified JavaScript chunk (280.61 kB gzip) and Vite's >500 kB warning. Settings, model management, execution tray, and virtualization are candidates for route/feature splitting. Measure startup on representative Windows/WebView hardware before setting a budget.

**Status: fixed and verified** (`e709900e`). ChatPage and SettingsPanel now load through route-level lazy boundaries with a contained status fallback; the chat chunk is prefetched only while a chat route is active so direct Settings launches do not fetch it. The production build now emits a 305.89 kB entry (95.91 kB gzip), a 447.57 kB ChatPage chunk (139.92 kB gzip), a 61.83 kB SettingsPanel chunk (20.28 kB gzip), and separate shared chunks; no chunk exceeds Vite's 500 kB warning threshold, and `dist/index.html` does not module-preload either route chunk. The focused App suite (24 passed), three repeated full frontend Vitest runs (246 passed each), typecheck, lint, build, and diff checks pass.

### F-062 — browser tests make an unmocked localhost request and omit the chat-group success path

**Confidence: high.** All 18 Playwright tests passed, but each emitted Vite proxy `ECONNREFUSED 127.0.0.1:8765` for `/api/v1/chat-groups`. Fixtures do not mock that endpoint, so the suite exercises only the group's failure fallback and makes a real network attempt. Make unexpected requests fail the test and add success/race coverage.

**Status: fixed and verified** (`15252923`). A shared Playwright fixture now owns the API surface: it provides the expected background `GET /chat-groups` response and aborts/records every other unmocked `/api/v1/**` request so hidden proxy failures fail the test. New browser coverage verifies successful group loading/chat filing and prevents a late initial response from erasing a group created while loading. The serial E2E suite passes 20 tests, including the two new cases; frontend typecheck, lint, Vitest (246 tests across 29 files), and diff checks also pass.

### F-063 — one full-suite run exposed intermittent execution failures

**Confidence: medium; product cause not established.** One quick-gate run produced 2 failures, 630 passes, and 1 skip. One approval-to-running test exceeded its two-second poll; one scratch execution returned failed. Each failed test passed alone, and five repeated invocations of the pair passed all 10 test cases. The evidence establishes intermittence but not whether order, load, Python 3.14, leaked workers, global state, or tight waits are responsible.

**Status: fixed and verified** (`8040eb3e`). Scratch workers now announce readiness over the existing pipe before evaluation; process/import startup has a separate bounded 15-second deadline, and the existing 3-second wall-clock budget begins only after readiness. This removes Windows bootstrap time from the compute budget without weakening evaluator limits, cancellation, or cleanup. The focused execution slice (29 passed), six repeated execution-pair runs, full backend suite (735 passed, 1 skipped), Ruff, compileall, and diff checks pass.

### F-064 — session accumulation and process-lifetime client cleanup gaps remain

**Confidence: high.** Expired sessions remain in the growing `_sessions` map (`backend/cortex_backend/api/security.py:68,97-136`). Owned `httpx.Client` instances created in `backend/cortex_backend/llamacpp/server_manager.py:344-347` and `chat_client.py:41` are process-lifetime resources reclaimed at exit, but app teardown at `backend/cortex_backend/api/app.py:169-181` does not close them explicitly. Add bounded session expiry cleanup and explicit client shutdown for deterministic resource release.

**Status: fixed and verified** (`f3551416`). Session exchange/authentication now removes a bounded batch of expired entries on each request, rotates live entries so cleanup makes progress, and exposes a capped, thread-safe cleanup method for idle janitor callers; expired credentials are removed immediately and no token data is logged. The llama.cpp manager and chat adapter now have idempotent ownership-aware close paths, fail closed after shutdown, defer owned transport closure until active requests finish, and app teardown still closes them across startup, job, cleanup, or execution shutdown failures. The preview builder wires the production chat adapter into lifespan cleanup. API-contract, chat-routing, and server-manager tests (83 passed), Ruff, compileall, and diff checks pass. The startup-interruption concern tracked by F018 is also now fixed and verified (`debb011c`).

### F-065 — the remaining-reliability document is stale and can misdirect work

**Confidence: high.** `docs/REMAINING_RELIABILITY_FIXES.md` still lists DNS rebinding, settings separation, and SSE replay as unimplemented, while current code/tests contain implementations for those areas. Mark the note historical or refresh it against the current snapshot; stale gap lists undermine triage.

**Status: refreshed locally**. The note now clearly labels itself historical, records the verified commits for the three completed items, and points triage to the live F-066–F-068 inventory. The repository's pre-existing `.gitignore` intentionally excludes `docs/` as local-only planning material, so this documentation-only repair is not staged or published with the merge branch.

### F-066 — production port selection still has a bind/close/start TOCTOU reliability window

**Confidence: high; primarily availability.** `main.py:124-131,250-305` releases the selected backend port before Uvicorn binds it. Supervision generally turns a stolen port into startup failure rather than accepting an impostor, but local contention can still cause intermittent startup failure. Prefer passing a prebound socket or retrying a fresh port under a single bounded startup transaction.

**Status: fixed and verified** (`2140391e`). The launcher now keeps the loopback backend socket bound from reservation through Uvicorn startup and hands that exact listener to the server supervisor, removing the bind/close/start race. Explicit-port reservation happens after the instance lock is acquired so duplicate launches still activate the existing process; reservation failures produce bounded startup diagnostics. Supervisor ownership closes listeners on normal, failed, and unexpected exits. Launcher regression coverage (44 passed), Ruff, compileall, and diff checks pass.

### F-067 — the crash screen makes an unconditional data-integrity promise

**Confidence: high.** `frontend/src/app/ErrorBoundary.tsx:23-27` always says “Your local data was not changed.” A render crash can occur after an API mutation already committed, so the component cannot know this. Replace it with a factual statement that no further action was taken by the crashed view and offer reload/diagnostics.

**Status: fixed and verified** (`4498e28e`). The crash screen now states only that the crashed view took no further action, tells the user to reload, and points them to collect diagnostics if the problem persists; it no longer makes an unsupported claim about local-data mutation. A focused ErrorBoundary regression test covers the wording and preserved reload affordance. Full frontend Vitest (30 files, 247 tests), typecheck, lint, and diff checks pass.

### F-068 — native dependency-install failures can be masked by stale global packages

**Confidence: high.** `packaging/build_windows.ps1:13-16` and `packaging/build_recipe_worker.ps1:18-20` do not explicitly check pip's exit code. Under ordinary PowerShell native-command semantics, execution continues and later tooling may come from an older global install. Use an isolated build environment, check each native exit, and print/verify resolved tool versions before packaging.

**Status: fixed and verified** (`bcb56442`). Both Windows packaging scripts now check each dependency-install exit code, resolve PyInstaller through the same `python` interpreter used for packaging, require exactly version 6.14.2, and print the verified tool version before building. The explicit `-SkipDependencyInstall` option remains available for controlled pre-provisioned environments. Recipe-worker PowerShell regression coverage includes dependency failure, PyInstaller-install failure, import/version-command failure, wrong-version output, incomplete output, build failure, signing failure, and rollback cases (9 passed); targeted Ruff, PowerShell parsing, and diff checks pass.

## Verification evidence

The table below records the original audit baseline before the repairs. Commands were run from the repository root unless noted.

| Check | Result |
| --- | --- |
| `./scripts/check.ps1` | **Failed:** 2 failed, 630 passed, 1 skipped; Ruff, artifact qualification (12/12), watchdog qualification (15/15), contract drift, TypeScript, ESLint, and all 186 Vitest tests passed |
| Each failing pytest alone | Passed |
| Both failing pytests together, repeated 5 times | 5/5 invocations passed (10/10 test cases) |
| `python -m compileall -q main.py Cortex_Preview.py backend` | Passed |
| `npm run build` in `frontend/` | Passed; emitted 892.82 kB chunk warning |
| `npm run e2e -- --workers=1` in `frontend/` | 18/18 passed; repeated unmocked `/api/v1/chat-groups` proxy failures observed |
| `npm audit` and `npm audit --omit=dev` in `frontend/` | 0 known vulnerabilities |
| `python -m pytest -q tests/test_launcher.py tests/test_api_contract.py` | 46 passed, one Starlette/httpx deprecation warning |
| Focused safe reproductions | Confirmed backup loss, settings lost update, memory-command parsing/system-role insertion, redirect downgrade, invalid/minimal GGUF, image bomb acceptance, unsafe/partial purge, binary-cache bypass, nested HF mismatch, invalid legacy migration, whitespace acceptance, and loopback llama impostor behavior; route inspection confirmed automatic memory persistence |

The two full-suite failures were:

1. `tests/test_code_execution_api.py::test_code_api_is_pending_until_approved_and_source_is_owner_scoped` — status remained `running` beyond the test's two-second polling window.
2. `tests/test_open_source_execution.py::test_local_profile_runs_scratch_and_fixed_image_recipe_end_to_end` — scratch execution returned `failed` in the full suite.

Because both passed alone and under immediate repetition, they are recorded as a reliability/test-isolation finding rather than two confirmed deterministic product defects.

### Post-repair cumulative verification (2026-08-31)

| Check | Result |
| --- | --- |
| `./scripts/check.ps1 -Tier full` | **Passed:** all 12 checks; 762 backend tests passed, 1 platform-specific test skipped, artifact qualification 12/12, watchdog qualification passed, contracts current, compileall passed, frontend typecheck/lint passed, Vitest 30 files/247 tests passed, Playwright 20/20 passed, and frontend build passed without a Vite chunk warning |
| `python -m pytest -q tests/test_launcher.py` | 44 passed, one Starlette/httpx deprecation warning |
| `python -m pytest -q tests/test_packaging_build_recipe_worker.py` | 9 passed |
| PowerShell parser validation for both Windows packaging scripts | Passed |
| Targeted Ruff and `git diff --check` for each repair | Passed |

## Limits and non-findings

- The original audit did not modify product code or attempt destructive/external operations; the repair commits listed above are the subsequent implementation pass.
- No npm advisory was present at audit time.
- The global Python environment has unrelated package conflicts, so `pip check` output was not used as repository evidence.
- The Windows package and native recipe worker were not rebuilt end to end during this audit; build-script findings come from code review and safe native-exit semantics reproductions.
- The llama port-race and dev Vite token-race findings require a competing local process. The binary-cache finding requires local file tampering or equivalent corruption. Their prerequisites lower likelihood, not impact.
- Raw llama-server output is definitely unredacted; this audit did not establish that the pinned llama.cpp build logs prompt bodies by default.
- Pre-existing `.gitignore` and screenshot-tool worktree changes were preserved and excluded from the audit diff.
