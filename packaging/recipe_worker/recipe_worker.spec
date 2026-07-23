"""PyInstaller one-folder definition for the fixed recipe worker contract."""

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(ROOT / "packaging" / "recipe_worker" / "recipe_worker.py")],
    pathex=[str(ROOT), str(ROOT / "backend")],
    binaries=[],
    datas=[],
    hiddenimports=[
        "cortex_backend.execution.worker_protocol",
        "cortex_backend.execution.recipe_provider",
        "PIL",
        "PIL.Image",
        "PIL.ImageEnhance",
        "PIL.ImageFile",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name="recipe_worker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="recipe-runtime",
)
