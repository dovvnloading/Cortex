# ADR-0001 Phase 1 approval and restart-supervisor contract

- **Status:** Implemented and verified; production provider enablement remains a separate gate
- **Parent:** [Phase 1 API/task-tray contract](0001-phase1-api-contract.md)
- **Scope:** Durable approval state and fake-only startup recovery

This stage closes the two remaining Phase 1 design gaps without enabling a real
provider. Approval is a persisted policy state, never a UI-only flag. Recovery is a
single-instance supervisor that reclaims stale fake jobs from immutable payloads;
it never resumes a native process or trusts a partial worker stream.

## Durable approval state

The durable job record has an associated approval record. Missing approval rows are
interpreted as `not_required` only for the `fake.v1` profile. The public response
always includes the effective state:

`not_required`, `pending`, `approved`, `denied`, or `expired`.

Allowed transitions are deliberately narrow:

```text
not_required --request (non-fake profile)--> pending
pending      --allow once-----------------> approved
pending      --deny-----------------------> denied
pending      --expiry---------------------> expired
```

`fake.v1` cannot enter `pending`; any attempt is rejected with a stable policy
error. No automatic path may transition to `approved`. Terminal jobs cannot mutate
approval state. Expired or denied approval never starts a worker, and an approval
decision is owner-scoped at the API boundary.

Approval rows contain only state, scope digest, safe reason, creation/decision
timestamps, and expiry; they never contain source, filesystem paths, or raw model
content. Expiry is enforced on reads and by the cleanup/recovery pass.

## Startup supervisor and recovery

`DurableFakeCoordinator` owns a single installation-scoped recovery lease in the
execution database. Startup is fail-closed if another live supervisor owns it. A
crashed supervisor's lease becomes reclaimable after its TTL.

Once the supervisor lease is held, startup recovery:

1. finds queued/running/cancelling jobs with expired per-job leases;
2. deletes each expired worker lease and appends one ordered `execution.recovered`
   event;
3. rehydrates only the bounded fake plan from the immutable job payload;
4. launches a fresh fake worker, never a previous process or stream; and
5. fails a malformed payload with stable `recovery_invalid_payload` rather than
   guessing or selecting another provider.

Recovery is idempotent: a second pass sees no expired lease and emits no duplicate
recovery event. Terminal jobs are never reclaimed. Shutdown releases the supervisor
lease after workers have received cancellation.

The supervisor lease is not an OS/process containment primitive. The application
lifecycle now owns startup/recovery/shutdown through the health-gated control-plane
contract in [the production lifecycle ADR](0001-phase1-production-lifecycle.md).
Real provider process-tree reaping remains a separate runtime gate backed by the
Phase 0 Job Object evidence.

## API behavior

- Fake preview creation always reports `approval_state=not_required`.
- Status/task responses report the persisted effective approval state.
- Approval decisions are exposed only for explicitly persisted, non-fake approval
  rows; the endpoint accepts allow-once or deny for the exact owner/job and never
  dispatches a provider. Broader provider policy remains separately reviewed.
- A startup recovery event is replayable through the existing execution SSE route.

## Required tests before the stage can close

1. A fake job defaults to `not_required`; requesting approval for `fake.v1` fails
   closed.
2. A non-fake profile follows only the listed approval transitions; invalid or
   terminal transitions are rejected.
3. Approval expiry is enforced even before cleanup runs.
4. A live supervisor lease blocks a second coordinator; an expired lease is
   reclaimable exactly once.
5. Startup recovery replays one recovered event, resumes a valid fake plan, and
   reaches a terminal result after a simulated restart.
6. Malformed persisted payloads fail with `recovery_invalid_payload` and never
   execute or select a fallback provider.
7. Owner-scoped API responses expose approval state but never lease-owner,
   supervisor-token, payload, or path fields.

This contract keeps the Phase 1 gate closed until these tests pass and the evidence
log is updated. It does not authorize Wasmtime, host subprocesses, workspace, or
network execution.
