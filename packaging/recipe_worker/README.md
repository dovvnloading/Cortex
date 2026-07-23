# Recipe worker package

This directory defines the fixed-function worker package boundary. The package
entrypoint accepts only the reviewed native broker launch shape (protected pipe,
expected broker PID, installation principal, and exact job ID). Direct launches,
malformed identity, transport failures, provider failures, and watchdog expiry
return status `78`; there is no host-process, shell, path, or stdio fallback.

Build it on a supported Windows machine with:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_recipe_worker.ps1
```

The output is `dist/recipe-runtime/recipe_worker.exe`. It is a dependency-closure
artifact for qualification only and is **not signed or launch-authorized** by this
repository. The build remains unsigned by default. A release build may opt into the
external-key signing boundary:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_recipe_worker.ps1 `
  -SigningKeyPath C:\secure\recipe-worker-release.key `
  -SigningKeyId release-2026 `
  -BundleVersion 1.0.0 `
  -Sequence 1
```

The key file must be an external raw 32-byte Ed25519 private key. The signer:

1. hash every immutable package entry;
2. creates one `recipe.manifest.v1` entry per file, marking `recipe_worker.exe` as
   the sole `image_transform` role and all dependencies as inert `resource` entries;
3. signs and self-verifies the canonical manifest with the supplied release key; and
4. leaves installation to `SignedBundleInstaller` with its independently pinned
   public-key trust root before `verify_active_worker()` can be considered.

The private key is never persisted, emitted, or committed. A generated manifest is
release metadata, not launch authorization.

The native launcher and live broker PID/AppContainer-token binding remain required
before this package can perform any transform. The worker loop itself is covered
by `tests/test_phase2_worker_runtime.py`, including in-flight cancellation and
watchdog cleanup.
