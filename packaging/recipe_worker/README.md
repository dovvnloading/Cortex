# Recipe worker package

This directory defines the fixed-function worker package boundary. The package
entrypoint deliberately exits with status `78` until the reviewed native broker
adapter is added. That refusal is a safety property: a package build must not
silently become a host-process or stdio execution fallback.

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
before this package can perform any transform.
