param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot
$RequiredPyInstallerVersion = "6.14.2"

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "Cortex Windows packages must be built on Windows."
}

if (-not $SkipDependencyInstall) {
    python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "The dependency installation failed; refusing to package a stale application."
    }
    python -m pip install --disable-pip-version-check "pyinstaller==$RequiredPyInstallerVersion"
    if ($LASTEXITCODE -ne 0) {
        throw "The PyInstaller installation failed; refusing to package a stale application."
    }
}

$PyInstallerVersion = python -c "import PyInstaller; print(PyInstaller.__version__)"
$PyInstallerVersionExitCode = $LASTEXITCODE
if ($PyInstallerVersionExitCode -ne 0) {
    throw "PyInstaller could not be imported; refusing to package a stale application."
}
if (($PyInstallerVersion -join "`n").Trim() -ne $RequiredPyInstallerVersion) {
    throw "The resolved PyInstaller version is not $RequiredPyInstallerVersion; refusing to package a stale application."
}
Write-Host "Resolved PyInstaller version for packaging: $RequiredPyInstallerVersion"

& (Join-Path $PSScriptRoot "prepare_webview2.ps1")
python main.py --build-frontend
if ($LASTEXITCODE -ne 0) {
    throw "Cortex frontend build failed; refusing to package a stale bundle."
}

python -m PyInstaller --noconfirm --clean packaging/Cortex.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed to produce the Cortex Windows package."
}

$executable = Join-Path (Get-Location) "dist\Cortex\Cortex.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "PyInstaller did not produce the expected one-folder executable: $executable"
}

$bootstrapper = Get-ChildItem -LiteralPath (Join-Path (Get-Location) "dist\Cortex") -Recurse -File |
    Where-Object { $_.Name -eq "MicrosoftEdgeWebview2Setup.exe" } |
    Select-Object -First 1
if ($null -eq $bootstrapper) {
    throw "The packaged application is missing its WebView2 bootstrapper."
}

$signature = Get-AuthenticodeSignature -LiteralPath $bootstrapper.FullName
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $signature.SignerCertificate.Subject -notmatch "(^|, )O=Microsoft Corporation(,|$)") {
    throw "The packaged WebView2 bootstrapper failed signature verification."
}

Write-Host "Cortex Windows package ready: $executable"
