<#
.SYNOPSIS
    Record the README tour GIFs from the real Cortex frontend.

.DESCRIPTION
    For each scene, starts tools/screenshots/tour_server.py with a fresh demo
    workspace, drives the built frontend through record_tour.mjs, and stops the
    server again, so no scene inherits state from another. render_tour.py then
    turns the recorded timelines into GIFs in -OutDir.

    The workspace is fixture data and every model reply is scripted, so no
    model runs and a re-run records the same tour. Agent mode and the approved
    code task are Cortex's own code: only the model is scripted.

    Requires a built frontend (npm.cmd run build --prefix frontend), the
    Playwright Chromium the e2e tests use, and ffmpeg 4 or newer
    (imageio-ffmpeg's copy is used when it is installed).

.PARAMETER ProjectParent
    Folder the sample csv-importer project is created in for the Agent scene.
    Its path is typed on screen; the README recording used C:\cortex-tour. The
    default is a temporary folder. The project is removed afterwards.

.EXAMPLE
    ./tools/screenshots/tour.ps1

.EXAMPLE
    ./tools/screenshots/tour.ps1 -Scenes agent -ProjectParent C:\cortex-tour
#>
[CmdletBinding()]
param(
    [int]$Port = 8802,
    [string[]]$Scenes = @('chat', 'palette', 'models', 'approval', 'agent'),
    [string]$OutDir,
    [string]$WorkDir,
    [string]$FrontendDist,
    [string]$ProjectParent
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repoRoot 'scripts/process_arguments.ps1')
if (-not $OutDir) { $OutDir = Join-Path $repoRoot '.github/images' }
if (-not $WorkDir) { $WorkDir = Join-Path $repoRoot 'build/tour' }
if (-not $FrontendDist) { $FrontendDist = Join-Path $repoRoot 'frontend/dist' }

if (-not (Test-Path (Join-Path $FrontendDist 'index.html'))) {
    throw "No built frontend at $FrontendDist. Run: npm.cmd run build --prefix frontend"
}

foreach ($scene in $Scenes) {
    Write-Host "Recording $scene..." -ForegroundColor Cyan
    $stdout = New-TemporaryFile
    $stderr = New-TemporaryFile
    $serverArgs = @('tools/screenshots/tour_server.py', '--port', $Port, '--frontend-dist', $FrontendDist)
    if ($ProjectParent) { $serverArgs += @('--project-parent', $ProjectParent) }
    # Paths may contain spaces; Start-Process quotes nothing itself.
    $serverArgs = $serverArgs | ForEach-Object { ConvertTo-ProcessArgument "$_" }
    $server = Start-Process -FilePath 'python' -ArgumentList $serverArgs `
        -WorkingDirectory $repoRoot -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -NoNewWindow -PassThru
    try {
        # The server prints its bootstrap token, then the sample project path.
        $lines = @()
        foreach ($attempt in 1..80) {
            Start-Sleep -Milliseconds 250
            $lines = @(Get-Content $stdout -ErrorAction SilentlyContinue)
            if ($lines.Count -ge 2) { break }
            if ($server.HasExited) {
                Get-Content $stderr | Write-Host -ForegroundColor Red
                throw 'The tour server exited before it was ready.'
            }
        }
        if ($lines.Count -lt 2) { throw 'Timed out waiting for the tour server.' }
        node (Join-Path $PSScriptRoot 'record_tour.mjs') $lines[0].Trim() `
            --port $Port --project $lines[1].Trim() --out $WorkDir --scene $scene
        if ($LASTEXITCODE -ne 0) {
            Get-Content $stderr -Tail 30 | Write-Host -ForegroundColor DarkGray
            throw "Recording $scene failed with exit code $LASTEXITCODE."
        }
    } finally {
        # Ask the server to stop so it removes its temporary folders; a killed
        # process on Windows never runs its cleanup.
        try { Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/showcase/shutdown" | Out-Null } catch { }
        if (-not $server.WaitForExit(15000)) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
        Remove-Item $stdout, $stderr -ErrorAction SilentlyContinue
    }
}

Write-Host 'Rendering GIFs...' -ForegroundColor Cyan
$renderArgs = @((Join-Path $PSScriptRoot 'render_tour.py'), $WorkDir, '--out', $OutDir)
foreach ($scene in $Scenes) { $renderArgs += @('--scene', $scene) }
python @renderArgs
if ($LASTEXITCODE -ne 0) { throw "Rendering failed with exit code $LASTEXITCODE." }
Write-Host "Tour GIFs written to $OutDir" -ForegroundColor Green
