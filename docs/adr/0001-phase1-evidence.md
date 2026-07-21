# ADR-0001 Phase 1 evidence log

- **Phase:** 1 — durable jobs, artifacts, and UI with a fake executor
- **Status:** Approval/recovery backend slice complete; overall Phase 1 remains in progress
- **Scope:** Durable execution workflow only; no model-generated code, guest
  runtime, recipe provider, or host subprocess is enabled.
- **Source decision:** [ADR-0001](0001-capability-tiered-agentic-execution-harness.md)

Phase 1 starts only after the Phase 0 strict probe reports green. The existing
generation `JobRegistry` remains the authority for chat/model jobs; this phase
adds an isolated execution store and deterministic fake provider so lifecycle
failures can be exhausted without executing code.

## Phase 1 checklist

| ADR requirement | Status | Evidence |
| --- | --- | --- |
| Add additive schema/migrations | **Complete (initial schema)** | New execution SQLite schema is isolated behind an injected database path; schema version 1 is created additively. |
| Durable jobs and leases | **Complete (backend slice)** | Lease ownership, expiry, idempotent creation, and stale-lease recovery are covered by repository tests. |
| Ordered durable events/replay | **Complete (backend slice)** | Append-only sequence numbers and cursor replay survive repository re-instantiation. Terminal state is immutable. |
| Artifact store and retention | **Complete (backend slice)** | Copy-in, generated-root confinement, SHA-256 verification, size limits, atomic publish, expiry checks, and cleanup are tested. |
| Task tray/accessibility/approvals | **Task tray and approval persistence complete; approval UX pending** | Global tray has an owner-scoped task list, polite live region, visible phase/status text, keyboard Stop action, and active spinner. Approval state is durable and transition-gated; no approval endpoint/card is exposed for fake-only work. |
| Cancellation and recovery | **Complete (backend + startup supervisor)** | Cooperative fake cancellation, terminal cancellation, per-job lease recovery, single-instance supervisor exclusion/reclaim, startup rehydration, approval expiry, and malformed-payload fail-closed behavior are covered; process/runtime cancellation is deferred to later phases. |
| Deterministic fake provider | **Complete (backend slice)** | `fake.v1` emits fixed prepare/progress/completion/failure/cancellation outcomes and never accepts source, paths, network, or host-process controls. |
| Phase 1 exit gate | **Blocked** | Durable backend, authenticated fake-only preview API, SSE replay, task tray, approval persistence/enforcement, and startup supervisor are green. Approval UI/API, installation-principal wiring, and production lifecycle/recovery integration remain. |

## Cross-check findings

- Existing `backend/cortex_backend/api/jobs.py` is in-memory and serves chat/model
  jobs; it cannot survive restart and is intentionally not replaced in this first
  vertical slice.
- Existing SSE routes already provide an event-stream shape that the durable
  execution API can reuse after the store contract is stable.
- No execution schema, artifact root, lease table, or fake execution provider was
  present before this phase.
- The Phase 0 probes remain outside application imports and packaging.
- The first implementation adds only an authenticated fake-only preview route;
  the coordinator is injected explicitly and the route is unavailable unless the
  app advertises `execution_preview_available`. No production route, model tool,
  or real provider can be selected through this surface.
- The task tray is mounted outside route content and polls only when the backend
  advertises the explicit preview capability. Normal app instances do not poll a
  missing execution service.
- Schema version 2 adds approval and single-instance supervisor tables
  additively; version-ahead databases fail closed.
- Startup recovery rehydrates only fake.v1 payloads and skips pending/denied/expired
  approvals. Unknown profiles or malformed payloads fail with
  `recovery_invalid_payload`.

## Phase 1 invariants

1. Every durable job has an owner, immutable request key, profile, status, and
   monotonic event sequence.
2. Retried requests with the same owner/request key return the original job and
   cannot create a duplicate.
3. A lease has an owner and expiry; an expired lease is recoverable exactly once.
4. Events are append-only, replayable after a cursor, and bounded by retention.
5. Artifacts are copied into a generated root, hash-verified, size-limited, and
   atomically published; arbitrary source paths are never accepted.
6. The fake provider is the only Phase 1 executor. It never interprets source,
   launches a process, imports Wasmtime, or accesses user files/network.
7. Terminal state is immutable: late worker callbacks cannot overwrite a result or
   append a second terminal event.
8. Artifact retention is enforced at read time as well as by cleanup, so expiry is
   fail-closed even if the cleanup worker is delayed.
9. Approval transitions are durable and monotonic; fake.v1 cannot request approval,
   and pending approval cannot reach a terminal job state.
10. Only one recovery supervisor can reclaim expired leases; recovery is idempotent
    and never resumes a previous process or stream.

## Re-run target

```powershell
python -m compileall -q backend\\cortex_backend\\execution tests\\test_phase1_execution.py
python -m pytest tests/test_phase1_execution.py -q
python -m pytest tests/test_phase1_execution_api.py -q
python -m pytest tests/test_phase1_recovery_contract.py -q
python -m pytest -q
npm.cmd run lint --prefix frontend
npm.cmd run typecheck --prefix frontend
npm.cmd run build --prefix frontend
npm.cmd test --prefix frontend -- --run
```

**Validation result (2026-07-21):** compileall passed; the full Python suite passed
108 tests with one pre-existing `pytest-asyncio` deprecation warning. Frontend lint,
typecheck, production build (`tsc -b` + Vite), and all 35 component tests passed.
Phase 1 cannot close until approval UI/API, installation principal wiring, and
production lifecycle/recovery integration are separately reviewed; this stage does
not enable production code execution.
