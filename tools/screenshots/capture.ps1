<#
.SYNOPSIS
    Regenerate the README screenshots from the staged demo workspace.

.DESCRIPTION
    Starts tools/screenshots/demo_server.py on an isolated port, hands the
    one-time bootstrap token to the Playwright capture script, writes the
    images to docs/images/, and stops the server again.

    The workspace is fixture data and no model is contacted, so re-running this
    reproduces the same images. Requires a built frontend bundle:

        npm run build --prefix frontend

.EXAMPLE
    ./tools/screenshots/capture.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8799
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path (Join-Path $repoRoot 'frontend/dist/index.html'))) {
    throw "No built frontend. Run: npm run build --prefix frontend"
}

$server = $null
try {
    Write-Host "Starting staged demo server on port $Port..." -ForegroundColor Cyan

    $stdout = New-TemporaryFile
    $stderr = New-TemporaryFile
    $server = Start-Process -FilePath 'python' `
        -ArgumentList @('tools/screenshots/demo_server.py', '--port', $Port) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -NoNewWindow -PassThru

    # The bootstrap token is the first line the server prints.
    $token = $null
    foreach ($attempt in 1..40) {
        Start-Sleep -Milliseconds 250
        $line = (Get-Content $stdout -TotalCount 1 -ErrorAction SilentlyContinue)
        if ($line) { $token = $line.Trim(); break }
        if ($server.HasExited) {
            Get-Content $stderr | Write-Host -ForegroundColor Red
            throw "Demo server exited before printing a token."
        }
    }
    if (-not $token) { throw "Timed out waiting for the demo server bootstrap token." }

    Write-Host 'Capturing screenshots...' -ForegroundColor Cyan
    node (Join-Path $PSScriptRoot 'capture.mjs') $token --port $Port
    if ($LASTEXITCODE -ne 0) { throw "Capture failed with exit code $LASTEXITCODE." }

    Write-Host 'Screenshots written to docs/images/.' -ForegroundColor Green
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        Write-Host 'Demo server stopped.'
    }
}
