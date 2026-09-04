<#
.SYNOPSIS
    Run Cortex's quality gates locally, including the fast gates in
    .github/workflows/quality.yml.

.DESCRIPTION
    Catches failures on your machine in ~2 minutes instead of waiting on CI.

    Tiers:
      quick  (default) Lint, backend tests, contract drift, security/watchdog
                       qualification, and frontend types/lint/unit tests. This is
                       what the pre-push hook runs.
      full             Everything in quick, plus compileall, Playwright browser
                       installation/e2e tests, and the frontend bundle build.

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

# README.md warns that PowerShell's execution policy can block npm's .ps1 shim.
# Prefer npm.cmd where it exists so this script works on a default machine.
$npm = if (Get-Command npm.cmd -ErrorAction SilentlyContinue) { 'npm.cmd' } else { 'npm' }

$results = [System.Collections.Generic.List[object]]::new()
# Distinct from any real tool exit code so a skip is never mistaken for one.
$SKIPPED_EXIT_CODE = 77

if ($SkipBackend -and $SkipFrontend) {
    throw 'At least one check surface must be enabled; refusing to run zero checks.'
}

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
    # A step can report SKIPPED_EXIT_CODE to mean "this machine cannot run
    # me". That is neither a pass nor a failure: it must not block a push, but
    # it must stay visible in the summary rather than looking green.
    $skipped = ($code -eq $SKIPPED_EXIT_CODE)
    $ok = ($code -eq 0)

    if ($skipped) {
        Write-Host "   skipped  ($elapsed s)" -ForegroundColor Yellow
    } elseif ($ok) {
        Write-Host "   ok  ($elapsed s)" -ForegroundColor Green
    } else {
        Write-Host "   FAILED  (exit $code, $elapsed s)" -ForegroundColor Red
    }

    $results.Add([pscustomobject]@{ Name = $Name; Ok = $ok; Skipped = $skipped; Seconds = $elapsed })
}

Write-Host "Cortex local quality check -- tier: $Tier" -ForegroundColor White

if (-not $SkipBackend) {
    # CI lints with the exact Ruff from the dev lock. Linting with a different
    # one locally means a green run here and a red one there, for no reason the
    # diff explains.
    Invoke-Step 'Dev environment matches the pins' {
        python scripts/check_dev_environment.py
    }

    # quality.yml's `fast` job fails on a stale lock. This script claims to run
    # the fast gates, so it has to run this one too -- otherwise editing
    # pyproject.toml is green locally and red in CI.
    # `return`, never `exit`: Invoke-Step runs this with the call operator, and
    # `exit` inside a script block terminates the whole script rather than the
    # block, taking every later check with it.
    Invoke-Step 'Dependency lockfiles are current' {
        # `python -m uv`, not `uv`: pip installs the launcher into the user
        # Scripts directory, which is frequently not on PATH, so probing for
        # the executable would skip on machines that do have uv.
        python -c "import uv" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '   uv is not installed; CI still enforces this.' -ForegroundColor Yellow
            Write-Host '   Install it with: python -m pip install uv' -ForegroundColor Yellow
            $global:LASTEXITCODE = $SKIPPED_EXIT_CODE
            return
        }
        python -m uv pip compile pyproject.toml --python-version 3.11 --generate-hashes -o requirements.lock.txt
        if ($LASTEXITCODE -ne 0) { return }
        python -m uv pip compile pyproject.toml --extra dev --python-version 3.11 --generate-hashes -o requirements-dev.lock.txt
        if ($LASTEXITCODE -ne 0) { return }
        # `git diff`, not `git status --porcelain`: uv writes LF and the
        # working tree is CRLF, so status reports a stat-only change on files
        # whose content is identical once normalized. diff compares content and
        # is silent for those. $ErrorActionPreference is Continue at script
        # scope here, so a non-zero native exit sets $LASTEXITCODE rather than
        # throwing.
        $drift = git --no-pager diff --name-only -- requirements.lock.txt requirements-dev.lock.txt
        if ($drift) {
            Write-Host '   Lockfiles no longer match pyproject.toml. The regenerated' -ForegroundColor Red
            Write-Host '   files are in your working tree -- review and commit them.' -ForegroundColor Red
            $global:LASTEXITCODE = 1
            return
        }
        $global:LASTEXITCODE = 0
    }

    Invoke-Step 'Lint Python (ruff)' {
        python -m ruff check backend tests tools main.py app_factory.py
    }

    Invoke-Step 'Backend tests (pytest)' {
        python -m pytest -q
    }

    Invoke-Step 'Artifact boundary qualification' {
        python tools/execution_spikes/artifact_security_review.py --json --strict
    }

    Invoke-Step 'Resource/watchdog qualification' {
        python tools/execution_spikes/resource_watchdog_qualification.py --json --strict
    }

    Invoke-Step 'API contracts are up to date' {
        python tools/generate_contracts.py --check
    }

    if ($Tier -eq 'full') {
        Invoke-Step 'Compile application modules' {
            python -m compileall -q main.py app_factory.py backend
        }
    }
}

if (-not $SkipFrontend) {
    if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
        Write-Host ''
        Write-Host 'frontend/node_modules missing -- running npm ci first.' -ForegroundColor Yellow
        Invoke-Step 'Install frontend dependencies' { & $npm ci } $frontend
    }

    Invoke-Step 'Frontend types (tsc)' { & $npm run typecheck } $frontend
    Invoke-Step 'Lint frontend (eslint)' { & $npm run lint } $frontend
    Invoke-Step 'Frontend unit tests (vitest)' { & $npm test -- --run } $frontend

    if ($Tier -eq 'full') {
        Invoke-Step 'Install Playwright Chromium' {
            npx playwright install chromium
        } $frontend

        Invoke-Step 'Frontend browser tests (playwright)' {
            & $npm run e2e -- --workers=1
        } $frontend

        Invoke-Step 'Build frontend bundle' { & $npm run build } $frontend
    }
}

Write-Host ''
Write-Host ('-' * 58)

$failed = @($results | Where-Object { -not ($_.Ok -or $_.Skipped) })
$skipped = @($results | Where-Object { $_.Skipped })
$total = [math]::Round(($results | Measure-Object -Property Seconds -Sum).Sum, 1)

foreach ($r in $results) {
    $mark = if ($r.Skipped) { 'skip' } elseif ($r.Ok) { 'ok  ' } else { 'FAIL' }
    $color = if ($r.Skipped) { 'Yellow' } elseif ($r.Ok) { 'Green' } else { 'Red' }
    Write-Host ("  {0}  {1,-42} {2,6}s" -f $mark, $r.Name, $r.Seconds) -ForegroundColor $color
}

Write-Host ('-' * 58)

if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) of $($results.Count) checks failed in ${total}s." -ForegroundColor Red
    exit 1
}

if ($skipped.Count -gt 0) {
    Write-Host "$($results.Count - $skipped.Count) checks passed, $($skipped.Count) skipped, in ${total}s." -ForegroundColor Yellow
    exit 0
}

Write-Host "All $($results.Count) checks passed in ${total}s." -ForegroundColor Green
exit 0
