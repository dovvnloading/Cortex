<#
.SYNOPSIS
    Sign a Cortex release built by .github/workflows/release.yml and replace
    the unsigned archive on its draft release.

.DESCRIPTION
    CI builds and smoke-tests the package but cannot sign it: signing uses the
    maintainer's Azure Artifact Signing identity, which is deliberately not
    handed to CI. This script closes that gap on the maintainer's machine:

      1. downloads the draft's unsigned archive and checks it against the
         SHA256SUMS.txt CI published beside it;
      2. signs Cortex.exe through signtool and the Artifact Signing dlib,
         authenticating only as the pinned dedicated signer through Azure CLI
         (every other credential source is excluded by the metadata file);
      3. verifies the signature and its publisher, re-packages, and writes a
         new SHA256SUMS.txt;
      4. replaces both assets on the draft (with -Upload).

    It never publishes. The draft stays a draft for the maintainer to review.

    Tool locations are parameters or environment variables, never assumed:
    Azure CLI (az.cmd), Microsoft Artifact Signing Client Tools (the dlib),
    signtool from the Windows SDK, and a .NET 8 runtime for the dlib.

    The signing account, certificate profile and the one allowed signer come
    from packaging/signing.local.json, which is git-ignored so account details
    never enter this public repository. Copy signing.local.example.json and
    fill it in on the signing machine.

.EXAMPLE
    ./packaging/sign_release.ps1 -Tag v2.0.0 -Upload `
        -AzureCliPath 'C:\tools\AzureCLI\...\wbin\az.cmd' `
        -DlibPath 'C:\tools\ArtifactSigningClientTools\Azure.CodeSigning.Dlib.dll' `
        -DotNetRoot 'C:\tools\dotnet'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Tag,
    [string]$Repository = 'dovvnloading/Cortex',
    [string]$AzureCliPath = $env:CORTEX_AZURE_CLI_PATH,
    [string]$DlibPath = $env:CORTEX_ARTIFACT_SIGNING_DLIB_PATH,
    [string]$SignToolPath = $env:CORTEX_SIGNTOOL_PATH,
    [string]$DotNetRoot = $env:CORTEX_DOTNET_ROOT,
    [string]$TimestampServer = 'http://timestamp.acs.microsoft.com',
    [string]$WorkDirectory = (Join-Path ([IO.Path]::GetTempPath()) "cortex-sign-$Tag"),
    [switch]$Upload
)

$ErrorActionPreference = 'Stop'

# Every credential source the Artifact Signing dlib could use except Azure CLI.
$ExcludedCredentials = @(
    'EnvironmentCredential', 'ManagedIdentityCredential', 'WorkloadIdentityCredential',
    'SharedTokenCacheCredential', 'VisualStudioCredential', 'VisualStudioCodeCredential',
    'AzurePowerShellCredential', 'AzureDeveloperCliCredential', 'InteractiveBrowserCredential'
)

function Resolve-Tool([string]$Path, [string]$Name, [string]$Hint) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Name was not found. $Hint"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Resolve-SignTool {
    if ($SignToolPath) { return Resolve-Tool $SignToolPath 'signtool.exe' 'Pass -SignToolPath.' }
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    $found = Get-ChildItem -LiteralPath $kits -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' } |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (-not $found) { throw 'signtool.exe was not found. Install the Windows SDK or pass -SignToolPath.' }
    return $found
}

function Read-SigningConfig([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Signing settings were not found at $Path. Copy packaging/signing.local.example.json there and fill it in."
    }
    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    foreach ($field in 'Endpoint', 'CodeSigningAccountName', 'CertificateProfileName', 'SignerObjectId', 'ExpectedPublisher') {
        if (-not ([string]$config.$field).Trim()) { throw "Signing settings are missing '$field'." }
    }
    if ($config.PSObject.Properties.Name -contains 'AccessToken') {
        throw 'Signing settings must not contain an access token.'
    }
    return $config
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

# --- tools and identity --------------------------------------------------------

$az = Resolve-Tool $AzureCliPath 'Azure CLI (az.cmd)' 'Pass -AzureCliPath or set CORTEX_AZURE_CLI_PATH.'
$dlib = Resolve-Tool $DlibPath 'Azure.CodeSigning.Dlib.dll' 'Install Microsoft Artifact Signing Client Tools; pass -DlibPath or set CORTEX_ARTIFACT_SIGNING_DLIB_PATH.'
$signtool = Resolve-SignTool
$config = Read-SigningConfig (Join-Path $PSScriptRoot 'signing.local.json')

# The dlib's AzureCliCredential shells out to `az`, and needs .NET 8.
$env:PATH = "$(Split-Path -Parent $az);$env:PATH"
if ($DotNetRoot) {
    $env:DOTNET_ROOT = $DotNetRoot
    $env:DOTNET_ROOT_X64 = $DotNetRoot
}

$signedIn = (& $az ad signed-in-user show --query id -o tsv --only-show-errors 2>$null | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0 -or "$signedIn".Trim() -ne $config.SignerObjectId) {
    throw 'Azure CLI must be signed in as the dedicated release signer named in signing.local.json.'
}
Write-Host 'Signer verified.' -ForegroundColor Cyan

# --- fetch and check the unsigned build ------------------------------------------

$version = $Tag.TrimStart('v')
$archiveName = "Cortex-$version-windows-x64.zip"
if (Test-Path -LiteralPath $WorkDirectory) { Remove-Item -LiteralPath $WorkDirectory -Recurse -Force }
$download = New-Item -ItemType Directory -Force -Path (Join-Path $WorkDirectory 'download')
gh release download $Tag --repo $Repository --dir $download.FullName --pattern $archiveName --pattern 'SHA256SUMS.txt'
if ($LASTEXITCODE -ne 0) { throw "Could not download $archiveName and SHA256SUMS.txt from $Tag." }

$unsigned = Join-Path $download.FullName $archiveName
$published = ((Get-Content -LiteralPath (Join-Path $download.FullName 'SHA256SUMS.txt') -Raw).Trim() -split '\s+')[0]
if ((Get-Sha256 $unsigned) -ne $published) {
    throw 'The downloaded archive does not match the checksum CI published. Refusing to sign it.'
}

# The dlib reads account and profile from a metadata file. It is written into
# the private work directory for this run only, never into the repository.
$metadataPath = Join-Path $WorkDirectory 'artifact-signing.metadata.json'
[ordered]@{
    Endpoint = ([string]$config.Endpoint).TrimEnd('/')
    CodeSigningAccountName = [string]$config.CodeSigningAccountName
    CertificateProfileName = [string]$config.CertificateProfileName
    ExcludeCredentials = $ExcludedCredentials
} | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath $metadataPath

$package = Join-Path $WorkDirectory 'Cortex'
Expand-Archive -LiteralPath $unsigned -DestinationPath $package
$executable = Join-Path $package 'Cortex.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw 'Cortex.exe is missing from the archive.' }

# --- sign and verify --------------------------------------------------------------

& $signtool sign /v /fd SHA256 /tr $TimestampServer /td SHA256 /dlib $dlib /dmdf $metadataPath $executable
if ($LASTEXITCODE -ne 0) { throw "signtool failed with exit code $LASTEXITCODE." }

& $signtool verify /pa /v $executable | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'signtool could not verify the new signature.' }
$signature = Get-AuthenticodeSignature -LiteralPath $executable
if ($signature.Status -ne 'Valid') {
    throw "The signature is not valid: $($signature.Status) $($signature.StatusMessage)"
}
if ($signature.SignerCertificate.Subject -notmatch [regex]::Escape("CN=$($config.ExpectedPublisher)")) {
    throw "Signed by an unexpected publisher: $($signature.SignerCertificate.Subject)"
}
if (-not $signature.TimeStamperCertificate) {
    throw 'The signature carries no timestamp, so it would stop validating when the short-lived certificate expires.'
}
Write-Host "Signed by: $($signature.SignerCertificate.Subject)" -ForegroundColor Green

# --- re-package ---------------------------------------------------------------------

$output = New-Item -ItemType Directory -Force -Path (Join-Path $WorkDirectory 'signed')
$signedArchive = Join-Path $output.FullName $archiveName
Compress-Archive -Path (Join-Path $package '*') -DestinationPath $signedArchive
$sums = Join-Path $output.FullName 'SHA256SUMS.txt'
"$(Get-Sha256 $signedArchive)  $archiveName" | Set-Content -Encoding ascii -NoNewline $sums
Write-Host "Signed archive: $signedArchive"
Write-Host "SHA-256:        $(Get-Sha256 $signedArchive)"

if ($Upload) {
    gh release upload $Tag $signedArchive $sums --repo $Repository --clobber
    if ($LASTEXITCODE -ne 0) { throw 'Uploading the signed assets failed.' }
    Write-Host "Replaced the draft's archive and SHA256SUMS.txt with the signed build. It is still a draft." -ForegroundColor Green
}
