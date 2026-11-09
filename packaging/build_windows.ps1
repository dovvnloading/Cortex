param(
    [switch]$SkipDependencyInstall,
    # Explicit development-only escape hatch. Normal packaging matches the
    # release/Quality runtime lock and must never re-resolve loose ranges.
    [switch]$UnlockedDependencies
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot
$RequiredPyInstallerVersion = "6.14.2"

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "Cortex Windows packages must be built on Windows."
}

if ($SkipDependencyInstall -and $UnlockedDependencies) {
    throw "Choose either -SkipDependencyInstall or -UnlockedDependencies, not both."
}

if (-not $SkipDependencyInstall) {
    if ($UnlockedDependencies) {
        Write-Warning "UNLOCKED DEVELOPMENT BUILD: dependency versions may differ from CI and release packages."
        python -m pip install --disable-pip-version-check -r requirements.txt
    } else {
        # The lock was resolved for Python 3.11 (not an arbitrary local version).
        # Avoid silently creating a package that release CI never tested.
        $PythonVersion = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -ne 0 -or ($PythonVersion -join "").Trim() -ne "3.11") {
            throw "Locked Cortex Windows packages require Python 3.11, matching release CI."
        }
        python -m pip install --disable-pip-version-check --require-hashes -r requirements.lock.txt
    }
    if ($LASTEXITCODE -ne 0) {
        throw "The dependency installation failed; refusing to package a stale application."
    }
    python -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Python dependencies are inconsistent; refusing to package."
    }
    python -m pip install --disable-pip-version-check "pyinstaller==$RequiredPyInstallerVersion"
    if ($LASTEXITCODE -ne 0) {
        throw "The PyInstaller installation failed; refusing to package a stale application."
    }
} else {
    Write-Warning "Skipping dependency installation: the existing environment is NOT verified against the release lock."
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

# Verify the result itself, not merely that PyInstaller exited with zero.
# This mirrors the Quality/release headless health smoke and uses isolated data.
$smokeData = Join-Path $env:TEMP ("cortex-package-smoke-" + [guid]::NewGuid().ToString("N"))
$smokeProcess = $null
$smokeReady = $false
try {
    $smokeProcess = Start-Process -FilePath $executable -ArgumentList @("--headless", "--data-dir", $smokeData) -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $smokeProcess.Refresh()
        if ($smokeProcess.HasExited) {
            throw "Packaged Cortex exited before its health endpoint became ready (code $($smokeProcess.ExitCode))."
        }
        $recordPath = Join-Path $smokeData "cortex.instance.json"
        if (Test-Path -LiteralPath $recordPath -PathType Leaf) {
            try {
                $record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
                $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$($record.port)/api/v1/health/ready"
                if ($response.StatusCode -eq 200) {
                    $smokeReady = $true
                    break
                }
            } catch {
                # The instance record can precede the loopback server bind.
            }
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $smokeReady) {
        throw "Packaged Cortex did not become healthy within 30 seconds."
    }
} finally {
    if ($null -ne $smokeProcess) {
        $smokeProcess.Refresh()
        if (-not $smokeProcess.HasExited) {
            Stop-Process -Id $smokeProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
    if (Test-Path -LiteralPath $smokeData) {
        Remove-Item -LiteralPath $smokeData -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Cortex Windows package ready: $executable"
