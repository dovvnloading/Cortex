# ADR-0001 Phase 2 evidence log

- **Phase:** 2 — signed image recipes and calculator/check primitives
- **Status:** Typed contract foundation complete; provider and release gates remain open
- **Scope:** Provider-independent validation and deterministic trusted primitives only
- **Source decision:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Contract ADR:** [Phase 2 typed recipe and primitive contract](0001-phase2-recipe-contract.md)

## Stage checklist

| Deliverable | Status | Evidence |
| --- | --- | --- |
| Versioned image transform schema | **Complete (contract only)** | `artifact.transform.v1` allows only bounded grayscale, contrast, brightness, crop, resize, and rotate steps with PNG/JPEG/WebP output. |
| Opaque artifact binding | **Complete (validation only)** | Artifact IDs are bounded opaque identifiers; paths, source text, plugin names, and model-selected output names are rejected or absent. |
| Calculator/check primitives | **Complete (trusted pure helpers)** | Decimal-only calculator operations and explicit comparisons are deterministic, bounded, and have no I/O or code execution surface. |
| Canonical plan identity | **Complete** | Validated plans expose stable canonical JSON and SHA-256 digests for future idempotency/signature binding. |
| Signed recipe bundle | **Blocked / next gate** | No manifest, key verification, updater, or runtime bundle is trusted yet. |
| Copy-in, image decoding, output validation, publication | **Blocked / next gate** | No codec or provider path has been enabled; Phase 1 artifact storage remains the only publication mechanism. |
| Production broker and sandbox provider | **Blocked / next gate** | No named-pipe broker, Wasmtime, AppContainer, Job Object, subprocess, or production execution route was added. |

## Security invariants

1. Unknown fields and operations fail closed; no best-effort expression or command
   interpretation occurs.
2. Image plans contain no filesystem path, arbitrary filename, network target, or
   dynamic filter/plugin identifier.
3. Calculator inputs are finite bounded decimals; floats, non-finite values, division
   by zero, and result overflow/precision exhaustion fail closed.
4. Comparison semantics are explicit; tolerance exists only for `is_close` and must be
   positive.
5. Validation and evaluation errors expose stable safe categories only.
6. Canonical digests identify accepted plans but grant no capability and verify no
   signature.
7. The Phase 1 application lifecycle remains explicitly disabled for production
   execution; this stage cannot make a provider visible by itself.

## Re-run target

```powershell
python -m pytest tests/test_phase2_recipe_contract.py -q
python -m compileall -q backend\cortex_backend\execution tests
python -m pytest -q
python tools/generate_contracts.py
npm.cmd run lint --prefix frontend
npm.cmd run typecheck --prefix frontend
npm.cmd run build --prefix frontend
npm.cmd test --prefix frontend -- --run
```

**Validation result (2026-07-21):** 16 Phase 2 contract tests and the full Python suite
passed (139 tests total) with one pre-existing `pytest-asyncio` deprecation warning.
Frontend lint, typecheck, production build, and all 39 frontend tests passed. Contract
generation, compileall, and `git diff --check` passed. No production execution
provider is enabled.
