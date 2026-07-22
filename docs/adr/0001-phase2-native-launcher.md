# ADR-0001 Phase 2 disposable native launcher qualification

- **Status:** Suspended-launch and Job Object policy spike complete; real worker launch blocked
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Windows sandbox qualification](0001-phase2-sandbox-qualification.md) and [signed worker provenance](0001-phase2-worker-provenance.md)
- **Scope:** Qualify construction order and resource-policy application with fixed benign Windows executables only.

## Decision

`tools/execution_spikes/native_launcher_qualification.py` exercises the reviewed
native construction sequence without accepting a worker path or user input:

1. create a unique zero-capability AppContainer profile;
2. create a fixed `findstr.exe` child suspended with the AppContainer token;
3. assign it to a Job Object before resume;
4. apply kill-on-close, active-process, process/job CPU-time, and process/job
   memory limits, with breakaway flags absent;
5. resume only after policy configuration;
6. query the Job Object policy/accounting and require every configured value; and
7. close all handles and remove the disposable profile/marker.

The lower-level `appcontainer_smoke._run_child()` now exposes bounded optional
resource-limit parameters and returns the queried policy/accounting. Existing
filesystem/network and cancellation probes continue to use the same helper and
remain green after this change.

This is qualification evidence for control application, not approval to launch a
recipe worker. The fixed probe reports `provider_launch_authorized=false` until
the signed worker package and broker identity gates pass.

## Evidence and limits

On the controlled Windows host (2026-07-22), the resource-policy check passed with:

- AppContainer token confirmed;
- active-process limit `1`;
- process memory `64 MiB`, job memory `128 MiB`;
- process CPU `2 s`, job CPU `4 s`;
- kill-on-close present; and
- breakaway flags absent.

The policy values are queried before the Job Object handle closes. This does not
yet prove enforcement against a real signed recipe worker, resource exhaustion
behavior across supported Windows versions, or external launcher review.

## Remaining blockers

- The fixed signed `recipe_worker.exe` package is not shipped at the immutable
  installer generation used by the launcher.
- The native broker transport is not yet bound to the launched worker PID and OS
  token by a single reviewed launcher transaction.
- Watchdog progress, output framing, staging ACLs, hostile decoder execution, and
  lifecycle health-gated wiring remain separate release gates.

If any control is missing or cannot be verified, the provider remains unavailable;
there is no host-process or weaker-sandbox fallback.

## Verification

`tests/test_native_launcher_qualification.py` covers non-Windows blocking, report
fail-closed behavior, and the no-breakaway policy invariant. The full repository
suite and the existing AppContainer/cancellation corpus are required before this
spike can be merged.
