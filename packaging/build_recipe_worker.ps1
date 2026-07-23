param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "The recipe worker must be packaged on Windows."
}

if (-not $SkipDependencyInstall) {
    python -m pip install --disable-pip-version-check -r requirements.txt
    python -m pip install --disable-pip-version-check "pyinstaller>=6.14,<7"
}

Remove-Item -LiteralPath (Join-Path $RepositoryRoot "build\recipe_worker") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $RepositoryRoot "dist\recipe-runtime") -Recurse -Force -ErrorAction SilentlyContinue

python -m PyInstaller --noconfirm --clean packaging/recipe_worker/recipe_worker.spec

$Executable = Join-Path $RepositoryRoot "dist\recipe-runtime\recipe_worker.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "The recipe worker package did not produce recipe_worker.exe."
}

Write-Host "Recipe worker contract package ready: $Executable"
Write-Host "This output is intentionally unsigned and not launch-authorized."
