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
repository. Release packaging must, outside source control:

1. hash every immutable package entry;
2. create the exact `recipe.manifest.v1` entry with role `image_transform` and
   path `recipe_worker.exe`;
3. sign the canonical manifest with the pinned release key; and
4. install it through `SignedBundleInstaller` before `verify_active_worker()` can
   be considered.

The native launcher and live broker PID/AppContainer-token binding remain required
before this package can perform any transform. The worker loop itself is covered
by `tests/test_phase2_worker_runtime.py`, including in-flight cancellation and
watchdog cleanup.
