<#
.SYNOPSIS
    Capture the full product screenshot set from the staged showcase workspace.

.DESCRIPTION
    Starts tools/screenshots/showcase_server.py on an isolated port, hands the
    bootstrap tokens to the Playwright capture script, writes the images to
    -OutDir, and stops the server again.

    The workspace is fixture data and no model is contacted, so re-running this
    reproduces the same images. Requires a built frontend bundle:

        npm run build --prefix frontend

.EXAMPLE
    ./tools/screenshots/showcase.ps1 -OutDir "$env:USERPROFILE\Desktop\Cortex Screenshots"
#>
[CmdletBinding()]
param(
    [int]$Port = 8801,
    [string]$OutDir = "$env:USERPROFILE\Desktop\Cortex Screenshots"
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path (Join-Path $repoRoot 'frontend/dist/index.html'))) {
    throw "No built frontend. Run: npm run build --prefix frontend"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$server = $null
try {
    Write-Host "Starting staged showcase server on port $Port..." -ForegroundColor Cyan

    $stdout = New-TemporaryFile
    $stderr = New-TemporaryFile
    $server = Start-Process -FilePath 'python' `
        -ArgumentList @('tools/screenshots/showcase_server.py', '--port', $Port) `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -NoNewWindow -PassThru

    # The bootstrap token is the first line the server prints.
    $token = $null
    foreach ($attempt in 1..60) {
        Start-Sleep -Milliseconds 250
        $line = (Get-Content $stdout -TotalCount 1 -ErrorAction SilentlyContinue)
        if ($line) { $token = $line.Trim(); break }
        if ($server.HasExited) {
            Get-Content $stderr | Write-Host -ForegroundColor Red
            throw "Showcase server exited before printing a token."
        }
    }
    if (-not $token) { throw "Timed out waiting for the showcase server bootstrap token." }

    Write-Host 'Capturing screenshots...' -ForegroundColor Cyan
    node (Join-Path $PSScriptRoot 'capture_showcase.mjs') $token --port $Port --out $OutDir
    if ($LASTEXITCODE -ne 0) {
        Get-Content $stderr -Tail 30 | Write-Host -ForegroundColor DarkGray
        throw "Capture failed with exit code $LASTEXITCODE."
    }

    Write-Host "Screenshots written to $OutDir" -ForegroundColor Green
} finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        Write-Host 'Showcase server stopped.'
    }
}
