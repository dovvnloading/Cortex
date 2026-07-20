param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "Cortex Windows packages must be built on Windows."
}

if (-not $SkipDependencyInstall) {
    python -m pip install --disable-pip-version-check -r requirements.txt
    python -m pip install --disable-pip-version-check "pyinstaller>=6.14,<7"
}

& (Join-Path $PSScriptRoot "prepare_webview2.ps1")
python main.py --build-frontend
python -m PyInstaller --noconfirm --clean packaging/Cortex.spec

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
