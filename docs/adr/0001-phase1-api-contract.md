# ADR-0001 Phase 1 API and task-tray contract

- **Status:** Contract frozen; fake preview, approval transport, and installation-principal ownership wired
- **Parent:** [ADR-0001](0001-capability-tiered-agentic-execution-harness.md)
- **Scope:** Authenticated, owner-scoped lifecycle transport for the Phase 1
  deterministic fake executor

This contract is the boundary between the durable execution repository and the
Cortex UI. It is deliberately frozen before production routes or model tools are
added. The
Phase 1 transport may expose only the deterministic `fake.v1` provider behind an
explicit preview/test switch; it must reject source code, filesystem paths,
network options, runtime selectors, and arbitrary capability grants. A production
route that can select Wasmtime, a host process, or a recipe provider is out of
scope for this stage.

## Authentication and ownership

Every endpoint uses the existing loopback `SessionManager` bearer session. The
session is short-lived authentication state; its `installation_principal_id` is
the execution owner. The principal is a random 256-bit identifier persisted in the
durable execution database under Cortex's per-user application-data directory and
is never returned by the API. Each native-window session maps to that same
principal, so a UI reload or application restart can reattach the installation's
own tasks without weakening loopback authentication. A caller cannot read,
cancel, stream, or enumerate a job owned by another installation principal. A
missing, malformed, expired, or cross-owner request returns the existing
401/403/404 contract without revealing whether another owner's job exists.

`request_id` is required for the preview creation route, is immutable, and is
deduplicated by `(installation_principal_id, request_id)`. Retrying a request returns the original
`job_id` and never starts a second worker.

## Preview lifecycle endpoints

| Method and path | Purpose | Response |
| --- | --- | --- |
| `POST /api/v1/execution/preview/fake` | Start a bounded deterministic fake job; available only when `preview` and an injected fake coordinator are both true. | `202 ExecutionAccepted` |
| `GET /api/v1/execution/{job_id}` | Read the owner-scoped durable snapshot. | `200 ExecutionStatusResponse` |
| `POST /api/v1/execution/{job_id}/approval` | Allow once or deny an already-pending, owner-owned approval; never creates or launches work. | `200 ExecutionStatusResponse` |
| `POST /api/v1/execution/{job_id}/cancel` | Request cooperative cancellation; idempotent after terminal state. | `200 ExecutionStatusResponse` |
| `GET /api/v1/execution/{job_id}/events` | Replay ordered events after `Last-Event-ID`, then follow live events. | `text/event-stream` |
| `GET /api/v1/execution/tasks` | Return the owner's active/recent task summaries for the global tray. | `200 ExecutionTaskListResponse` |

The preview request is intentionally narrow:

```json
{
  "request_id": "ui-generated-id",
  "outcome": "success",
  "steps": 3,
  "step_delay_seconds": 0.05
}
```

`outcome` is `success` or `failure`; `steps` is 1–20; delay is 0–1 seconds.
There is no `code`, `path`, `command`, `environment`, `network`, `mount`, or
`capability` field. The route returns `404`/`409` when the preview provider is not
enabled rather than silently selecting another executor.

## Stable response envelopes

`ExecutionAccepted` contains `job_id`, `profile`, `status`, `sequence`, and
`request_id`. `ExecutionStatusResponse` contains the same identity plus
`owner-scoped status`, `phase`, `sequence`, `error`, and a validated result map.
Results contain values and artifact IDs only; they never contain absolute paths,
lease-owner tokens, raw exception tracebacks, or private payloads.

`ExecutionTaskListResponse` returns compact summaries only:

```json
{
  "tasks": [
    {
      "job_id": "…",
      "profile": "fake.v1",
      "status": "running",
      "phase": "compute",
      "message": "Fake step 2 of 3.",
      "sequence": 4,
      "can_cancel": true,
      "created_at": "…",
      "updated_at": "…"
    }
  ]
}
```

The tray must not infer progress from a missing event. Unknown phase or missing
message is rendered as a neutral working state and remains announced through
`aria-live="polite"`.

## Event and replay contract

Events are append-only and use the repository sequence as both the SSE `id` and
the JSON `sequence` field. The event names are:

| Event | Terminal? | Required data |
| --- | --- | --- |
| `execution.queued` | no | `message` |
| `execution.started` | no | `message`, `provider` |
| `execution.progress` | no | `message`, `phase` and provider-safe progress fields |
| `execution.cancelling` | no | `message` |
| `execution.recovered` | no | `message` |
| `execution.completed` | yes | validated result and/or artifact IDs |
| `execution.failed` | yes | safe user-facing `message`, stable failure class |
| `execution.cancelled` | yes | `message` |

On reconnect, the server first replays events with `sequence > Last-Event-ID` in
strict ascending order, then follows new events. A stale or malformed cursor is a
400; an unknown or foreign job is handled through the normal owner-safe job error.
The server never emits a second terminal event. A delayed worker callback is
ignored after terminal state is committed.

## Approval state

Phase 1 fake jobs use `approval_state="not_required"`; no approval prompt is
shown. The repository persists the response state and enforces the transition
rules in [the recovery/approval contract](0001-phase1-recovery-approval.md). The
durable response shape supports `not_required`, `pending`, `approved`, `denied`,
and `expired` for later profiles. The owner-scoped decision behavior and safe
presentation fields are frozen in [the approval UI/API contract](0001-phase1-approval-ui.md).
Automatic execution must never transition to `pending`; broader capabilities will
require a separate policy/approval ADR and an explicit user action. The UI must
render `pending` as an actionable approval card, never as a generic spinner. The
decision endpoint accepts only allow-once or deny for an existing immutable scope;
it cannot accept replacement scope and never dispatches a provider. Denial or
expiry terminally cancels the inert job with a stable diagnostic so no approval
task remains queued forever.

## Task-tray accessibility and cancellation

The global tray is mounted outside route content so it survives chat navigation. It
must expose:

- a labelled landmark (`aria-label="Background tasks"`);
- a polite live region for queued/running/completed/failed/cancelled changes;
- a visible phase/message and status text, not color alone;
- a keyboard-accessible `Stop` button while status is queued, running, or
  cancelling; and
- a terminal summary with a retry affordance only when the server reports a safe
  retry path.

Stop is optimistic only for presentation. The UI stays in `cancelling` until the
server emits the terminal `execution.cancelled` event, and it treats a lost SSE
connection as reconnectable rather than as success or failure.

## Contract tests required before route wiring

1. Preview creation is rejected when `preview` or the injected fake coordinator is
   absent; no fallback provider is selected.
2. Duplicate `(owner, request_id)` creation returns one job and one queued event.
3. Status, cancel, task-list, and SSE endpoints cannot cross owner boundaries.
4. SSE reconnect replays only events after the supplied cursor and preserves
   sequence order.
5. A terminal job cannot be changed by a late callback, and cancellation remains
   terminal after reconnect.
6. Response serialization contains no filesystem path, lease token, source, or
   traceback fields.
7. The task tray exposes the required landmark, live region, status text, and
   keyboard Stop action in component tests.
8. Approval decisions are owner-scoped and exactly-once; expiry wins over a late
   allow, sensitive scope/payload data is redacted, and the actionable card is
   keyboard-native without taking focus.

This contract does not close Phase 1. Fake preview, replay, task tray, recovery,
approval-decision transport, and installation-principal wiring are implemented.
Production provider lifecycle integration remains a separate reviewed stage; real
code execution is still unavailable.
