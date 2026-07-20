# PyInstaller one-folder package for the Windows native web runtime.

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parent
FRONTEND_DIST = ROOT / "frontend" / "dist"
WEBVIEW2_BOOTSTRAPPER = (
    ROOT / "packaging" / ".runtime" / "webview2" / "MicrosoftEdgeWebview2Setup.exe"
)

if not (FRONTEND_DIST / "index.html").is_file():
    raise SystemExit(
        "frontend/dist/index.html is missing; run `python main.py --build-frontend` first."
    )
if not WEBVIEW2_BOOTSTRAPPER.is_file():
    raise SystemExit(
        "The signed WebView2 bootstrapper is missing; run "
        "`powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1`."
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
        (str(WEBVIEW2_BOOTSTRAPPER), "webview2"),
    ],
    hiddenimports=[
        "clr",
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
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
    name="Cortex",
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
    name="Cortex",
)
