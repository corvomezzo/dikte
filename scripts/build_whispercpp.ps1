# whisper.cpp'yi Vulkan (AMD/Intel/NVIDIA GPU) desteğiyle derler ve whisper-server.exe'yi bin/ altına koyar.
# Gerekenler: VS 2022 Build Tools (C++), CMake, Vulkan SDK (glslc).
#   winget install Kitware.CMake KhronosGroup.VulkanSDK
param(
    [string]$Tag = "v1.9.5",
    [switch]$Clean
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "vendor\whisper.cpp"
$build = Join-Path $src "build"
$bin = Join-Path $root "bin"

if (-not (Test-Path $src)) {
    git clone --depth 1 --branch $Tag https://github.com/ggml-org/whisper.cpp $src
}

# Yeni kurulan araçlar bu oturumun PATH'inde olmayabilir
if (-not $env:VULKAN_SDK) { $env:VULKAN_SDK = [Environment]::GetEnvironmentVariable("VULKAN_SDK", "Machine") }
if (-not $env:VULKAN_SDK) {
    $sdk = Get-ChildItem C:\VulkanSDK -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $sdk) { throw "Vulkan SDK bulunamadı (winget install KhronosGroup.VulkanSDK)" }
    $env:VULKAN_SDK = $sdk.FullName
}
$env:PATH = "$env:VULKAN_SDK\Bin;$env:ProgramFiles\CMake\bin;$env:PATH"
if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
    # winget bazen CMake'i portable zip olarak kurar
    $c = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Directory -Filter "Kitware.CMake*" -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ChildItem $_.FullName -Recurse -Filter cmake.exe } | Select-Object -First 1
    if (-not $c) { throw "cmake bulunamadı (winget install Kitware.CMake)" }
    $env:PATH = "$($c.DirectoryName);$env:PATH"
}

if ($Clean -and (Test-Path $build)) { Remove-Item -Recurse -Force $build }

cmake -S $src -B $build -G "Visual Studio 17 2022" -A x64 `
    -DGGML_VULKAN=ON -DWHISPER_BUILD_EXAMPLES=ON -DWHISPER_BUILD_TESTS=OFF -DBUILD_SHARED_LIBS=OFF
if ($LASTEXITCODE) { throw "cmake configure başarısız" }
cmake --build $build --config Release -j --target whisper-server whisper-cli
if ($LASTEXITCODE) { throw "derleme başarısız" }

New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item "$build\bin\Release\*" $bin -Force
Write-Host "Tamam: $bin\whisper-server.exe"
