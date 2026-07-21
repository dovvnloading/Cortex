# Cortex execution Phase 0 spikes

This directory contains non-production probes for ADR-0001. Nothing here is
imported by `backend/cortex_backend`, the launcher, the frontend, or the
packaging entry point. The probes do not execute model-generated code and do not
provide a fallback execution path.

## Run the prerequisite report

From the repository root on Windows:

```powershell
python tools/execution_spikes/phase0_probe.py --json --job-smoke --ipc-smoke --appcontainer-smoke --guest-language-smoke --cancellation-smoke
```

Add `--wasi-smoke` only after the optional, pinned Wasmtime spike dependency is
installed in a disposable development environment. The smoke module is fixed
Wasm that returns the integer `42`; it is not a model or user input. The helper
script `run_pinned_wasmtime_smoke.ps1` verifies the wheel hash and performs this
qualification without changing Cortex dependencies.

Use `--strict` when a CI or release gate should return exit code `2` for any
required check that is blocked or fails:

```powershell
python tools/execution_spikes/phase0_probe.py --json --job-smoke --ipc-smoke --appcontainer-smoke --guest-language-smoke --cancellation-smoke --wasi-smoke --strict
```

## What the probes prove

- `environment`: supported Windows host and interpreter metadata.
- `appcontainer_api_surface`: required `userenv.dll` profile API exports exist.
- `appcontainer_process_isolation_smoke`: the reviewed native helper starts fixed
  `findstr.exe` and `curl.exe` children in a zero-capability AppContainer, proves
  their token state, and checks denied parent-file and loopback access. It is
  intentionally separate from the application and accepts no model input.
- `job_object_api_surface`: required Job Object lifecycle API exports exist.
- `named_pipe_api_surface`: required named-pipe API exports exist.
- `job_object_kill_on_close_smoke`: a fixed benign child is terminated when its
  Job Object handle closes.
- `named_pipe_ipc_smoke`: a fixed child exchanges one authenticated,
  length-bounded frame over a local named pipe.
- `wasmtime_guest_runtime`: the pinned Wasmtime package can execute a fixed,
  side-effect-free module when explicitly requested.
- `wasmtime_runtime_controls`: fixed no-import, fuel, and memory-limit probes
  exercise the runtime policy; these are not a guest-language qualification.
- `guest_language_qualification`: pins AssemblyScript 0.28.19, verifies npm
  dependency integrity and no native compiler files, compiles deterministic
  TypeScript-like guest code, and runs it through Wasmtime with fuel.
- `containment_cancellation_corpus`: starts a fixed AppContainer launcher that
  creates a native descendant, closes the kill-on-close Job Object, and verifies
  every observed process ID is reaped.
- `security_review`: records the conditional Phase 0 spike review and residual
  blockers.
- `pyinstaller_package_preconditions`: all currently known one-folder package
  inputs are present before a build is attempted.

An API export check is not proof of AppContainer process isolation. The native
helper is evidence for this disposable smoke corpus only; it is not authorized
to launch guest runtimes or model-generated code. LPAC policy qualification,
resource limits, cancellation corpus, and the security review remain separate
Phase 0 gates.

## Safety rules

1. Do not point a probe at model output, uploaded files, user paths, or arbitrary
   commands.
2. Do not add this directory to application imports or PyInstaller hidden imports.
3. Do not change the probe to silently skip a failed containment check.
4. Keep package/runtime experiments in a disposable environment and record exact
   versions and hashes in the Phase 0 evidence log.
5. A blocked result is safer than a green result produced by a weaker fallback.
