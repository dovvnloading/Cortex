# ADR-0001 Phase 2 fixed-function recipe provider qualification

- **Status:** Qualification core implemented and verified; OS sandbox and lifecycle enablement remain blocked
- **Phase:** 2 - fixed-function image provider
- **Parent:** [Capability-tiered agentic execution harness](0001-capability-tiered-agentic-execution-harness.md)
- **Depends on:** [Phase 2 typed recipe contract](0001-phase2-recipe-contract.md) and [trusted artifact boundary](0001-phase2-artifact-boundary.md)
- **Scope:** Qualification-only decode/transform/re-encode core. It is not a production provider route.

## Context and research basis

The typed recipe and artifact boundary are now complete, but image bytes still need
format-aware decoding before a fixed-function transform can be considered safe. Image
decoders process attacker-controlled bytes in native libraries and can encounter
decompression bombs, malformed metadata, multi-frame inputs, truncated data, and
resource exhaustion. Pillow's current security guidance recommends independent byte
and format validation, an explicit format allowlist, treating decompression warnings as
errors, strict pixel/resource limits, metadata stripping, pinned dependencies, and an
isolated subprocess for the decoder. See the official [Pillow security guidance](https://pillow.readthedocs.io/en/stable/handbook/security.html),
[Image.open/verify/load reference](https://pillow.readthedocs.io/en/stable/reference/Image.html),
and [Python support matrix](https://pillow.readthedocs.io/en/stable/installation/python-support.html).
The repository pins the supported Pillow line as `>=12.3,<12.4`; signed bundle
hashing and runtime packaging remain a later sandbox gate and are not implied by this
Python dependency declaration.

## Decision

`RecipeImageProvider` is a qualification-only core. It is deliberately not imported
by the application lifecycle or exported as an execution API. A future `RecipeExecutor`
may call this core only after the Windows sandbox has been independently constructed
and attested.

### Input and decoder boundary

1. The input is immutable bytes plus an already parsed `ImageTransformPlan`; no path,
   filename, MIME claim, source text, network target, or plugin name is accepted.
2. Bytes are first checked by the trusted artifact MIME sniffer. Only PNG, JPEG, and
   WebP magic types are passed to Pillow, and Pillow receives the corresponding
   `formats` allowlist. TIFF, GIF, SVG, PDF, RAW, archives, executables, and active
   content are not accepted by this provider.
3. Pillow's `verify()` pass is followed by a fresh `load()` pass. Truncated images are
   disabled, decompression-bomb warnings are promoted to errors, and bomb/resource
   errors map to stable `resource_limit` failures.
4. Only one frame is accepted. Width, height, total pixels, encoded input bytes, and
   estimated decoded bytes are checked before and after pixel loading and after every
   transform step.

### Fixed-function transform and output boundary

The only operations are the schema's grayscale, contrast, brightness, crop, resize,
and rotate steps. Crop bounds are rechecked against the current image dimensions;
the provider never trusts the parser alone. Each step checks cancellation and produces
a fresh image object. There is no expression evaluation, dynamic import, native
extension selection, filesystem access, or network capability.

Outputs are encoded only as PNG, JPEG, or lossless WebP with fixed encoder settings.
Metadata containers are cleared and EXIF, ICC, XMP, comments, and PNG text are not
carried forward. The encoded bytes are then independently sniffed, reopened, fully
loaded, verified for format/dimensions/one-frame status, checked for metadata and size,
and hashed before a `RecipeProviderResult` is returned. The provider never writes a
file or publishes an artifact; the existing artifact boundary remains the only
publication path.

### Health and enablement gate

The provider starts disabled. `start()` requires an externally supplied available
`RuntimeHealth` from the future sandbox probe and a local Pillow capability check for
PNG/JPEG/WebP. Missing sandbox health, unavailable codecs, or any failed probe leaves
the provider disabled. There is no host-process, in-process `exec`, virtual-environment,
or weaker-provider fallback. `stop()` is monotonic and clears the enabled state.

The current `RuntimeHealth.ready()` test fixture is only a contract test. It is not an
attestation of an AppContainer/LPAC, Job Object, restricted handle set, signed bundle,
named-pipe identity, CPU watchdog, or memory limit. Those controls remain required
before this provider can be wired into `ExecutionLifecycle`.

## Qualification ceilings

| Resource | Ceiling |
| --- | ---: |
| Encoded input | 100 MiB |
| Encoded output | 128 MiB |
| Pixels | 64 megapixels |
| Width or height | 16,384 pixels |
| Estimated decoded memory | 256 MiB |
| Transform steps | 8 |
| Frames | 1 |

The ceilings are hard caps in `RecipeProviderLimits`; callers cannot raise them by
configuration. CPU time, process-tree lifetime, and OS memory enforcement are not
implemented by this core and therefore cannot satisfy the release gate.

## Failure contract

Only stable categories cross the provider boundary: `provider_disabled`,
`sandbox_unverified`, `sandbox_unavailable`, `recipe_dependency_missing`,
`recipe_codec_unavailable`, `invalid_input`, `unsupported_format`,
`unsupported_frames`, `decode_failed`, `invalid_plan`, `resource_limit`,
`input_too_large`, `output_too_large`, `encode_failed`, `output_invalid`,
`output_metadata_present`, `cancelled`, and `cancellation_check_failed`.
Decoder messages, paths, bytes, OS errors, and stack traces are not returned.

## Verification and next gate

`tests/test_phase2_recipe_provider.py` covers disabled/health-gated startup,
allowlisted transforms, deterministic output hashes, metadata stripping, malformed and
active inputs, malformed JPEGs, animated WebP rejection, pixel/decoded-memory/output
limits, crop bounds, cancellation, stop behavior, and non-raiseable configuration caps.

The next gate is the [signed worker provenance binding](0001-phase2-worker-provenance.md)
followed by the disposable [Windows sandbox qualification harness](0001-phase2-sandbox-qualification.md).
The storage boundary now proves only the exact role and immutable bytes; the actual
provider worker must still launch out of process
under a signed bundle, AppContainer/LPAC and Job Object controls, restricted
handles/environment, native broker identity, watchdog, and accounting; run the hostile
decoder corpus there; then wire it to `ExecutionLifecycle` only after external review.
