param(
    [switch]$SkipDependencyInstall,
    [string]$SigningKeyPath,
    [string]$SigningKeyId,
    [string]$BundleVersion,
    [int]$Sequence,
    [string]$ManifestOutput
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "The recipe worker must be packaged on Windows."
}

$SigningRequested = -not [string]::IsNullOrWhiteSpace($SigningKeyPath)
$SigningOptionsPresent = $SigningRequested -or
    -not [string]::IsNullOrWhiteSpace($SigningKeyId) -or
    -not [string]::IsNullOrWhiteSpace($BundleVersion) -or
    $Sequence -ne 0 -or
    -not [string]::IsNullOrWhiteSpace($ManifestOutput)
if (-not $SigningRequested) {
    if ($SigningOptionsPresent) {
        throw "SigningKeyPath is required when any signing option is supplied."
    }
} elseif ([string]::IsNullOrWhiteSpace($SigningKeyId) -or
    [string]::IsNullOrWhiteSpace($BundleVersion) -or
    $Sequence -lt 1) {
    throw "Signing requires SigningKeyId, BundleVersion, and a positive Sequence."
} elseif (-not (Test-Path -LiteralPath $SigningKeyPath -PathType Leaf)) {
    throw "The external signing key file was not found."
}

$DistRoot = Join-Path $RepositoryRoot "dist"
$CanonicalPackage = Join-Path $DistRoot "recipe-runtime"
$BuildId = [guid]::NewGuid().ToString("N")
$StagingRoot = Join-Path $DistRoot ".recipe-runtime-build-$BuildId"
$StagingDist = Join-Path $StagingRoot "dist"
$StagingWork = Join-Path $StagingRoot "work"
$StagedPackage = Join-Path $StagingDist "recipe-runtime"
$StagedExecutable = Join-Path $StagedPackage "recipe_worker.exe"
$StagedManifest = Join-Path $StagingRoot "recipe-runtime.manifest.json"
$PromotionBackupRoot = Join-Path $DistRoot ".recipe-runtime-backup-$BuildId"
$PromotionBackupPackage = Join-Path $PromotionBackupRoot "recipe-runtime"

if ([string]::IsNullOrWhiteSpace($ManifestOutput)) {
    $FinalManifest = Join-Path $DistRoot "recipe-runtime.manifest.json"
} elseif ([IO.Path]::IsPathRooted($ManifestOutput)) {
    $FinalManifest = [IO.Path]::GetFullPath($ManifestOutput)
} else {
    $FinalManifest = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $ManifestOutput))
}

$PackageBackedUp = $false
$ManifestBackedUp = $false
$PackagePromoted = $false
$ManifestPromoted = $false
$Completed = $false
$RollbackCompleted = $false

try {
    if (Test-Path -LiteralPath $DistRoot -PathType Leaf) {
        throw "The recipe worker dist path is not a directory."
    }
    if ((Test-Path -LiteralPath $FinalManifest) -and
        -not (Test-Path -LiteralPath $FinalManifest -PathType Leaf)) {
        throw "The existing recipe worker manifest is not a file."
    }
    if (-not (Test-Path -LiteralPath $DistRoot)) {
        New-Item -ItemType Directory -Path $DistRoot | Out-Null
    }
    if (Test-Path -LiteralPath $StagingRoot) {
        throw "The unique recipe worker staging directory already exists."
    }
    if (Test-Path -LiteralPath $PromotionBackupRoot) {
        throw "The unique recipe worker promotion backup directory already exists."
    }
    if (Test-Path -LiteralPath $CanonicalPackage -PathType Leaf) {
        throw "The existing recipe worker output is not a directory."
    }

    New-Item -ItemType Directory -Path $StagingRoot | Out-Null
    New-Item -ItemType Directory -Path $StagingDist | Out-Null
    New-Item -ItemType Directory -Path $StagingWork | Out-Null

    if (-not $SkipDependencyInstall) {
        python -m pip install --disable-pip-version-check -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            throw "The dependency installation failed; refusing to package a stale worker."
        }
        python -m pip install --disable-pip-version-check "pyinstaller==6.14.2"
        if ($LASTEXITCODE -ne 0) {
            throw "The PyInstaller installation failed; refusing to package a stale worker."
        }
    }

    python -m PyInstaller --noconfirm --clean `
        --distpath $StagingDist `
        --workpath $StagingWork `
        packaging/recipe_worker/recipe_worker.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed; refusing to package a stale worker."
    }

    if (-not (Test-Path -LiteralPath $StagedExecutable -PathType Leaf)) {
        throw "The recipe worker package did not produce recipe_worker.exe."
    }
    $StagedExecutableInfo = Get-Item -LiteralPath $StagedExecutable -Force
    if ($StagedExecutableInfo.Length -lt 1) {
        throw "The recipe worker executable is empty."
    }
    $StagedExecutableHash = (Get-FileHash -LiteralPath $StagedExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    $BuildIdentity = "$BuildId/$StagedExecutableHash"

    # PyInstaller emits an empty PEP 376 REQUESTED marker for some transitive
    # distributions. It carries no runtime bytes and cannot be represented by
    # the positive-size signed manifest, so remove only that known generated marker.
    Get-ChildItem -LiteralPath $StagedPackage -Recurse -File -Filter "REQUESTED" -ErrorAction Stop |
        Where-Object { $_.Length -eq 0 -and $_.DirectoryName -like "*.dist-info" } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop }

    if ($SigningRequested) {
        python tools/sign_recipe_worker.py `
            --source-root $StagedPackage `
            --private-key $SigningKeyPath `
            --key-id $SigningKeyId `
            --bundle-version $BundleVersion `
            --sequence $Sequence `
            --output-manifest $StagedManifest
        if ($LASTEXITCODE -ne 0) {
            throw "The recipe worker manifest signing gate failed; refusing to promote the package."
        }
        if (-not (Test-Path -LiteralPath $StagedManifest -PathType Leaf)) {
            throw "The recipe worker manifest signing gate produced no manifest."
        }
    }

    # A directory cannot be replaced atomically on every supported Windows
    # filesystem. Move the old output to a unique rollback location, then move
    # the verified staging directory into place. Any later failure restores the
    # previous output before this script can report success.
    New-Item -ItemType Directory -Path $PromotionBackupRoot | Out-Null
    if (Test-Path -LiteralPath $CanonicalPackage) {
        Move-Item -LiteralPath $CanonicalPackage -Destination $PromotionBackupPackage -ErrorAction Stop
        $PackageBackedUp = $true
    }

    if (Test-Path -LiteralPath $FinalManifest) {
        if (-not (Test-Path -LiteralPath $FinalManifest -PathType Leaf)) {
            throw "The existing recipe worker manifest is not a file."
        }
        $ManifestBackup = Join-Path $PromotionBackupRoot "recipe-runtime.manifest.json"
        Move-Item -LiteralPath $FinalManifest -Destination $ManifestBackup -ErrorAction Stop
        $ManifestBackedUp = $true
    }

    Move-Item -LiteralPath $StagedPackage -Destination $CanonicalPackage -ErrorAction Stop
    $PackagePromoted = $true
    if ($SigningRequested) {
        Move-Item -LiteralPath $StagedManifest -Destination $FinalManifest -ErrorAction Stop
        $ManifestPromoted = $true
    }

    $PromotedExecutable = Join-Path $CanonicalPackage "recipe_worker.exe"
    if (-not (Test-Path -LiteralPath $PromotedExecutable -PathType Leaf)) {
        throw "The promoted recipe worker package is missing recipe_worker.exe."
    }
    $PromotedExecutableInfo = Get-Item -LiteralPath $PromotedExecutable -Force
    $PromotedExecutableHash = (Get-FileHash -LiteralPath $PromotedExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($PromotedExecutableInfo.Length -ne $StagedExecutableInfo.Length -or
        $PromotedExecutableHash -ne $StagedExecutableHash) {
        throw "The promoted recipe worker does not match the verified build."
    }

    $Completed = $true
    # The new package is verified and committed. If backup cleanup fails, keep
    # the rollback copy and fail without risking the committed package.
    Remove-Item -LiteralPath $PromotionBackupRoot -Recurse -Force -ErrorAction Stop
} catch {
    if (-not $Completed) {
        try {
            if ($ManifestPromoted -and (Test-Path -LiteralPath $FinalManifest)) {
                Remove-Item -LiteralPath $FinalManifest -Force -ErrorAction Stop
            }
            if ($PackagePromoted -and (Test-Path -LiteralPath $CanonicalPackage)) {
                Remove-Item -LiteralPath $CanonicalPackage -Recurse -Force -ErrorAction Stop
            }
            if ($PackageBackedUp -and (Test-Path -LiteralPath $PromotionBackupPackage)) {
                Move-Item -LiteralPath $PromotionBackupPackage -Destination $CanonicalPackage -ErrorAction Stop
            }
            if ($ManifestBackedUp) {
                $ManifestBackup = Join-Path $PromotionBackupRoot "recipe-runtime.manifest.json"
                if (Test-Path -LiteralPath $ManifestBackup) {
                    Move-Item -LiteralPath $ManifestBackup -Destination $FinalManifest -ErrorAction Stop
                }
            }
            $RollbackCompleted = $true
        } catch {
            throw "Recipe worker promotion failed and rollback could not be completed; the backup was retained at $PromotionBackupRoot."
        }
    }
    throw
} finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force -ErrorAction Stop
    }
    if ($RollbackCompleted -and (Test-Path -LiteralPath $PromotionBackupRoot)) {
        Remove-Item -LiteralPath $PromotionBackupRoot -Recurse -Force -ErrorAction Stop
    }
}

Write-Host "Recipe worker contract package ready: $CanonicalPackage\recipe_worker.exe"
Write-Host "Build identity: $BuildIdentity"
if (-not $SigningRequested) {
    Write-Host "This output is intentionally unsigned and not launch-authorized."
    exit 0
}
Write-Host "Signed recipe worker manifest ready: $FinalManifest"
Write-Host "Installation still requires the pinned public-key trust root and SignedBundleInstaller."
