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

**Confidence: high. Area: privacy/integrity.** `backend/cortex_backend/llamacpp/server_manager.py:295-298,519-524,721-736,777-782` chooses a free port with bind-then-close, starts the child later, and accepts any HTTP 200 from `/health`. `backend/cortex_backend/llamacpp/chat_client.py:72-85,110-116` then trusts that origin for full chat bodies.

A safe loopback reproduction held the selected port with an unrelated HTTP server while a disposable fake child remained alive. Cortex entered `ready`, posted `PRIVATE_PROMPT` to the unrelated service, and accepted its response. Exploitation requires another local process to win a narrow bind race, but the consequence is disclosure and substitution of model output. Reserve/pass an already-bound socket where supported, and make readiness attest the expected child/protocol instance rather than only status 200.

### F-002 — recipe-worker build can sign and report a stale executable after a failed build

**Confidence: high. Area: build/supply chain.** `packaging/build_recipe_worker.ps1:23-30,66-76` suppresses cleanup errors, does not check PyInstaller's native exit code, and only verifies that an executable already exists. On this host, PowerShell's `$PSNativeCommandUseErrorActionPreference` is false, so `$ErrorActionPreference = 'Stop'` does not make a nonzero native exit terminate the script.

If a stale output is locked, cleanup can leave it behind; PyInstaller can then fail; the script can still announce success and sign the old tree. Build into a fresh unique directory, check every native exit code, validate build identity/version, and atomically promote only the new verified output.

### F-003 — opening a corrupt settings database overwrites its valid backup before validation

**Confidence: high. Area: data loss.** `backend/cortex_backend/repositories/sqlite_settings.py:53-61,203-210` calls `_create_backup()` before opening or validating the primary database and blindly copies primary over `.bak`.

Reproduction: create a valid backup, replace the primary with `b"corrupt-primary"`, then construct the repository. Initialization fails and the backup becomes the corrupt bytes too. Validate/open the primary before rotating recovery copies, use multiple generations, and test corrupt-primary/valid-backup startup.

### F-004 — memory recovery can lose both primary and backup on the next failed save

**Confidence: high. Area: data loss.** `backend/cortex_backend/repositories/legacy_storage.py:937-952` can recover from a good backup, but `_save_memos()` at `:972-974` first copies the still-corrupt primary over that backup before replacing primary.

Reproduction recovered `['first']` from backup, forced `os.replace` to fail during the next add, and left both files containing `{corrupt}`; a fresh manager loaded no memories. `tests/test_persistence.py:140-150` verifies fallback only, not the subsequent failure window. Once recovery occurs, first restore a validated primary atomically; never rotate an invalid source over the last good copy.

### F-005 — concurrent settings writes fail or silently lose updates

**Confidence: high. Area: data integrity/concurrency.** `backend/cortex_backend/api/routes.py:525-537` performs load → increment revision → unconditional save. `backend/cortex_backend/repositories/sqlite_settings.py:362-385` has no compare-and-swap or lock spanning that operation, and backup I/O is also unlocked.

Two stale snapshots both saved revision 1, with the second reverting the first change. A forced concurrent pair of API PUTs produced one repository-backed 500 and one 200; only one change survived. The reproduction did not isolate which of the unlocked repository/backup operations raised the 500. Require an expected revision, commit updates transactionally, return 409 on conflicts, and add concurrent API/repository tests.

### F-006 — model-produced memory can become persistent system-role prompt injection

**Confidence: high. Area: model trust boundary.** `backend/cortex_backend/services/llm.py:1245-1250,1331-1371` accepts arbitrary short model-produced additions, `backend/cortex_backend/api/routes.py:1975-1981` auto-persists them, and `backend/cortex_backend/services/llm.py:367-384` interpolates them verbatim into a later system message.

A focused reproduction confirmed that the parser accepted `Ignore all prior instructions...` and prompt construction inserted it verbatim with `role="system"`; route inspection confirms that accepted additions are automatically persisted. Memory being enabled is contextual consent to remember facts, not authority for model output to write future instructions. Bind memories to explicit user-originated facts, separate/delimit them as untrusted data, and require confirmation for model-proposed additions.

### F-007 — GGUF redirects can downgrade HTTPS and reach arbitrary/private HTTP targets

**Confidence: high. Area: network/model integrity.** `backend/cortex_backend/llamacpp/download.py:65-92` validates only the initial URL; `:178` enables automatic redirects without validating every hop and the final URL.

An HTTPS origin redirecting to HTTP was accepted and its body saved. This enables transport downgrade and, for a user-supplied URL or hostile upstream, an unauthenticated GET to a loopback/private target; the reproduction did not demonstrate disclosure of a private response back to the remote origin. Validate every redirect manually, reject HTTPS→HTTP, define and enforce a host/IP policy on the initial URL and every redirect after DNS resolution, and cap redirect count.

### F-008 — GGUF download is unbounded, minimally validated, and overwrites an existing model

**Confidence: high. Area: data loss/resource exhaustion.** `backend/cortex_backend/llamacpp/download.py:180-214` has no total-byte/free-space ceiling, accepts any body beginning with `GGUF`, and unconditionally replaces an existing same-name destination.

`b"GGUF-not-a-real-model"` was accepted. A chunked source can fill disk, while malformed content can replace a working local model and fail only at launch. Enforce configured and advertised sizes, reserve disk headroom, structurally validate GGUF metadata, download to a unique staging file, and require an explicit overwrite policy with rollback.

### F-009 — inherited Vite configuration can bake a remote API endpoint into a production package

**Confidence: high. Area: local-first privacy boundary.** `frontend/src/api/client.ts:68-75,95-105` accepts `VITE_API_BASE_URL` without restricting it to relative/loopback URLs. `backend/cortex_backend/launcher/frontend.py:145-153,248-251` inherits the build environment, and `packaging/build_windows.ps1:18-24` packages the resulting assets without inspecting the baked target.

An accidental `VITE_API_BASE_URL=https://...` environment value or `.env.production` can disclose the bootstrap token, chats, settings, and model requests to a remote endpoint that permits the app origin through CORS. Production builds should reject non-relative/non-loopback targets and record the resolved API target in a verified manifest.

### F-010 — “Retry last message” creates a new idempotency key

**Confidence: high. Area: duplicate work/data.** `frontend/src/features/chat/ChatPage.tsx:277-287` creates a new request ID on every `startGeneration()` call; Retry at `:441-445` invokes it anew.

If the original POST was admitted but its response was lost, retry creates another backend job and, from `/chat/new`, can create a second thread. Retain and reuse the admission key for ambiguous failures until status/replay proves the original request was not accepted.

### F-011 — model-job SSE EOF and cancellation are reported as success

**Confidence: high. Area: workflow correctness.** `frontend/src/api/client.ts:246-260` treats any stream EOF as normal. `frontend/src/app/App.tsx:385-422` treats a return without `kind:"error"` as successful, refreshes inventory, and can display “Model operation completed”; it does not treat terminal `state: cancelled` as failure/cancellation.

A transient disconnect can clear busy state and announce success for pull/rescan while backend work continues or has incomplete state. The GGUF-download caller suppresses that generic success toast, but then reports a generic failure because terminal completion data is absent even though work may continue. Require a terminal event, recover authoritative job status after EOF, and distinguish succeeded, failed, and cancelled.

### F-013 — Settings keeps a stale whole-document draft that can revert newer updates

**Confidence: high. Area: data integrity.** `frontend/src/features/settings/SettingsPanel.tsx:66,102-112` initializes draft state once and later submits the entire document. Updates made while Settings remains mounted—such as GGUF auto-selection or command-palette model/theme changes in `frontend/src/app/App.tsx:435-480,498-518`—do not resynchronize that draft.

Reproduction: open Settings with no chat model, download and auto-select a GGUF, then Save; the stale draft submits `chat: null` and reverts selection. Combine field-level patches with backend revision checks, or explicitly merge/resynchronize and surface conflicts.

### F-014 — expired execution artifacts and chat attachments are never purged in production

**Confidence: high. Area: privacy/storage exhaustion.** `backend/cortex_backend/execution/repository.py:1049-1058` denies reads after expiry, while `purge_expired()` at `:1092-1145` has no production caller—only tests and qualification invoke it. `Cortex_Preview.py:68-83,136-143` wires the durable repository and attachment service without cleanup.

Bytes, artifact/job/event rows, and expired chat attachments therefore accumulate indefinitely even after they become inaccessible. Add a supervised, bounded cleanup lifecycle with metrics and restart-safe tests.

## Priority 2 — important defects and concerns

### F-012 — sessionStorage exceptions can crash startup or strand onboarding after a successful exchange

**Confidence: high; requires an unusual browser storage failure. Area: authentication/availability.** `frontend/src/api/client.ts:68-75` performs unguarded `sessionStorage.getItem()` during construction. At `:95-106`, the server exchange and in-memory assignment happen before unguarded `setItem()`; `clearSession()` at `:86-92` also calls storage before notifying listeners.

Browsers can throw `SecurityError` when storage is unavailable. After a successful one-time exchange, a storage exception leaves the valid session token in client memory but returns the UI to a stranded onboarding state; reload loses that token, while retrying the consumed bootstrap may fail. Treat storage as best effort, preserve the in-memory session, and test throwing storage implementations.

### F-015 — the currently unused purge method immediately deletes fresh retained results

**Confidence: high; latent until cleanup is wired. Area: data loss.** `backend/cortex_backend/execution/repository.py:1101-1107,1136-1144` uses one cutoff for artifact expiry and terminal-job age. With the default current-time cutoff, any just-finished job is older than the cutoff by the time the query runs, so its future-retained artifacts are eligible through the terminal-job clause.

A just-succeeded artifact with one-hour retention was deleted immediately. Separate artifact expiry from job/event retention, and never cascade-delete a job while a retained artifact still references it.

### F-016 — the currently unused purge method deletes files before its database transaction is durable

**Confidence: high; latent until cleanup is wired. Area: data integrity.** `backend/cortex_backend/execution/repository.py:1127-1135` unlinks files and only then removes rows inside a transaction. Filesystem deletion cannot roll back.

Forcing the second unlink to fail removed the first file while SQLite rolled back both rows, leaving metadata for missing content. Use a recoverable tombstone/staging protocol: mark rows, commit, move files to quarantine, then finalize deletion idempotently.

### F-017 — pinned llama.cpp verification cache is bypassable with unchanged size and mtime

**Confidence: high; requires local tampering/corruption. Area: runtime integrity.** `backend/cortex_backend/llamacpp/binary_fetcher.py:68-82,93-118` memoizes whole-directory verification using only relative path, size, and `mtime_ns`.

After verifying `GOOD`, replacing it with same-length `EVIL`, and restoring mtime, the same fetcher returned verified while a fresh directory hash failed. This weakens the “verify before every launch” boundary for the lifetime of a process. Rehash at launch or use a file identity/change mechanism that cannot be restored by ordinary metadata writes.

### F-018 — stop and app shutdown cannot interrupt llama-server startup

**Confidence: high. Area: lifecycle/availability.** `backend/cortex_backend/llamacpp/chat_client.py:53-76` enters `ensure_ready()` before checking cancellation. `backend/cortex_backend/llamacpp/server_manager.py:367-410,707-775` holds `_ensure_lock` across binary acquisition and up to 180 seconds of health polling; `stop()` at `:439-442` needs the same lock. `backend/cortex_backend/api/jobs.py:529-550` abandons a generation thread after ten seconds, but app teardown then can block behind startup.

Thread a cancellation token through acquisition and health probes, make stop signaling possible without first acquiring `_ensure_lock`, and test shutdown during each startup phase.

### F-019 — encoded image limits do not prevent decompression bombs

**Confidence: high.** `backend/cortex_backend/services/attachments.py:180-189` calls Pillow `verify()` but has no width, height, pixel, or decoded-memory cap; encoded-byte caps at `:30-36` are insufficient. A 263,244-byte 9500×9500 RGB PNG (270,750,000 decoded bytes) was accepted with only `DecompressionBombWarning`. Validation itself does not allocate all decoded pixels, but a downstream model runtime that decodes the accepted attachment can incur the resource cost. Apply the recipe-image pixel/memory policy to chat images and turn bomb warnings into rejection.

### F-020 — model-requested memory clearing is silently ignored end to end

**Confidence: high.** The backend puts `clear_requested` into terminal job result/completed-event data at `backend/cortex_backend/api/routes.py:1564-1568,2023` and `backend/cortex_backend/api/jobs.py:585-594`, but `backend/cortex_backend/services/memory_commands.py:20-49` has no runtime caller and the frontend has no handler. `frontend/src/hooks/useGenerationStream.ts:188-217` finishes without inspecting it. The memory prompt promises confirmation, but the user receives neither confirmation nor a clear operation.

### F-021 — Hugging Face discovery advertises nested GGUF files that download rejects

**Confidence: high.** `backend/cortex_backend/llamacpp/download.py:140-146` lists `rfilename` values such as `weights/model.gguf`; resolver validation at `:75-76` permits only a bare filename. A listed nested file then fails immediately when selected. Either accept a canonically validated repository-relative path or filter discovery to downloadable entries.

### F-022 — legacy migration accepts records that make migrated chats unopenable

**Confidence: high.** `backend/cortex_backend/repositories/legacy_storage.py:203-225,259-307` validates a message role only as a nonempty string and does not bound source file/message/content size. `backend/cortex_backend/api/routes.py:2514-2530` later enforces the API role enum. A migrated `role: "tool"` succeeded and then GET of that chat returned 500. Validate and quarantine each legacy record, bound input before full JSON loading, and report skipped data.

### F-023 — in-memory job event histories have no event or byte ceiling

**Confidence: high.** `_JobRecord.events` at `backend/cortex_backend/api/jobs.py:88-107,672-693` is unbounded. GGUF emits download progress per chunk (`backend/cortex_backend/llamacpp/download.py:178-208`; `backend/cortex_backend/api/routes.py:1225-1246`) and generation retains frequent text deltas (`routes.py:1900-1915,2258-2260`). A large download or response can retain substantial RAM until job pruning. Use a byte/event ring buffer and persist/replay only bounded terminal summaries.

### F-024 — the composer's one-model state is non-selectable and does not direct the user to Settings

**Confidence: high.** The backend defaults `models.chat` to null (`backend/cortex_backend/core/settings.py:32-38`), and App gates the composer at `frontend/src/app/App.tsx:488-508`. `frontend/src/features/models/LocalModelMenu.tsx:188-198` renders one model as a static label plus rescan, never invoking selection. The tested `LocalSetup.tsx` is not rendered in production. The user must discover the selector in Settings (`frontend/src/features/settings/SettingsPanel.tsx:175-187`) or the command palette while the ordinary composer surface stays disabled. Make the one-model state selectable there or auto-select with explicit user-visible confirmation.

### F-025 — memory editor inputs remount on every keystroke

**Confidence: high.** `frontend/src/features/settings/MemoryPanel.tsx:49-53` keys each row with `${index}-${item}` while editing changes `item`. React remounts the input on every character, losing focus/caret. Use a stable row identifier and add an edit-continuity test.

### F-026 — failed memory API calls still mutate the panel draft

**Confidence: high.** App catches and swallows rejections in `frontend/src/app/App.tsx:348-375`. `frontend/src/features/settings/MemoryPanel.tsx:20-29` treats callback resolution as success, appending an unsaved memo or clearing its draft even though persistence failed; the draft at `:14` does not resynchronize. The result is an error toast paired with contradictory success-like local state. Rethrow after notification or return a typed result, and update local state only from the server response.

### F-027 — unsaved GGUF directory edits are ignored by the adjacent Download action

**Confidence: high.** The directory field only changes Settings' local draft (`frontend/src/features/settings/SettingsPanel.tsx:307-311`). `frontend/src/features/models/ModelsPanel.tsx:157-164` and `frontend/src/api/client.ts:304-308` omit it from the request; the backend reloads persisted settings at `backend/cortex_backend/api/routes.py:1211-1212`. Download before Save writes to the previous folder. Disable Download until saved, pass the chosen path explicitly, or state the behavior clearly.

### F-028 — command-palette Settings can close back to the wrong chat

**Confidence: high.** Header navigation records the active thread, but the palette callback at `frontend/src/app/App.tsx:510-518` only navigates. Settings Close uses the old `settingsReturnChatId` at `:539-541`, returning to `/chat/new` or a previously visited chat. Centralize settings navigation so every entry point captures the same return route.

### F-029 — “System” theme does not react to live Windows theme changes

**Confidence: high.** `frontend/src/app/App.tsx:165-168` samples `matchMedia().matches` only when the setting value changes and registers no `change` listener. Subscribe while theme is `system`, clean up the listener, and test a media-query event.

### F-030 — Settings keeps the initial llama.cpp status until a full workspace reload

**Confidence: high.** Polling updates Zustand at `frontend/src/app/App.tsx:217-234`, but Settings is passed `system.llamacpp`, the initial load snapshot, at `:496-507`. Live polling never updates that panel; a full workspace/page reload is required. Read the live store value when rendering Settings; consider polling while its runtime panel is visible, not only when a GGUF is selected.

### F-031 — disabled composer can display the wrong runtime message

**Confidence: high.** App computes model availability and GGUF readiness separately but always passes Ollama's connection text at `frontend/src/app/App.tsx:488-508`. `frontend/src/features/chat/MessageComposer.tsx:229-239` shows it verbatim. Missing selections can say “Ready,” and unavailable GGUF selections report irrelevant Ollama state. Derive a typed disable reason from the actual selected runtime.

### F-032 — slow initial GETs can overwrite newer user mutations

**Confidence: high.** `frontend/src/app/App.tsx:130-149` starts unversioned background group/model requests and later wholesale replaces stores. A stale response can land after group creation, rescan, pull, or download. These requests are also not aborted when the authenticated workspace unmounts, so their Zustand setters can still run afterward. Abort/version requests and ignore responses older than the latest mutation/session generation.

### F-033 — failed rename/delete/group operations close dialogs and discard input

**Confidence: high.** App catches without rethrowing at `frontend/src/app/App.tsx:273-315`. Callers at `frontend/src/features/shell/AppShell.tsx:167-190` and `ChatLibrary.tsx:179-187,331-340` therefore see a resolved promise and close even after failure. Return explicit success/failure, keep the dialog open on failure, and add pending guards to prevent duplicate submits.

### F-034 — collapsed compact sidebar remains keyboard- and screen-reader-reachable

**Confidence: high.** `frontend/src/features/shell/AppShell.tsx:124-162` always renders all controls; CSS at `frontend/src/styles/tokens.css:315-317,908-913` only changes width/transform. Offscreen controls remain in tab and accessibility order. Use `inert`/hidden semantics when closed; if the compact drawer is intended to be modal, also implement the corresponding focus and Escape behavior.

### F-035 — sidebar search has no empty state when any group exists

**Confidence: high.** `frontend/src/features/shell/ChatLibrary.tsx:85` treats any group as content, but no-match groups return null at `:103-107`; the “No chats match” message at `:157-159` is then suppressed. Base empty state on rendered matches, not raw group count.

### F-036 — chat actions have no reliable touch affordance while still consuming title width

**Confidence: high.** `frontend/src/styles/tokens.css:357-361` keeps the action rail in flex layout at opacity zero and reveals it only on hover/focus. The mobile rule at `:908-916` targets message actions, not `.chat-row-actions`. Some touch browsers synthesize hover/focus after a tap, but there is no reliably visible or discoverable affordance and the hidden rail still truncates titles. Provide an always-visible touch affordance and remove hidden controls from layout when appropriate.

### F-037 — crossing the 40-message virtualization threshold can yank a reader to the bottom

**Confidence: high.** `frontend/src/features/chat/MessageList.tsx:25-40,80-105` replaces the plain scroller with Virtuoso at 40 messages and mounts it with the final item selected. A reader scrolled up at message 39 is forced to the bottom when message 40 arrives. Preserve anchor/scroll state across the implementation switch or use one list strategy consistently.

### F-038 — live generation tracking is lost across routes if active-job persistence later fails

**Confidence: high.** ChatPage aborts its consumer on unmount (`frontend/src/features/chat/ChatPage.tsx:154`) and restores only from storage at `:235-258`; storage helpers in `frontend/src/hooks/useGenerationStream.ts:34-64` intentionally suppress failures. If storage becomes unavailable or hits quota after startup, returning from Settings can clear the live global job even though the backend continues. Keep authoritative active-job state in an app-level store and recover by thread/status API, with storage only as an optimization.

### F-039 — an unused query bootstrap credential can remain in URL/history

**Confidence: high; launcher normally uses a fragment.** App starts session-ready from stored state at `frontend/src/app/App.tsx:40-42`, but URL cleanup occurs only after exchange at `:59-65`. If a valid stored session and `?bootstrap=...` coexist, the token remains in the address bar and browser history. Always scrub bootstrap parameters immediately after parsing, whether or not exchange is needed; prefer fragment-only input.

### F-040 — FastAPI validation details are discarded by the frontend client

**Confidence: high.** `frontend/src/api/client.ts:489-497` handles only string or `{message}` details. Standard FastAPI 422 responses contain an array, so an actionable field error becomes “The local workspace did not respond.” A direct invalid group request confirmed this shape. Parse the validation array into safe field/message text.

### F-041 — the execution tray/client expose no generic non-code result or artifact UI path

**Confidence: medium.** `frontend/src/features/shell/ExecutionTaskTray.tsx:158-190` renders detailed results only for `code.exec.v1`. `downloadExecutionArtifact()` exists at `frontend/src/api/client.ts:373-380` but has no production caller. Whether a particular profile surfaces its output elsewhere is workflow-dependent, but this shared execution surface cannot inspect or download generic results. Define a generic result/artifact presentation contract.

### F-042 — frontend freshness tracking omits real build inputs

**Confidence: high.** `backend/cortex_backend/launcher/frontend.py:23-31,69-79` hashes selected config plus `src`/`public`, but omits `contracts/cortex-api.ts`, Vite `.env*`, and other external build inputs. `FrontendManifest` records Node/npm majors at `:38-55`, while `needs_build()` at `:132-142` ignores them. Stale types, stale remote API configuration, or incompatible caches can be reused. Hash every build input and toolchain identity.

### F-043 — concurrent frontend builds can delete each other's active staging trees

**Confidence: high.** `backend/cortex_backend/launcher/frontend.py:156-178` deletes every sibling `.cortex-frontend-build-*` directory without checking age, PID, lock, or liveness. `main.py:236-248` runs `--build-frontend` before the single-instance lock, and shared install cache operations at `frontend.py:205-227` are unprotected. Use a build lock and delete only stale, ownership-verified staging directories.

### F-044 — generated API contracts omit authentication and leave execution SSE untyped

**Confidence: high.** `backend/cortex_backend/api/routes.py:1023-1031` declares execution SSE without a schema; `contracts/openapi.json` therefore has an empty event-stream media object. `tools/generate_contracts.py:27-33` inserts `ExecutionSSEEvent` only as an orphan component. Authentication hidden in a custom request dependency produces no `securitySchemes` or operation security declarations, and manually read headers such as `Last-Event-ID`/`X-Cortex-Handoff` are absent. Generated clients cannot discover the real wire contract.

### F-045 — packaged startup failures provide no usable diagnostics

**Confidence: high.** `packaging/Cortex.spec:62-73` builds with `console=False`; `main.py:140-155,364-404` has no durable file logger and a fallback dialog that tells users to run the package from a terminal. A GUI-subsystem executable still lacks attached console streams, so missing assets, port conflicts, WebView failures, and bad paths remain opaque. Write a privacy-safe startup log and include its path/copy action in the dialog.

### F-046 — absolute session expiry strands a running desktop window after 24 hours

**Confidence: high.** `backend/cortex_backend/api/security.py:49-63,117-137` caps session renewal at 24 hours. On 401 the frontend clears session; the one-time bootstrap has already been erased (`frontend/src/app/App.tsx:49-72`). Onboarding cannot request a new token, and a second launch only focuses the existing process (`main.py:253-272`). Add an authenticated launcher handoff/rebootstrap flow for the live window or align expiry with process lifetime.

### F-047 — private or untrusted text is logged without classification/redaction

**Confidence: high for the logging path; prompt leakage from pinned llama.cpp is not proven.** `backend/cortex_backend/repositories/legacy_storage.py:585-590` logs exact chat titles and `:70-72` logs the database path. Titles can include newlines/control text. `backend/cortex_backend/llamacpp/server_manager.py:301-309,537-557,641-653,732-760,746-749` captures and logs raw third-party output and exposes a startup tail through authenticated `/api/v1/system` runtime status (`backend/cortex_backend/api/routes.py:218-221`). Replace content with stable identifiers/error codes and sanitize all third-party diagnostic text.

### F-048 — Windows Job Object containment failures are usually silent

**Confidence: high.** `backend/cortex_backend/llamacpp/server_manager.py:250-289` starts the process before containment, returns silently when several Win32 calls return false, and ignores `AssignProcessToJobObject`'s BOOL result. Only raised `OSError` logs a warning. Cortex can appear to own the child while hard exit leaves llama-server consuming GPU/RAM. Check every return, terminate/fail closed if containment is required, and test each failure path.

### F-049 — bundled WebView installer is not verified immediately before execution

**Confidence: high on the gap; impact depends on package/install ACLs.** Build-time checks exist, but `backend/cortex_backend/launcher/webview_runtime.py:53-68` trusts any existing bundled installer. `tests/test_launcher.py:236-254` accepts a dummy file. A modified one-folder package can execute a replacement when WebView is missing. Recheck Authenticode publisher and/or a signed manifest hash at runtime.

### F-050 — custom Windows data roots and launcher secrets lack canonical/DACL enforcement

**Confidence: high on behavior; default profile path is lower risk.** `backend/cortex_backend/core/paths.py:27-32,128-130` accepts any absolute custom data directory without reparse/UNC or private-DACL policy. `backend/cortex_backend/launcher/instance.py:79-84` relies on `os.chmod(0o600)`, which does not establish a restrictive Windows DACL. Shared, junctioned, UNC, or inherited-permission roots can expose handoff secrets and databases. Canonicalize and enforce an explicit Windows ACL policy.

### F-051 — packaging/runtime regressions are not gated before merge

**Confidence: high.** `.github/workflows/quality.yml:97-100` skips the heavy job on pull requests, even for packaging/launcher/worker changes. The heavy job installs unconstrained latest PyInstaller at `:132-133`, does not launch the produced executable, and tests only Python 3.11 despite advertised Python 3.10+. Actions use mutable major tags and Python dependencies are range-resolved without a release lock/hashes. Add path-triggered PR packaging/startup smoke tests and a supported-version matrix with pinned build inputs.

### F-052 — “pinned Wasmtime” qualification can run an unpinned global package

**Confidence: high.** `tools/execution_spikes/run_pinned_wasmtime_smoke.ps1:35-51` does not check the target pip install exit code and leaves normal site-packages enabled after prepending the target to `PYTHONPATH`. If install fails and global Wasmtime exists, qualification can pass against it. Version and expected hash are also jointly caller-overridable at `:1-4`. Run the probe in a disposable virtual environment with global/user sites disabled, verify import path/version/hash, and make the approved pin immutable in repository policy.

### F-053 — dev Vite port race can expose the live bootstrap token to a local page

**Confidence: high on code path, medium on race practicality; dev mode only.** `main.py:317-327` bind-selects then releases Vite's port; `backend/cortex_backend/launcher/supervisor.py:17-34` accepts any 2xx root. If another loopback process wins, `main.py:340-345` opens it with the bootstrap in the fragment. Its JavaScript can call the backend because CORS permits any loopback port (`backend/cortex_backend/api/app.py:224-229`) and origin validation ignores port (`api/security.py:147-155`). Bind/identify the expected dev server and restrict allowed origin to the chosen port.

### F-054 — default in-memory attachment storage races and never evicts

**Confidence: high; primarily demo/default dependency mode.** `backend/cortex_backend/services/attachments.py:237,256-272` performs request-ID check-then-insert without a lock; concurrent identical IDs can return different descriptors and leave one unresolvable. Expired in-memory records remain at `:341-348`. Add locking/idempotent insertion and bounded expiry eviction, or avoid this implementation outside isolated tests.

## Priority 3 — lower-impact bugs and quality concerns

### F-055 — whitespace-only titles, group names, and messages pass validation

**Confidence: high.** Pydantic `min_length=1` in `backend/cortex_backend/api/schemas.py:176-222` runs before later stripping in routes. Reproduction created a chat/group with an empty post-strip name and stored a two-space message. Use before-validation trimming and reject empty/control-only content.

### F-056 — generation reconnect can hammer the API indefinitely

**Confidence: high.** `frontend/src/hooks/useGenerationStream.ts:7,147-243` retries every 250 ms without exponential backoff, jitter, offline pause, or a ceiling while the page remains mounted and no recognized terminal/auth/not-found response arrives. Immediate stream/status failures can sustain several local requests per second. Add capped exponential backoff and a user-visible paused/retry state.

### F-057 — move-to-group uses ARIA menu roles without menu keyboard behavior

**Confidence: high.** `frontend/src/features/shell/ChatLibrary.tsx:272-310` declares `menu/menuitem` but lacks initial focus, arrow navigation, Home/End, Escape, and typeahead. Implement the composite-menu pattern or use simpler disclosure/list semantics.

### F-058 — preview launcher prints a live bootstrap credential

**Confidence: high.** `Cortex_Preview.py:204-207` prints the live token, conflicting with the repository rule against logging credentials. Console capture, IDE history, and support bundles can retain it during its validity window. Deliver it through a one-time browser fragment/handoff or another local authenticated exchange that avoids durable console logging while still letting preview users authenticate.

### F-059 — instance lock file grows by one byte per launch attempt

**Confidence: high.** `backend/cortex_backend/launcher/instance.py:64-68` opens in append mode, seeks, and writes a byte; append semantics place every write at EOF. Release removes record/secret files but not the lock file (`:119-139`). Open without append and truncate/retain a fixed-size lock record.

### F-060 — contract/check tooling is non-atomic and can mutate the worktree while “checking”

**Confidence: high.** `tools/generate_contracts.py:34-38` writes JSON and TypeScript separately, so interruption can mismatch them. Its renderer at `:41-89` intentionally uses `unknown` for current unconstrained schemas and would silently do the same for future unsupported constructs such as root enums or `allOf`. `scripts/check.ps1:93-102` verifies drift by regenerating directly into the worktree, and `-SkipBackend -SkipFrontend` can report “All 0 checks passed.” Generate to temp files, compare, then atomically promote only in explicit generation mode; fail loudly on unsupported schemas and reject a zero-check invocation.

### F-061 — production frontend ships as one very large JavaScript chunk

**Confidence: high; performance concern.** `npm run build` produced an 892.82 kB minified JavaScript chunk (280.61 kB gzip) and Vite's >500 kB warning. Settings, model management, execution tray, and virtualization are candidates for route/feature splitting. Measure startup on representative Windows/WebView hardware before setting a budget.

### F-062 — browser tests make an unmocked localhost request and omit the chat-group success path

**Confidence: high.** All 18 Playwright tests passed, but each emitted Vite proxy `ECONNREFUSED 127.0.0.1:8765` for `/api/v1/chat-groups`. Fixtures do not mock that endpoint, so the suite exercises only the group's failure fallback and makes a real network attempt. Make unexpected requests fail the test and add success/race coverage.

### F-063 — one full-suite run exposed intermittent execution failures

**Confidence: medium; product cause not established.** One quick-gate run produced 2 failures, 630 passes, and 1 skip. One approval-to-running test exceeded its two-second poll; one scratch execution returned failed. Each failed test passed alone, and five repeated invocations of the pair passed all 10 test cases. The evidence establishes intermittence but not whether order, load, Python 3.14, leaked workers, global state, or tight waits are responsible.

### F-064 — session accumulation and process-lifetime client cleanup gaps remain

**Confidence: high.** Expired sessions remain in the growing `_sessions` map (`backend/cortex_backend/api/security.py:68,97-136`). Owned `httpx.Client` instances created in `backend/cortex_backend/llamacpp/server_manager.py:344-347` and `chat_client.py:41` are process-lifetime resources reclaimed at exit, but app teardown at `backend/cortex_backend/api/app.py:169-181` does not close them explicitly. Add bounded session expiry cleanup and explicit client shutdown for deterministic resource release.

### F-065 — the remaining-reliability document is stale and can misdirect work

**Confidence: high.** `docs/REMAINING_RELIABILITY_FIXES.md` still lists DNS rebinding, settings separation, and SSE replay as unimplemented, while current code/tests contain implementations for those areas. Mark the note historical or refresh it against the current snapshot; stale gap lists undermine triage.

### F-066 — production port selection still has a bind/close/start TOCTOU reliability window

**Confidence: high; primarily availability.** `main.py:124-131,250-305` releases the selected backend port before Uvicorn binds it. Supervision generally turns a stolen port into startup failure rather than accepting an impostor, but local contention can still cause intermittent startup failure. Prefer passing a prebound socket or retrying a fresh port under a single bounded startup transaction.

### F-067 — the crash screen makes an unconditional data-integrity promise

**Confidence: high.** `frontend/src/app/ErrorBoundary.tsx:23-27` always says “Your local data was not changed.” A render crash can occur after an API mutation already committed, so the component cannot know this. Replace it with a factual statement that no further action was taken by the crashed view and offer reload/diagnostics.

### F-068 — native dependency-install failures can be masked by stale global packages

**Confidence: high.** `packaging/build_windows.ps1:13-16` and `packaging/build_recipe_worker.ps1:18-20` do not explicitly check pip's exit code. Under ordinary PowerShell native-command semantics, execution continues and later tooling may come from an older global install. Use an isolated build environment, check each native exit, and print/verify resolved tool versions before packaging.

## Verification evidence

Commands were run from the repository root unless noted.

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

## Limits and non-findings

- This audit did not modify product code or attempt destructive/external operations.
- No npm advisory was present at audit time.
- The global Python environment has unrelated package conflicts, so `pip check` output was not used as repository evidence.
- The Windows package and native recipe worker were not rebuilt end to end during this audit; build-script findings come from code review and safe native-exit semantics reproductions.
- The llama port-race and dev Vite token-race findings require a competing local process. The binary-cache finding requires local file tampering or equivalent corruption. Their prerequisites lower likelihood, not impact.
- Raw llama-server output is definitely unredacted; this audit did not establish that the pinned llama.cpp build logs prompt bodies by default.
- Pre-existing `.gitignore` and screenshot-tool worktree changes were preserved and excluded from the audit diff.
