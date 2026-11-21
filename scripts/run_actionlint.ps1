# Run the same pinned actionlint on CI and Windows development machines.
# The actionlint-py sdist downloads this release during pip's build, so keep
# that package out of the shared backend test dependencies. Download here,
# only when workflows are actually being linted.
$ErrorActionPreference = 'Stop'

$version = '1.7.12'
$archiveName = "actionlint_$($version)_windows_amd64.zip"
$url = "https://github.com/rhysd/actionlint/releases/download/v$($version)/$archiveName"
# GitHub's v1.7.12 release asset SHA-256 (actionlint_1.7.12_windows_amd64.zip).
$expectedSha256 = '6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9'
$cacheDir = Join-Path ([System.IO.Path]::GetTempPath()) "cortex-actionlint-$version"
New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
$archive = Join-Path $cacheDir $archiveName

# Always verify a cached archive before extracting; never trust an incomplete
# download or an unverified executable left over from an earlier run.
if (Test-Path -LiteralPath $archive -PathType Leaf) {
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expectedSha256) {
        Remove-Item -LiteralPath $archive -Force
    }
}
if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
    $partial = Join-Path $cacheDir "$archiveName.$PID.partial"
    try {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Invoke-WebRequest -Uri $url -OutFile $partial -TimeoutSec 45
                break
            } catch {
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $expectedSha256) {
            throw 'actionlint download SHA-256 mismatch; refusing to execute it.'
        }
        Move-Item -LiteralPath $partial -Destination $archive -Force
    } finally {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    }
}

# Extract into a unique directory, so concurrent checks cannot execute a
# partially overwritten binary. Never run a previously extracted executable.
$extractDir = Join-Path $cacheDir "extract-$PID-$([guid]::NewGuid().ToString('N'))"
try {
    Expand-Archive -LiteralPath $archive -DestinationPath $extractDir
    $binary = Join-Path $extractDir 'actionlint.exe'
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        throw 'Verified actionlint archive does not contain actionlint.exe.'
    }
    & $binary -shellcheck= -pyflakes=
    if ($LASTEXITCODE -ne 0) { throw "actionlint exited with code $LASTEXITCODE" }
} finally {
    Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
}

