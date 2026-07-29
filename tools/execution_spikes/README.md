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

## Run the Phase 2 recipe sandbox qualification gate

The recipe harness runs only fixed repository helpers and fixed decoder bytes. It
does not accept model input and never falls back to host-process decoding:

```powershell
python tools/execution_spikes/recipe_sandbox_qualification.py --json
python tools/execution_spikes/recipe_sandbox_qualification.py --json --strict
python tools/execution_spikes/native_launcher_qualification.py
```

The expected result at this stage is `qualification_status=blocked`: the native
AppContainer and Job Object controls may pass, but the signed `recipe_worker.exe`
bundle is not shipped yet. A blocked worker-provenance check is intentional and
must remain blocking until trust-root verification, native worker identity, and
resource/watchdog enforcement are implemented.

The native launcher qualification prints a passing resource-policy subcheck when
the fixed suspended child receives and reports Job Object CPU/memory/active-process
limits with no breakaway flags. Its overall exit remains blocked until the signed
worker package is installed and the worker completes the live authenticated broker
handshake; launcher-side PID/AppContainer binding is now implemented separately.

The fixed worker protocol/package boundary can be qualified separately on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_recipe_worker.ps1
```

This produces `dist/recipe-runtime/recipe_worker.exe` and verifies dependency
closure only. The entrypoint accepts only the fixed native broker identity
arguments and exits with status `78` for direct or failed launches; the output is
unsigned and must not be installed or launched as a provider. The package and
runtime contracts are covered by `tests/test_phase2_worker_protocol.py` and
`tests/test_phase2_worker_runtime.py`.

The signed worker/AppContainer/broker qualification gate is a separate,
disposable end-to-end check:

```powershell
python tools/execution_spikes/recipe_worker_e2e_qualification.py --json
python tools/execution_spikes/recipe_worker_e2e_qualification.py --json --strict
# Optional: rerun one release-gate case in isolation.
python tools/execution_spikes/recipe_worker_e2e_qualification.py --case cancellation --json --strict
```

It creates an in-memory ephemeral Ed25519 trust root, signs the already-built
one-folder package, installs one immutable generation, verifies provenance,
launches the worker through the native AppContainer/job-policy factory, and
exercises a fixed PNG transform, truncated-PNG and active-SVG hostile decoder
cases, and an in-flight eight-step cancellation corpus over the authenticated
broker. It accepts no user files, model text, commands, or production trust
material. `--strict` returns exit code `2` unless every selected stage passes.
Use `--case` to rerun one case during diagnosis; the default runs the full
corpus. Full package closure verification can take several minutes on Windows;
the protocol timeout applies after launch and is fail-closed.

The current evidence result passes the complete packaged-worker corpus:
signed installation, provenance, AppContainer/job identity binding, broker
handshake, normal `collect_output`, hostile decoder rejection for truncated PNG
and active SVG bytes, and an in-flight `cancel_ack` after `input_complete`. The
native read path polls `PeekNamedPipe` with a bounded 5 ms wait before `ReadFile`,
keeping the worker's cancellation reader live without starving the provider
transform. Resource/watchdog accounting, external review, and production-lifecycle
release gates remain open; parser-fuzz and artifact-security evidence are qualified
below.

## Run the typed parser fuzz qualification

The parser probe is deterministic and bounded; it never executes a recipe or
accepts model/user input:

```powershell
python tools/execution_spikes/recipe_parser_fuzz.py --json
python tools/execution_spikes/recipe_parser_fuzz.py --iterations 2000 --seed 20260728 --json --strict
```

The fixed corpus mutates image, calculator, and check payloads, including
unknown fields, malformed operations, non-mapping values, oversized payloads,
control/unicode text, and invalid optional values. A green result requires only
typed models or stable redacted `RecipeValidationError` categories; unexpected
exceptions, unbounded budgets, or canonicalization failures return exit code 2.
The current evidence is 158 accepted payloads, 1,842 rejections, and zero
unexpected exceptions. This closes parser-fuzz evidence only; resource/watchdog,
external-review, and production-lifecycle gates remain open.

## Run the artifact security review qualification

The artifact review is deterministic and disposable. It uses fixed bytes and a
temporary root to exercise the trusted copy-in/publication boundary; it never
accepts user/model input, opens a provider, or executes a file:

```powershell
python tools/execution_spikes/artifact_security_review.py --json
python tools/execution_spikes/artifact_security_review.py --json --strict
```

The `artifact-boundary-review.v1` corpus has 12 cases covering owner and ADS/path
binding, active UTF-8/UTF-16 and non-finite content, hardlink/symlink rejection,
exact output claims, quarantine, all-or-nothing rollback, and repository stored-size
integrity. The current evidence is 12/12 passed with corpus digest
`a748cc9f0a514c8d`. `--strict` returns exit code `2` for any failure or for an
unavailable required link primitive; a blocked result is not a release pass.

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
- `recipe_sandbox_qualification`: composes the native isolation/cancellation
  controls with the qualification-only decoder corpus and a mandatory signed
  worker provenance gate; it never authorizes a host-process fallback.
- `native_launcher_qualification`: creates only a fixed suspended `findstr.exe`
  child, applies and queries Job Object resource policy before resume, and reports
  the signed-worker and broker-binding blockers without launching either.
- `recipe_worker_e2e_qualification`: signs and installs a disposable worker
  generation, proves the live native broker identity boundary, and runs the fixed
  packaged-worker protocol corpus; any provider or cleanup timeout remains
  blocking.
- `backend/cortex_backend/execution/native_launcher.py`: production-facing
  launch-plan boundary that revalidates the signed worker and refuses process
  creation until a reviewed native process factory and live broker binder are
  supplied. It does not provide a fallback launcher.
- `backend/cortex_backend/execution/native_win32.py`: reviewed Windows factory
  that creates a suspended zero-capability AppContainer child, verifies its token,
  applies/query-verifies Job Object policy, and cleans up every handle on failure.
- `worker_protocol`: validates the future worker's bounded request state machine,
  in-order hashed chunks, cancellation, and redacted output contract; it has no
  filesystem, process, or transport capability.
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
6. Do not add the recipe qualification harness or any worker bundle to application
   imports, model tools, or PyInstaller hidden imports before its release gate is
   explicitly closed.
