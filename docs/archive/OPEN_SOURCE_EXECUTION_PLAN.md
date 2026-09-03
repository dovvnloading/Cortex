# Cortex open-source execution plan

- **Status:** Complete for the source-checkout scope; this remains the authoritative delivery plan for the open-source desktop app.
- **Scope:** Local model assistance, bounded computation, and user-requested image transformations.
- **Non-goal:** Turning Cortex into a terminal, IDE, autonomous coding agent, or hosted service.

## Plain-English decision

Cortex is a local Windows desktop app. The product should do three useful things:

1. keep ordinary chat fast and reliable;
2. verify a small, safe computation when it materially improves an answer; and
3. let a user make a common change to an uploaded image.

The execution feature must be safe enough for untrusted model-generated input, but it
does not need bank-style release bureaucracy. A contributor can clone the repository,
run the normal local profile, and improve the app without an external reviewer,
production signing key, attestation service, or paid infrastructure.

When the runtime reports `blocked`, a specific optional capability is unavailable and
the app has failed closed. Ordinary chat remains available; this is not an unfinished
roadmap phase.

## Delivery status

| Workstream | Status | Meaning |
| --- | --- | --- |
| Chat and model-management UX | Complete | Model-pull progress and status are implemented and tested. |
| Durable execution foundation | Complete | Jobs, events, artifacts, cancellation, recovery, and task-tray status are implemented and tested. |
| Fixed image-recipe safety boundary | Complete | Attachment staging, typed plans, output validation, cancellation, retention, ownership, and cleanup are in place. |
| Automatic scratch computation | Complete | `scratch.auto.v1` evaluates a small decimal-expression language in a short-lived local worker. Explicit requests such as "calculate 9 * 9" are verified before generation, and a setting can disable this behavior. |
| User-facing attachment transformations | Complete | The normal app now stages a user-selected PNG/JPEG/WebP, runs a fixed transform in a local worker, shows progress/Stop/error state, and provides a result download. |
| Workspace mutation and network access | Deferred | Not needed for the product goal; each would require a separate design and explicit user permission. |

## Delivery record and remaining product work

### 1. Narrow scratch-compute MVP — implemented

The shipped tool is a small expression language, not arbitrary Python, shell, or
WebAssembly source. It runs in a short-lived local worker process and supports bounded
decimal arithmetic plus `abs`, `sqrt`, `min`, `max`, and `round`. The app recognizes
unambiguous requests such as "calculate 12 * (3 + 4)", waits a short bounded time for a
verified result, and gives that observation to the local model. The General settings
toggle can disable automatic computation.

Implemented protections:

- no filesystem, secrets, network, shell, subprocess, package-install, or persistence API;
- bounded expression size, AST depth, operation count, precision, input, output, and wall time;
- durable progress, Stop/cancel, timeout, and clean worker shutdown;
- typed requests and bounded observations, never raw paths or tracebacks; and
- ordinary text chat when the tool is unavailable.

The scratch tool cannot read attachments or modify files. It is intentionally not a
general coding agent.

### 2. Normal image transformation flow — implemented

The composer now has an Image transformation control. A user chooses a PNG, JPEG, or
WebP file and a fixed operation such as grayscale, contrast, or brightness. The UI
shows an active spinner and a human-readable phase, offers Stop while work is active,
surfaces a safe failure message, previews the result, and downloads an owner-scoped
artifact. Known image operations remain fixed recipes rather than generated code.

### 3. Final product hardening — source checkout complete

The source checkout passes the full Python and frontend suites, lint, typecheck, and
production frontend build. Before distributing a Windows binary, run the existing
package smoke check and a brief manual Windows verification for the two shipped
profiles. Keep this as a short release checklist; it is not a separate security-review,
signing, or service-integration project.

## Current implementation evidence

- `python -m pytest -q`: **345 passed, 1 skipped**.
- `npm.cmd run lint --prefix frontend`: passed.
- `npm.cmd run typecheck --prefix frontend`: passed.
- `npm.cmd test --prefix frontend -- --run`: **43 passed**.
- `npm.cmd run build --prefix frontend`: passed.

## Safety that is mandatory

These controls directly protect users from malformed files and untrusted model output:

- short-lived execution outside the chat/backend process;
- no filesystem, network, shell, package, or user-path authority exposed through the worker contract;
- bounded resources and reliable cancellation;
- byte-derived input/output validation and atomic publication; and
- visible status, errors, and recovery behavior.

The local worker is a practical containment boundary, not an operating-system security
sandbox or a claim of arbitrary-code isolation. The existing Phase 0/Phase 2
qualification code supplies additional implementation evidence, not a list of separate
product launches.

## Explicitly optional or out of scope

The following are **not** blockers for the open-source checkout or normal local profile:

- outside security review or an attestation authority;
- a production signing key, pinned public release root, or signed prebuilt worker;
- updater/rollback drills for an official installer;
- external red-team certification;
- Hyper-V, remote attestation, cloud workers, a workflow service, or a broker service;
- workspace write/commit support; and
- network egress.

Maintainers may add release hardening before distributing a trusted prebuilt binary,
but those controls must not be mixed into the contributor/product roadmap.

## Definition of done for the current goal - complete

The current goal is complete when:

1. ordinary chat remains usable if execution is disabled or fails;
2. the model can safely use scratch computation for explicit math requests when it improves an answer;
3. a user can transform an uploaded image through the normal UI;
4. progress, cancellation, errors, and results are understandable; and
5. focused safety and reliability tests pass on the supported Windows matrix.

The detailed capability ADRs remain useful technical reference. If they conflict with
this delivery scope, this document controls sequencing and what is considered a project
blocker.
