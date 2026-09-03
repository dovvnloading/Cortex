# ADR-0001 Phase 1 approval UI and decision API contract

- **Status:** Implemented and verified
- **Parent:** [Phase 1 approval/recovery contract](0001-phase1-recovery-approval.md)
- **Scope:** Owner-scoped decisions and accessible presentation for already-pending
  durable approvals

This stage makes the persisted approval state actionable without adding a provider,
an execution-creation route, or any model-controlled authorization path. The only
Phase 1 executor remains `fake.v1`, and `fake.v1` remains incapable of requesting
approval. Tests may seed an inert `artifact.extended.v1` job to exercise the
decision boundary; no Phase 1 route may create or launch that profile.

## API boundary

Status and task-list responses expose only the approval information required for a
decision: effective state, a server-authored safe reason, and expiry. They never
expose the scope digest, immutable payload, lease owner, supervisor token, source,
path, or raw model content.

`POST /api/v1/execution/{job_id}/approval` accepts exactly one decision:

```json
{ "decision": "approved" }
```

or:

```json
{ "decision": "denied" }
```

The existing bearer session is the authority. The server binds the decision to
the authenticated owner, exact `job_id`, and already-persisted immutable scope; the
client cannot submit or replace scope. Unknown and foreign jobs return the same
`404` response. Terminal, non-pending, duplicate, or expired decisions return
`409`. Malformed decisions return `422`.

Approval expiry is effective on every status/task read even if cleanup has not run.
The decision transaction rechecks owner, job state, approval state, and expiry under
one immediate SQLite transaction. A decision received at or after expiry persists
`expired`, appends one ordered approval event, and cannot be converted to approved.
Every successful decision appends one ordered event and returns the fresh durable
snapshot.

Denial and expiry are terminal for the inert execution job: the same transaction
records `denied` or `expired`, appends one `execution.cancelled` event, and stores
stable `approval_denied` or `approval_expired` diagnostics. This prevents an
unlaunchable approval job from remaining queued forever. Approval leaves the job
queued for the later provider-dispatch stage.

This endpoint never launches a worker. Provider dispatch after approval belongs to
the later production-lifecycle stage and must perform its own final authorization
gate immediately before execution.

## User experience

A pending approval is an actionable card in the existing global task tray, not a
spinner or modal dialog. It contains:

- visible “Action required” status;
- the safe reason and bounded profile label;
- an expiry time when present;
- keyboard-native **Allow once** and **Deny** buttons; and
- an in-place busy/result status announced through the existing polite live region.

The card does not take focus when it appears. While one decision request is in
flight, both decision buttons for that job are disabled. A network or policy failure
keeps the card actionable and reports the error through the existing toast/status
surfaces. `denied` and `expired` never show a spinner or approval controls.

This follows WCAG 2.2 status-message guidance: dynamic state is programmatically
announced without an unnecessary focus change. It also follows OWASP transaction
authorization guidance by keeping authorization server-side, binding it to exact
transaction data, and retaining a final control gate before any later execution.

## Required tests before this stage closes

1. Missing, foreign, fake-profile, terminal, non-pending, duplicate, malformed,
   and expired decisions fail with the documented status without leaking job data.
2. An owner can allow once or deny exactly one pending approval; event sequences
   remain gap-free and the stored scope digest cannot be changed by the request.
3. Reads report an effectively expired approval before cleanup, and a late allow
   persists expiry rather than approval.
4. Response serialization omits scope digest, payload, paths, lease data, and raw
   exception details.
5. The task tray renders pending approval without a spinner, exposes keyboard-native
   Allow once/Deny controls, disables duplicate submission, and announces results.
6. API/client failures retain the actionable card and produce a safe user message.
7. Full Python, frontend lint/typecheck/unit, and production-build gates pass.

## Primary references

- [W3C WCAG 2.2 — Understanding status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages)
- [OWASP Transaction Authorization Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Transaction_Authorization_Cheat_Sheet.html)

## Implementation result

The owner-scoped decision route, effective-expiry reads, atomic decision checks,
safe approval response fields, generated TypeScript/OpenAPI contracts, non-modal
task-tray card, and handled-failure behavior are implemented. Concurrent
approve/deny testing proves exactly one decision commits with gap-free events.
No non-fake creation route, provider dispatch, model tool, source interpreter,
filesystem capability, or network capability was added.
