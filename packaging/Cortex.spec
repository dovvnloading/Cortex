# PyInstaller one-folder package for the Windows web runtime.

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parent
FRONTEND_DIST = ROOT / "frontend" / "dist"

if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit(
        "frontend/dist/index.html is missing; run `python main.py --build-frontend` first."
    )

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[
        str(ROOT),
        str(ROOT / "backend"),
    ],
    binaries=[],
    datas=[
        (str(FRONTEND_DIST), "frontend/dist"),
        (str(ROOT / "assets"), "assets"),
    ],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto"],
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
    name="Cortex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Cortex",
)
