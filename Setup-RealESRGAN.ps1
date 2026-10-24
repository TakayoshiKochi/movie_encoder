# Download the official portable Windows build and bundled models into tools/.
$ErrorActionPreference = 'Stop'
$destination = Join-Path $PSScriptRoot 'tools\realesrgan'
if (Test-Path -LiteralPath (Join-Path $destination 'realesrgan-ncnn-vulkan.exe')) {
    Write-Host "Real-ESRGAN already installed: $destination"
    exit 0
}
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$archive = Join-Path $destination 'windows.zip'
$url = 'https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip'
Invoke-WebRequest -Uri $url -OutFile $archive
$expectedHash = 'ABC02804E17982A3BE33675E4D471E91EA374E65B70167ABC09E31ACB412802D'
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expectedHash) {
    throw 'Real-ESRGAN archive checksum mismatch. Installation stopped.'
}
Expand-Archive -LiteralPath $archive -DestinationPath $destination -Force
Write-Host "Installed Real-ESRGAN: $destination"
