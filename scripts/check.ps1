<#
.SYNOPSIS
    Run Cortex's quality gates locally, the same ones .github/workflows/quality.yml runs.

.DESCRIPTION
    Catches failures on your machine in ~2 minutes instead of waiting on CI.

    Tiers:
      quick  (default) Lint, backend tests, contract drift, frontend types/lint/unit tests.
                       This is what the pre-push hook runs.
      full             Everything in quick, plus compileall, Playwright e2e, and the
                       frontend bundle build.

    Deliberately NOT included at any tier: PyInstaller packaging, the recipe-worker /
    coordinator qualification spikes, and WebView2 signature verification. Those take
    35+ minutes and need signing tooling -- leave them to CI or run them by hand.

.EXAMPLE
    ./scripts/check.ps1
    ./scripts/check.ps1 -Tier full
    ./scripts/check.ps1 -SkipFrontend
#>
[CmdletBinding()]
param(
    [ValidateSet('quick', 'full')]
    [string]$Tier = 'quick',
    [switch]$SkipBackend,
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path -Parent $PSScriptRoot
$frontend = Join-Path $repoRoot 'frontend'

$results = [System.Collections.Generic.List[object]]::new()

function Invoke-Step {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Body,
        [string]$WorkingDirectory = $repoRoot
    )

    Write-Host ''
    Write-Host "-> $Name" -ForegroundColor Cyan

    $started = Get-Date
    Push-Location $WorkingDirectory
    try {
        $global:LASTEXITCODE = 0
        & $Body
        $code = $LASTEXITCODE
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        $code = 1
    } finally {
        Pop-Location
    }

    $elapsed = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    $ok = ($code -eq 0)

    if ($ok) {
        Write-Host "   ok  ($elapsed s)" -ForegroundColor Green
    } else {
        Write-Host "   FAILED  (exit $code, $elapsed s)" -ForegroundColor Red
    }

    $results.Add([pscustomobject]@{ Name = $Name; Ok = $ok; Seconds = $elapsed })
}

Write-Host "Cortex local quality check -- tier: $Tier" -ForegroundColor White

if (-not $SkipBackend) {
    Invoke-Step 'Lint Python (ruff)' {
        python -m ruff check backend tests tools main.py
    }

    Invoke-Step 'Backend tests (pytest)' {
        python -m pytest -q
    }

    Invoke-Step 'API contracts are up to date' {
        # Regenerates in place, then fails if that produced a diff -- the same
        # check CI runs, and the usual cause of a red build after touching a
        # Pydantic model.
        python tools/generate_contracts.py
        if ($LASTEXITCODE -ne 0) { return }
        git diff --exit-code -- contracts/openapi.json contracts/cortex-api.ts
        if ($LASTEXITCODE -ne 0) {
            Write-Host '   Contracts were stale and have been regenerated. Commit them.' -ForegroundColor Yellow
        }
    }

    if ($Tier -eq 'full') {
        Invoke-Step 'Compile application modules' {
            python -m compileall -q main.py backend
        }
    }
}

if (-not $SkipFrontend) {
    if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
        Write-Host ''
        Write-Host 'frontend/node_modules missing -- running npm ci first.' -ForegroundColor Yellow
        Invoke-Step 'Install frontend dependencies' { npm ci } $frontend
    }

    Invoke-Step 'Frontend types (tsc)' { npm run typecheck } $frontend
    Invoke-Step 'Lint frontend (eslint)' { npm run lint } $frontend
    Invoke-Step 'Frontend unit tests (vitest)' { npm test -- --run } $frontend

    if ($Tier -eq 'full') {
        Invoke-Step 'Frontend browser tests (playwright)' {
            npm run e2e -- --workers=1
        } $frontend

        Invoke-Step 'Build frontend bundle' { npm run build } $frontend
    }
}

Write-Host ''
Write-Host ('-' * 58)

$failed = @($results | Where-Object { -not $_.Ok })
$total = [math]::Round(($results | Measure-Object -Property Seconds -Sum).Sum, 1)

foreach ($r in $results) {
    $mark = if ($r.Ok) { 'ok  ' } else { 'FAIL' }
    $color = if ($r.Ok) { 'Green' } else { 'Red' }
    Write-Host ("  {0}  {1,-42} {2,6}s" -f $mark, $r.Name, $r.Seconds) -ForegroundColor $color
}

Write-Host ('-' * 58)

if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) of $($results.Count) checks failed in ${total}s." -ForegroundColor Red
    exit 1
}

Write-Host "All $($results.Count) checks passed in ${total}s." -ForegroundColor Green
exit 0
