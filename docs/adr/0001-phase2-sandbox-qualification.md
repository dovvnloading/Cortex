# ADR-0001 Phase 2 Windows recipe sandbox qualification

- **Status:** Qualification harness implemented; signed provider worker and release gate blocked
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Phase 2 recipe provider core](0001-phase2-recipe-provider.md), [signed bundle installation](0001-phase2-bundle-installation.md), [native broker adapter](0001-phase2-native-broker.md), and [trusted artifact boundary](0001-phase2-artifact-boundary.md)
- **Scope:** Disposable Windows control qualification only. No production provider or lifecycle route is enabled.

## Decision

The next gate is represented by `tools/execution_spikes/recipe_sandbox_qualification.py`.
The harness is deliberately fail-closed and has independent checks for:

1. it runs the reviewed zero-capability AppContainer isolation helper in a child
   process and requires token identity, parent-file denial, loopback denial, and
   bounded completion;
2. it runs the reviewed AppContainer/Job Object cancellation corpus in a child
   process and requires full process-tree reaping after watchdog cancellation;
3. it exercises a fixed allowlisted/hostile decoder corpus against the
   qualification-only Pillow core, while recording `sandboxed=false`; and
4. it requires the future fixed recipe worker package at the repository's fixed
   packaging location;
5. it reports per-worker CPU/memory/breakaway/accounting controls as blocked until
   the native launcher applies and verifies them; and
6. it reports end-to-end broker execution as blocked until the launcher binds the
   qualified transport to a signed worker's actual PID/AppContainer token and the
   packaged worker completes the authenticated client handshake and hostile corpus.

The fourth check is intentionally blocked in this stage. A directory, executable,
self-reported digest, or unverified manifest cannot authorize a launch. The future
implementation must verify the packaged trust root, signed manifest, every declared
byte, worker identity, and runtime version before the native launcher is allowed to
start the worker. Until that exists, `provider_launch_authorized` is always false.

The harness accepts no command, source text, uploaded path, network target, or model
input. It invokes only fixed repository helpers and fixed bytes. It is not imported
by `backend/cortex_backend`, not a PyInstaller hidden import, and not an execution
fallback.

## Control matrix

| Control | Evidence in this stage | Release interpretation |
| --- | --- | --- |
| AppContainer token and zero-capability denials | `appcontainer_smoke.py`, child report `recipe_appcontainer_control` | Required prerequisite; does not prove LPAC policy or provider launch identity |
| Job Object kill-on-close and tree cancellation | `cancellation_corpus.py`, child report `recipe_cancellation_control` | Required prerequisite; resource limits and accounting remain open |
| Suspended launch/resource policy | `native_launcher_qualification.py`, child report `recipe_native_launcher_policy` | Policy application/query passes for a fixed benign child; real worker enforcement/review remains open |
| Decoder hostile corpus | Fixed one-pixel PNG, truncated PNG, and active SVG against the core | Qualification-only evidence; not OS-sandbox evidence |
| Signed worker provenance | Storage-only `verify_active_worker()` role binding plus fixed package precondition | **Storage gate complete; launch remains blocked** until a packaged executable and native launcher exist |
| Broker identity and framed IPC | Native broker transport tests and ADR | Must be bound to the actual worker PID/token before launch |
| Lifecycle enablement | `ExecutionLifecycle` remains disabled by default; provider is not exported | No provider can become reachable from the application |

No single green smoke result closes the gate. A missing, failed, or unverified
control produces `blocked` or `fail`, and no weaker host-process path is attempted.

## Required future worker qualification

The remaining release gate must install a signed, pinned worker bundle and run the
existing native launcher/worker loop per attempt:

1. verifies the installed immutable generation and image-worker entrypoint;
2. creates private staging and grants only the sandbox identity and required
   system read/write handles;
3. starts the worker suspended under the intended LPAC/AppContainer policy;
4. applies Job Object kill-on-close, active-process, CPU-time, memory, and
   breakaway restrictions and records accounting;
5. binds the protected broker pipe to the expected PID and OS token identity;
6. resumes only after all checks pass, enforces wall-clock/progress watchdogs,
   and bounds every IPC frame; and
7. closes the job on completion or cancellation, validates output through the
   trusted artifact boundary, and removes staging with recoverable cleanup state.

If any step cannot be applied or verified, the provider remains unavailable. The
host process must never decode the input as a fallback.

## Verification performed

On the controlled Windows host (2026-07-21):

```powershell
python tools/execution_spikes/recipe_sandbox_qualification.py --strict --json
```

The AppContainer and cancellation controls passed, and the fixed decoder corpus
passed. The overall result was intentionally `blocked` because the signed worker
package is not shipped. This is the expected result for the current stage.

Regression coverage is in `tests/test_recipe_sandbox_qualification.py`, including
missing/unsigned worker refusal, helper timeout/evidence failure, and the invariant
that a blocked worker gate never authorizes provider launch.

## Consequences

This stage provides reproducible evidence for the controls that already exist and
prevents accidental false-green qualification. It does not claim decoder isolation,
LPAC capability policy, resource enforcement, signed runtime provenance, native
worker identity, or production readiness. Those remain explicit blockers before
the provider can be wired to lifecycle or exposed to any model/UI route.
