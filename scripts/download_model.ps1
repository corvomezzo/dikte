# whisper.cpp GGML modelini models/ altına indirir (kesilirse kaldığı yerden devam eder).
param([string]$Name = "large-v3-turbo-q8_0")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "models"
New-Item -ItemType Directory -Force $dir | Out-Null
$out = Join-Path $dir "ggml-$Name.bin"
$url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$Name.bin"
for ($i = 0; $i -lt 5; $i++) {
    curl.exe -L --fail -C - --retry 3 -o $out $url
    if ($LASTEXITCODE -eq 0) { break }
}
if ($LASTEXITCODE) { throw "indirme başarısız ($LASTEXITCODE)" }
Write-Host "Tamam: $out ($([math]::Round((Get-Item $out).Length/1MB)) MB)"
