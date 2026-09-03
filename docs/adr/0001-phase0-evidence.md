# ADR-0001 Phase 0 evidence log

- **Phase:** 0 — threat model and executable spikes
- **Status:** Complete; the Phase 0 exit gate is met
- **Started:** 2026-07-21
- **Owner:** Cortex maintainers
- **Source decision:** [ADR-0001](0001-capability-tiered-agentic-execution-harness.md)
- **Probe source:** [`tools/execution_spikes/phase0_probe.py`](../../tools/execution_spikes/phase0_probe.py)

This log is updated alongside the work. A check is marked complete only when the
probe or repository evidence proves the exact property named by the ADR. API
availability alone is not accepted as proof of process containment.

## Phase 0 checklist

| ADR requirement | Status | Evidence |
| --- | --- | --- |
| Build a throwaway Wasmtime/WASI spike | **Complete (initial)** | Pinned PyPI wheel `wasmtime==46.0.1` was hash-verified and the fixed module returned `42` in a disposable target. |
| Build an AppContainer/LPAC spike | **Complete (AppContainer baseline)** | Native helper creates a unique zero-capability AppContainer, verifies `IsAppContainer=true`, denies a parent marker file, denies a loopback listener, and deletes the profile. LPAC remains an open matrix question. |
| Build a Job Object spike | **Complete** | Fixed benign child was assigned to a Job Object with kill-on-close and terminated when the handle closed. |
| Build a bounded IPC spike | **Complete** | Fixed child exchanged one authenticated 20-byte frame through a named pipe. |
| Build a PyInstaller/package spike | **Complete (initial)** | Existing spec/build inputs were completed with the signed WebView2 bootstrapper; the one-folder build finished successfully. |
| Qualify a guest runtime | **Complete (AssemblyScript baseline)** | Pinned Wasmtime executed the fixed `42` module and no-import/fuel/memory controls. AssemblyScript `0.28.19` was hash/integrity verified, compiled deterministic TypeScript-like guest code with zero host imports/native dependency files, and trapped a guest infinite loop under fuel. |
| Define Windows support and health probes | **Complete (initial)** | Probe records Windows version, architecture, Python, required Win32 API surfaces, package preconditions, and explicit blocked states. |
| Complete security review before production execution | **Complete (pre-production spike review)** | [`0001-phase0-security-review.md`](0001-phase0-security-review.md) reviews threats, fail-closed behavior, and deferred production-boundary work; it does not authorize arbitrary execution. |
| Phase 0 exit gate: hostile corpus cannot access host/network or outlive cancellation | **Complete** | AppContainer host/network denial passes; the hostile four-process AppContainer corpus was cancelled through Job Object close and every observed PID was reaped in 11–15 ms. |
| Phase 0 exit gate: packaging and startup health are reliable | **Complete (initial)** | Signed WebView2 bootstrapper was prepared and `python -m PyInstaller --noconfirm --clean packaging\\Cortex.spec` completed successfully. |

## Evidence captured

### Repository cross-check — complete

The ADR rollout was cross-checked against the repository before adding code:

- Cortex is Windows-first and packaged with PyInstaller.
- The existing package build requires `packaging/.runtime/webview2/MicrosoftEdgeWebview2Setup.exe`.
- No execution runtime or execution-spike harness existed under `backend`, `tools`,
  or `tests`.
- Existing production jobs remain untouched. The probe is isolated under
  `tools/execution_spikes` and is not imported by the application.

### Host and API surface — complete

Command:

```powershell
python tools/execution_spikes/phase0_probe.py --json
```

Observed host:

```text
Windows 11 / 10.0.26200
AMD64
Python 3.14.0 at C:\Python314\python.exe
```

The following required exports were present:

- `userenv.dll`: `CreateAppContainerProfile`, `DeleteAppContainerProfile`,
  `DeriveAppContainerSidFromAppContainerName`;
- `kernel32.dll`: Job Object lifecycle functions; and
- `kernel32.dll`: named-pipe lifecycle functions.

These are only API-surface checks. They do not authorize production use.

### Job Object kill-on-close smoke — complete

Command:

```powershell
python tools/execution_spikes/phase0_probe.py --json --job-smoke
```

Result: **pass**. A fixed child running `time.sleep(30)` was assigned to a Job
Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; closing the Job
Object handle caused the child to terminate. No model input or user file was
used.

### Native AppContainer containment smoke — complete (baseline)

Command:

```powershell
python tools/execution_spikes/appcontainer_smoke.py
```

Result: **pass**. The fixed native helper created a unique zero-capability
AppContainer profile and started `findstr.exe` and `curl.exe` with
`STARTUPINFOEX` security capabilities, no inherited handles, and a kill-on-close
Job Object. Both child tokens reported `IsAppContainer=true`; `findstr.exe`
could not open the parent-owned marker file, and `curl.exe` could not connect to a
parent loopback listener. The profile and marker were removed in `finally` paths.
This is an AppContainer baseline only; LPAC matrix qualification and cancellation
corpus timing remain open.

### AppContainer cancellation and full-tree corpus — complete

Command:

```powershell
python tools/execution_spikes/cancellation_corpus.py
```

Result: **pass**. A fixed AppContainer PowerShell launcher created a native
`choice.exe` descendant. The helper observed four process IDs in the Job Object,
closed the kill-on-close handle after a 1-second wall timeout, and verified every
observed process was terminated. Reaping completed in approximately 11–15 ms in
the captured runs. The shell is a fixed hostile-test launcher only; production
execution will not accept shell commands or user source through this path.

### Named-pipe IPC smoke — complete

Command:

```powershell
python tools/execution_spikes/phase0_probe.py --json --ipc-smoke
```

Result: **pass**. A fixed child exchanged the exact bounded payload
`cortex-phase0-ipc-v1` using a Windows `AF_PIPE` listener and an authentication
key. This proves only the disposable probe transport; the production broker
still requires a versioned schema, ACL verification, framing limits, and peer
identity checks.

### WASI/Wasmtime — complete (initial)

The official PyPI wheel `wasmtime==46.0.1` for Windows x86-64 was downloaded to
a unique disposable directory and verified before installation:

```text
SHA-256: 559b0753e3ea311fd16000fe51c08592a625e61ebb8640601ae7173fc516e430
```

The fixed module `(module (func (export "answer") (result i32) i32.const 42))`
executed and returned `42`. Fixed follow-up controls also rejected a host import,
trapped an infinite loop after fuel exhaustion, and rejected a two-page module
under a 64 KiB memory limit. These controls qualify the runtime policy only.

The AssemblyScript qualification helper additionally verified the pinned npm
tarball (`C8E02501...98E2AAF2`), dependency lock integrity, deterministic module
hash `491a4016664ffaaa7847c70c5d9fca5c5201da881402aacc28d98ab17b809c0d`, zero
imports, no `.node`/`.dll`/`.exe` dependency files, arithmetic results `42`, and
fuel trapping of a guest infinite loop. Process-tree cancellation is separately
proven by the native AppContainer corpus below.

### Guest-language selection — complete

The ADR's primary candidate is a bundled JavaScript runtime in WebAssembly. The
current Javy v9 release was reviewed from its official repository and release
assets; its Windows asset is ARM-only, so it was not accepted for this AMD64
baseline. AssemblyScript `0.28.19` was selected as the Phase 0 Windows baseline
because it is a TypeScript-like model-facing language with a deterministic Wasm
compiler and no native dependency files in the pinned install. This is a
qualification decision, not a production execution enablement.

### PyInstaller/package — complete (initial)

The repository contains the spec and build script, `frontend/dist/index.html` is
present, and PyInstaller 6.17.0 is importable. The existing preparation flow
downloaded and verified the signed WebView2 bootstrapper at:

```text
packaging/.runtime/webview2/MicrosoftEdgeWebview2Setup.exe
```

The bootstrapper SHA-256 was
`23A55FBFF920C0F99887848CFC25125F8F915DF35638E01BEB8F8FA9B5A0BC51`. The
one-folder PyInstaller build completed and produced `dist/Cortex/Cortex.exe`.
No execution-spike code is included in the package.

### Security review — complete (conditional)

[`0001-phase0-security-review.md`](0001-phase0-security-review.md) records the
threat-by-threat review of the disposable probes. It explicitly keeps production
execution disabled and lists the remaining guest-language, cancellation, and
production IPC review work.

### Regression validation — complete

The new probe and the existing repository suite were validated without enabling
production execution:

```text
python -m compileall -q tools/execution_spikes tests/test_phase0_probe.py
3 Phase 0 probe contract tests passed
91 repository tests passed
1 existing pytest-asyncio deprecation warning
```

The strict probe mode was checked after the guest-language and cancellation work.
It returns exit code `0` with every required check green and reports
`phase0_status: "pass"` and `phase0_ready_for_phase1: true`. This confirms the
Phase 0 gate cannot close before the complete corpus is present.

## Phase 0 closure and Phase 1 handoff

All Phase 0 required probes now report `pass`; Phase 1 may begin. Production IPC
ACL/framing, staging/publish, updater, and external review remain implementation
gates before any real execution provider is enabled.

## Re-run command

After the blockers are resolved, the complete Phase 0 command is:

```powershell
python tools/execution_spikes/phase0_probe.py `
  --json `
  --job-smoke `
  --ipc-smoke `
  --appcontainer-smoke `
  --guest-language-smoke `
  --wasi-smoke `
  --strict
```

The report now shows `phase0_status: "pass"` and
`phase0_ready_for_phase1: true`. This authorizes only the ADR’s Phase 1 fake
executor/durable workflow work; no production execution capability is enabled.
