# PyInstaller one-folder prototype for the Windows desktop target.

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
        str(ROOT / "Chat_LLM" / "Chat_LLM"),
    ],
    binaries=[],
    datas=[(str(FRONTEND_DIST), "frontend/dist")],
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
