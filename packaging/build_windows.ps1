$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)
python main.py --build-frontend
python -m PyInstaller --noconfirm --clean packaging/Cortex.spec

$executable = Join-Path (Get-Location) "dist\Cortex\Cortex.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "PyInstaller did not produce the expected one-folder executable: $executable"
}

Write-Host "Cortex Windows package ready: $executable"
