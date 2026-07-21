param(
    [string]$Version = "46.0.1",
    [string]$ExpectedSha256 = "559b0753e3ea311fd16000fe51c08592a625e61ebb8640601ae7173fc516e430"
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TempRoot = Join-Path $env:TEMP ("cortex-wasmtime-phase0-" + [guid]::NewGuid().ToString("N"))
$DownloadRoot = Join-Path $TempRoot "download"
$SiteRoot = Join-Path $TempRoot "site"

New-Item -ItemType Directory -Force -Path $DownloadRoot, $SiteRoot | Out-Null

Write-Host "Downloading pinned Wasmtime wheel into disposable directory: $TempRoot"
python -m pip download `
    --disable-pip-version-check `
    --no-deps `
    --only-binary=:all: `
    --dest $DownloadRoot `
    ("wasmtime==" + $Version)

$wheel = Get-ChildItem -LiteralPath $DownloadRoot -Filter ("wasmtime-" + $Version + "-*.whl") |
    Select-Object -First 1
if ($null -eq $wheel) {
    throw "Pinned Wasmtime $Version wheel was not downloaded."
}

$actualSha256 = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Wasmtime wheel: $($wheel.Name)"
Write-Host "Wasmtime wheel SHA-256: $actualSha256"
if ($actualSha256 -ne $ExpectedSha256.ToLowerInvariant()) {
    throw "Wasmtime wheel hash mismatch. Expected $ExpectedSha256, got $actualSha256."
}

python -m pip install `
    --disable-pip-version-check `
    --no-deps `
    --target $SiteRoot `
    $wheel.FullName

$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $SiteRoot
    $reportJson = python (Join-Path $RepositoryRoot "tools\execution_spikes\phase0_probe.py") `
        --json `
        --job-smoke `
        --ipc-smoke `
        --appcontainer-smoke `
        --guest-language-smoke `
        --cancellation-smoke `
        --wasi-smoke
    if ($LASTEXITCODE -ne 0) {
        throw "Pinned Wasmtime smoke probe failed with exit code $LASTEXITCODE."
    }
    $report = $reportJson | ConvertFrom-Json
    $requiredPasses = @(
        "appcontainer_process_isolation_smoke",
        "wasmtime_guest_runtime",
        "wasmtime_runtime_controls",
        "guest_language_qualification",
        "containment_cancellation_corpus"
    )
    foreach ($name in $requiredPasses) {
        $check = @($report.checks | Where-Object { $_.name -eq $name })
        if ($check.Count -ne 1 -or $check[0].status -ne "pass") {
            throw "Pinned Wasmtime/AppContainer prerequisite $name did not pass."
        }
    }
    if ($report.phase0_status -ne "pass" -or -not $report.phase0_ready_for_phase1) {
        throw "Phase 0 did not close cleanly: status=$($report.phase0_status), ready=$($report.phase0_ready_for_phase1)."
    }
}
finally {
    if ($null -eq $oldPythonPath) {
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONPATH = $oldPythonPath
    }
}

Write-Host "Pinned Wasmtime, runtime-control, AppContainer, AssemblyScript guest-language, and cancellation smoke passed; Phase 0 is green. Disposable files retained for inspection at: $TempRoot"
