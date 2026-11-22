<#
.SYNOPSIS
    Builds a single-file, self-contained Windows GUI Installer (AIBridgeSetup.exe)
    with the published AI Bridge C# WPF Windows Client embedded inside.

.EXAMPLE
    pwsh scripts/build_windows_installer.ps1
    pwsh scripts/build_windows_installer.ps1 -OutDir dist/windows -SkipClientPublish:$false
#>
[CmdletBinding()]
param(
    [string]$OutDir = "dist/windows",
    [string]$Runtime = "win-x64",
    [switch]$SkipClientPublish
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$ClientProj = Join-Path $RepoRoot "apps\windows-client\AIBridgeClient.csproj"
$InstallerDir = Join-Path $RepoRoot "apps\windows-installer"
$InstallerProj = Join-Path $InstallerDir "AIBridgeInstaller.csproj"
$PayloadDir = Join-Path $InstallerDir "Payload"
$PayloadZip = Join-Path $PayloadDir "AIBridgeClientPayload.zip"
$ClientPublishDir = Join-Path $RepoRoot "apps\windows-client\bin\Release\net9.0-windows\publish\$Runtime"
$InstallerPublishDir = Join-Path $InstallerDir "bin\Release\net9.0-windows\publish\$Runtime"
$AbsOutDir = if ([System.IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $RepoRoot $OutDir }

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw ".NET 9 SDK ('dotnet') was not found on PATH. Install .NET 9 SDK from https://dotnet.microsoft.com/download/dotnet/9.0"
}

$CppDir = Join-Path $RepoRoot "windows-agent"
$CppBuildDir = Join-Path $CppDir "build"
if (Get-Command cmake -ErrorAction SilentlyContinue) {
    Write-Host "==> [0/4] Building Native C++20 Windows Agent with libdatachannel WebRTC (ai_bridge_native.dll, ai_bridge_desktop.exe, ai_bridge_service.exe)..." -ForegroundColor Cyan
    # Production Windows releases must ship a real PeerConnection backend.
    # Without -DAIBRIDGE_WITH_LIBDATACHANNEL=ON, ai_bridge_desktop exits
    # no_cpp_webrtc_backend and Live Desktop falls back to JPEG only.
    cmake -S $CppDir -B $CppBuildDir -DAIBRIDGE_WITH_LIBDATACHANNEL=ON -DCMAKE_BUILD_TYPE=Release
    if ($LASTEXITCODE -eq 0) {
        cmake --build $CppBuildDir --config Release
    }
}

Write-Host "==> [1/4] Publishing AI Bridge Windows Client ($Runtime single-file)..." -ForegroundColor Cyan
if (-not $SkipClientPublish) {
    dotnet publish $ClientProj -c Release -r $Runtime --self-contained true `
        -p:PublishSingleFile=true `
        -p:IncludeNativeLibrariesForSelfExtract=true `
        -p:EnableCompressionInSingleFile=true `
        -o $ClientPublishDir
    if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed for AIBridgeClient." }
}

$ClientExe = Join-Path $ClientPublishDir "AIBridgeClient.exe"
if (-not (Test-Path $ClientExe)) {
    throw "Expected published client binary at $ClientExe"
}

Write-Host "==> [2/4] Packaging client payload into $PayloadZip..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $PayloadDir | Out-Null
if (Test-Path $PayloadZip) { Remove-Item -Force $PayloadZip }
Compress-Archive -Path (Join-Path $ClientPublishDir "*") -DestinationPath $PayloadZip -CompressionLevel Optimal

Write-Host "==> [3/4] Publishing bundled AIBridgeSetup.exe ($Runtime single-file)..." -ForegroundColor Cyan
dotnet publish $InstallerProj -c Release -r $Runtime --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:EnableCompressionInSingleFile=true `
    -o $InstallerPublishDir
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed for AIBridgeInstaller." }

$SetupExe = Join-Path $InstallerPublishDir "AIBridgeSetup.exe"
if (-not (Test-Path $SetupExe)) {
    throw "Expected published setup binary at $SetupExe"
}

Write-Host "==> [4/4] Writing release artifacts to $AbsOutDir..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $AbsOutDir | Out-Null
$OutExe = Join-Path $AbsOutDir "AIBridgeSetup.exe"
Copy-Item -Force $SetupExe $OutExe

$Hash = (Get-FileHash -Path $OutExe -Algorithm SHA256).Hash.ToLowerInvariant()
$HashFile = "$OutExe.sha256"
Set-Content -Path $HashFile -Value "$Hash  AIBridgeSetup.exe" -Encoding Ascii

# Clean temporary payload archive so git working tree stays clean
Remove-Item -Force $PayloadZip -ErrorAction SilentlyContinue

$SizeMb = [Math]::Round((Get-Item $OutExe).Length / 1MB, 2)
Write-Host ""
Write-Host "SUCCESS: Built $OutExe ($SizeMb MB)" -ForegroundColor Green
Write-Host "SHA-256: $Hash" -ForegroundColor Green
