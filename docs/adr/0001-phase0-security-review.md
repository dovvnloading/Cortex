# ADR-0001 Phase 0 security review

- **Review status:** Complete for Phase 0 disposable spikes; production execution
  remains prohibited until later release gates.
- **Date:** 2026-07-21
- **Scope:** Phase 0 evidence under `tools/execution_spikes/` only.
- **Reviewer:** Cortex maintainers (implementation review; external review remains
  a Phase 3 requirement before arbitrary scratch code is released).

This review checks that the Phase 0 work does not accidentally create an execution
path in Cortex and that each disposable probe fails closed when a prerequisite or
containment property is missing. It is not approval to execute model-generated code.

## Invariants reviewed

| Threat | Required invariant | Evidence | Result |
| --- | --- | --- | --- |
| Host filesystem disclosure | The child has no host-file capability; a parent marker is unreadable | Native zero-capability AppContainer helper; `findstr.exe` returns nonzero and `is_appcontainer=true` | Pass |
| Network/loopback disclosure | The child cannot connect to a parent listener | Native helper; no loopback accept and `curl.exe` times out | Pass |
| Sandbox escape by weaker fallback | Missing native controls produce `blocked`/`fail`, never a host-process fallback | `phase0_probe.py` required-check set and strict exit code | Pass |
| Host imports/capability injection | An imported host function cannot instantiate against an empty import list | Fixed Wasmtime no-import control plus AssemblyScript module import count `0` | Pass |
| Resource exhaustion | Fuel and memory budgets reject fixed infinite/oversized modules | Fixed Wasmtime fuel/memory probes plus AssemblyScript `spin()` fuel trap | Pass |
| Guest supply-chain/native loading | Guest compiler/runtime inputs are pinned and contain no native addon files | AssemblyScript `0.28.19` tarball SHA-512, npm lock integrities, and `.node`/`.dll`/`.exe` scan | Pass |
| Child outlives cancellation | Job Object kill-on-close is deterministic | Hostile AppContainer corpus observed four process IDs and reaped all in 11–15 ms | Pass |
| IPC confused deputy | Disposable transport is authenticated and bounded | 20-byte authenticated `AF_PIPE` smoke; production ACL/schema review is deferred to Phase 1 implementation | Partial |
| Model/user input reaches probe | Probe commands and module text are constants; no user paths or model text accepted | Source review of all files under `tools/execution_spikes` | Pass |
| Production exposure | Cortex imports no spike code and packaging excludes it | Repository/package cross-check and PyInstaller build | Pass |

## Decision

The review passes the narrow safety properties demonstrated by the disposable
spikes. AssemblyScript is accepted as the Phase 0 Windows baseline guest
language, and the hostile cancellation corpus closes the Phase 0 containment
gate. Production IPC/ACL design, staging/publish, updater, and external review
remain later implementation/release gates. The probe therefore reports
`phase0_ready_for_phase1=true`, but no production execution capability is enabled.

No production execution code, model prompt changes, arbitrary command path, or
runtime dependency was added as part of this review.

## Required follow-up before any real execution provider

1. Review the production broker's named-pipe ACL, framing limits, peer identity,
   staging/publish rules, and update/rollback procedure.
2. Obtain the required external launcher/IPC/policy review before release.
