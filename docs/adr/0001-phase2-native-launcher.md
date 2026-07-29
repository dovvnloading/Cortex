# ADR-0001 Phase 2 disposable native launcher qualification

- **Status:** Win32 suspended factory, broker identity binder, and signed-worker end-to-end qualification complete; official release trust is optional hardening
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Windows sandbox qualification](0001-phase2-sandbox-qualification.md) and [signed worker provenance](0001-phase2-worker-provenance.md)
- **Scope:** Qualify construction order and resource-policy application with fixed benign Windows executables only.

## Decision

`tools/execution_spikes/native_launcher_qualification.py` exercises the reviewed
native construction sequence without accepting a worker path or user input. The
production boundary in `backend/cortex_backend/execution/native_launcher.py` now
accepts only an installer-verified worker and a trusted `BrokerWorkerBinding`:

1. recheck the active signed generation through `verify_active_worker()`;
2. revalidate the fixed `recipe_worker.exe` identity, size, link count, and hash
   immediately before launch planning;
3. construct a fixed command line containing only the native-broker endpoint,
   expected broker PID, installation principal, and job ID;
4. require a reviewed native process factory and live broker binder before any
   process is created; and
5. enforce the order `create suspended -> apply Job policy -> bind worker PID and
   AppContainer SID -> resume`.

`NativeWin32ProcessFactory` now implements the reviewed factory: it creates a
unique zero-capability AppContainer profile, starts the fixed executable suspended,
verifies the process token is an AppContainer with the expected SID shape, and
returns a handle that applies/query-verifies the active-process, CPU, memory,
kill-on-close, and no-breakaway Job Object policy before resume. Cleanup terminates
unassigned processes, closes the kill-on-close Job, waits for reaping, closes all
handles, and deletes the disposable profile.

`NativeBrokerIdentityBinder` now creates a protected `NativeBrokerServer` whose
peer policy contains the actual suspended worker PID and AppContainer SID. It also
requires the broker PID to equal the current server process, pins the installation
principal/job, and exposes accept/close ownership to the coordinator. The launcher
closes the endpoint if binding or resume fails.

The disposable qualification helper still exercises the lower-level construction
sequence with a fixed benign executable:

1. create a unique zero-capability AppContainer profile;
2. create a fixed `findstr.exe` child suspended with the AppContainer token;
3. assign it to a Job Object before resume;
4. apply kill-on-close, active-process, process/job CPU-time, and process/job
   memory limits, with breakaway flags absent;
5. resume only after policy configuration;
6. query the Job Object policy/accounting and require every configured value; and
7. close all handles and remove the disposable profile/marker.

The lower-level `appcontainer_smoke._run_child()` now exposes bounded optional
resource-limit parameters and returns the queried policy plus actual basic CPU,
memory, process, page-fault, and I/O accounting. The deterministic
`resource_watchdog_qualification.py` corpus also exercises monotonic logical
budgets and the kill-on-close process-tree watchdog. Existing filesystem/network
and cancellation probes continue to use the same helper and remain green after
this change.

This is qualification evidence for control application, not approval to launch a
recipe worker. The fixed probe reports `provider_launch_authorized=false` until
the signed worker package is installed and the worker loop completes an end-to-end
authenticated broker session. The production launcher boundary also fails closed
when either adapter is absent; it never falls back to `subprocess`, a shell, stdio,
or a weaker sandbox.

## Evidence and limits

On the controlled Windows host (2026-07-29), the resource-policy check passed with:

- AppContainer token confirmed;
- active-process limit `1`;
- process memory `64 MiB`, job memory `128 MiB`;
- process CPU `2 s`, job CPU `4 s`;
- actual Job Object accounting fields present with non-negative CPU, memory,
  process, page-fault, and I/O values;
- kill-on-close present; and
- breakaway flags absent.

The policy and accounting values are queried before the Job Object handle closes.
The fixed probe and watchdog corpus do not yet prove enforcement against a real
signed recipe worker, resource-exhaustion behavior across supported Windows
versions, or external launcher review.

## Remaining release-hardening boundaries

- The open-source qualification path uses a disposable ephemeral signing key and
  temporary immutable installer generation; a production signed package and
  pinned trust root are still optional official-release material.
- External security review and release-specific resource/enforcement review remain
  separate official-release gates.

The disposable signed-worker qualification and the strict durable-coordinator
qualification now prove a real broker handshake and native process lifecycle on
Windows. Missing or unverifiable worker material still blocks the profile; there
is no host-process or weaker-sandbox fallback.

If any control is missing or cannot be verified, the provider remains unavailable;
there is no host-process or weaker-sandbox fallback.

## Verification

`tests/test_phase2_native_launcher.py` covers bounded policy values, no-breakaway
flags, trusted binding validation, worker revalidation, fixed command-line
construction, refusal before process creation without a binder, policy/bind/resume
ordering, cleanup on binding/resume failure, broker PID/AppContainer binding, and
tamper rejection. `tests/test_phase2_native_win32.py` creates a fixed suspended
AppContainer child on Windows, verifies its token, applies/query-verifies Job Object
policy, and closes it without resuming. `tests/test_native_launcher_qualification.py`
covers the disposable probe's
non-Windows blocking, report fail-closed behavior, and no-breakaway invariant. The
`resource_watchdog_qualification.py --json --strict` corpus additionally requires
actual accounting and full-tree reaping. The full repository suite and the existing
AppContainer/cancellation corpus are required before this boundary can be merged.
