param(
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$BootstrapperUri = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $RepositoryRoot "packaging\.runtime\webview2\MicrosoftEdgeWebview2Setup.exe"
}

function Test-MicrosoftSignature([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    return $signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid -and
        $signature.SignerCertificate.Subject -match "(^|, )O=Microsoft Corporation(,|$)"
}

$destinationPath = [System.IO.Path]::GetFullPath($Destination)
$destinationDirectory = Split-Path -Parent $destinationPath
New-Item -ItemType Directory -Force -Path $destinationDirectory | Out-Null

if (Test-MicrosoftSignature $destinationPath) {
    Write-Host "Using cached, signed WebView2 bootstrapper: $destinationPath"
    exit 0
}

$temporaryPath = Join-Path $destinationDirectory ("webview2-" + [guid]::NewGuid().ToString("N") + ".download")
try {
    Invoke-WebRequest -UseBasicParsing -Uri $BootstrapperUri -OutFile $temporaryPath
    if (-not (Test-MicrosoftSignature $temporaryPath)) {
        throw "The downloaded WebView2 bootstrapper does not have a valid Microsoft Authenticode signature."
    }
    Move-Item -LiteralPath $temporaryPath -Destination $destinationPath -Force
}
finally {
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

$hash = (Get-FileHash -LiteralPath $destinationPath -Algorithm SHA256).Hash
Write-Host "Prepared signed WebView2 bootstrapper ($hash): $destinationPath"
