# Keep the qualification input immutable. Updating this pin requires a reviewed
# change to this script and its hash, rather than an untrusted caller override.
$ApprovedWasmtimeVersion = "46.0.1"
$ApprovedWasmtimeSha256 = "559b0753e3ea311fd16000fe51c08592a625e61ebb8640601ae7173fc516e430"

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$TempRoot = Join-Path $env:TEMP ("cortex-wasmtime-phase0-" + [guid]::NewGuid().ToString("N"))
$DownloadRoot = Join-Path $TempRoot "download"
$VenvRoot = Join-Path $TempRoot "venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null

Write-Host "Creating disposable Python environment: $VenvRoot"
& python -I -m venv $VenvRoot
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "Could not create the disposable Python environment."
}

Write-Host "Downloading pinned Wasmtime wheel into disposable directory: $TempRoot"
& $VenvPython -I -m pip download `
    --disable-pip-version-check `
    --no-deps `
    --only-binary=:all: `
    --dest $DownloadRoot `
    ("wasmtime==" + $ApprovedWasmtimeVersion)
if ($LASTEXITCODE -ne 0) {
    throw "Could not download the approved Wasmtime wheel."
}

$wheels = @(Get-ChildItem -LiteralPath $DownloadRoot -Filter ("wasmtime-" + $ApprovedWasmtimeVersion + "-*.whl") -File)
if ($wheels.Count -ne 1) {
    throw "Expected exactly one approved Wasmtime wheel, found $($wheels.Count)."
}
$wheel = $wheels[0]

$actualSha256 = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "Wasmtime wheel: $($wheel.Name)"
Write-Host "Wasmtime wheel SHA-256: $actualSha256"
if ($actualSha256 -ne $ApprovedWasmtimeSha256) {
    throw "Wasmtime wheel hash mismatch."
}

& $VenvPython -I -m pip install `
    --disable-pip-version-check `
    --no-deps `
    --force-reinstall `
    $wheel.FullName
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the approved Wasmtime wheel in the disposable environment."
}

$verificationJson = & $VenvPython -I -c @'
import importlib.metadata
import importlib.util
import json

spec = importlib.util.find_spec("wasmtime")
if spec is None or not spec.origin:
    raise RuntimeError("Wasmtime import spec is unavailable")
print(json.dumps({
    "origin": spec.origin,
    "version": importlib.metadata.version("wasmtime"),
}))
'@
if ($LASTEXITCODE -ne 0) {
    throw "Could not verify the Wasmtime import in the disposable environment."
}
$verification = $verificationJson | ConvertFrom-Json
$venvRootFull = [IO.Path]::GetFullPath($VenvRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
$originFull = [IO.Path]::GetFullPath([string]$verification.origin)
if (-not $originFull.StartsWith($venvRootFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Wasmtime resolved outside the disposable environment."
}
if ([string]$verification.version -ne $ApprovedWasmtimeVersion) {
    throw "Wasmtime version verification failed."
}

$reportJson = & $VenvPython -I (Join-Path $RepositoryRoot "tools\execution_spikes\phase0_probe.py") `
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

Write-Host "Pinned Wasmtime, runtime-control, AppContainer, AssemblyScript guest-language, and cancellation smoke passed; Phase 0 is green. Disposable files retained for inspection at: $TempRoot"
