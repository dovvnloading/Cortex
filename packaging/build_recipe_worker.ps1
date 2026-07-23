param(
    [switch]$SkipDependencyInstall,
    [string]$SigningKeyPath,
    [string]$SigningKeyId,
    [string]$BundleVersion,
    [int]$Sequence,
    [string]$ManifestOutput
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

# PyInstaller emits an empty PEP 376 REQUESTED marker for some transitive
# distributions. It carries no runtime bytes and cannot be represented by the
# positive-size signed manifest, so remove only that known generated marker.
Get-ChildItem -LiteralPath (Split-Path -Parent $Executable) -Recurse -File -Filter "REQUESTED" |
    Where-Object { $_.Length -eq 0 -and $_.DirectoryName -like "*.dist-info" } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }

Write-Host "Recipe worker contract package ready: $Executable"

$SigningRequested = -not [string]::IsNullOrWhiteSpace($SigningKeyPath)
$SigningOptionsPresent = $SigningRequested -or
    -not [string]::IsNullOrWhiteSpace($SigningKeyId) -or
    -not [string]::IsNullOrWhiteSpace($BundleVersion) -or
    $Sequence -ne 0 -or
    -not [string]::IsNullOrWhiteSpace($ManifestOutput)
if (-not $SigningRequested) {
    if ($SigningOptionsPresent) {
        throw "SigningKeyPath is required when any signing option is supplied."
    }
    Write-Host "This output is intentionally unsigned and not launch-authorized."
    exit 0
}
if ([string]::IsNullOrWhiteSpace($SigningKeyId) -or
    [string]::IsNullOrWhiteSpace($BundleVersion) -or
    $Sequence -lt 1) {
    throw "Signing requires SigningKeyId, BundleVersion, and a positive Sequence."
}
if (-not (Test-Path -LiteralPath $SigningKeyPath -PathType Leaf)) {
    throw "The external signing key file was not found."
}
if ([string]::IsNullOrWhiteSpace($ManifestOutput)) {
    $ManifestOutput = Join-Path $RepositoryRoot "dist\recipe-runtime.manifest.json"
}
python tools/sign_recipe_worker.py `
    --source-root (Split-Path -Parent $Executable) `
    --private-key $SigningKeyPath `
    --key-id $SigningKeyId `
    --bundle-version $BundleVersion `
    --sequence $Sequence `
    --output-manifest $ManifestOutput
if ($LASTEXITCODE -ne 0) {
    throw "The recipe worker manifest signing gate failed."
}
Write-Host "Signed recipe worker manifest ready: $ManifestOutput"
Write-Host "Installation still requires the pinned public-key trust root and SignedBundleInstaller."
