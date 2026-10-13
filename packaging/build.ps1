<#
Builds the packaged Mud Music Manager (v0.19.1, Checklist item 60c).

    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

1. Makes (once) a separate build environment, .venv-build, and installs the
   app's libraries (requirements.txt) and PyInstaller into it. Your own
   Python installation is not changed.
2. Runs PyInstaller with packaging\mud_music_manager.spec, producing
   dist\Mud Music Manager\ (both programs plus _internal\).
3. Copies FFmpeg (ffmpeg.exe, ffprobe.exe) and Deno (deno.exe) into its
   tools\ folder. By default the copies found on this computer's PATH are
   used; -FfmpegDir / -DenoExe name others.
4. Runs the build's self-test (Mud Music Manager CLI.exe --selftest).

Options: -Clean (start the build environment again), -SkipSelfTest,
-NoDownload (self-test without the test download), -BuildRoot <folder>
(where .venv-build, build\ and dist\ go; default: the project folder).
#>
param(
    [string]$FfmpegDir = "",
    [string]$DenoExe = "",
    [string]$BuildRoot = "",
    [switch]$Clean,
    [switch]$SkipSelfTest,
    [switch]$NoDownload
)
$ErrorActionPreference = "Stop"
$Project = Split-Path $PSScriptRoot -Parent
if (-not $BuildRoot) { $BuildRoot = $Project }
$Venv = Join-Path $BuildRoot ".venv-build"
$Py = Join-Path $Venv "Scripts\python.exe"
$Dist = Join-Path $BuildRoot "dist"
$Work = Join-Path $BuildRoot "build"
$App = Join-Path $Dist "Mud Music Manager"

if ($Clean -and (Test-Path $Venv)) { Remove-Item $Venv -Recurse -Force }
if (-not (Test-Path $Py)) {
    Write-Host "Creating the build environment in $Venv ..."
    python -m venv $Venv
}
Write-Host "Installing the app's libraries and PyInstaller into the build environment ..."
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet -r (Join-Path $Project "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "Building with PyInstaller ..."
& $Py -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work (Join-Path $PSScriptRoot "mud_music_manager.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "Copying FFmpeg and Deno into tools\ ..."
$Tools = Join-Path $App "tools"
New-Item -ItemType Directory -Force $Tools | Out-Null
if (-not $FfmpegDir) {
    $f = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $f) { throw "FFmpeg not found on PATH; pass -FfmpegDir <folder holding ffmpeg.exe>" }
    $FfmpegDir = Split-Path $f.Source -Parent
}
foreach ($exe in "ffmpeg.exe", "ffprobe.exe") {
    $src = Join-Path $FfmpegDir $exe
    if (-not (Test-Path $src)) { throw "$exe not found in $FfmpegDir" }
    Copy-Item $src $Tools -Force
}
if (-not $DenoExe) {
    $d = Get-Command deno -ErrorAction SilentlyContinue
    if (-not $d) { throw "Deno not found on PATH; pass -DenoExe <path to deno.exe>" }
    $DenoExe = $d.Source
}
Copy-Item $DenoExe (Join-Path $Tools "deno.exe") -Force

if (-not $SkipSelfTest) {
    Write-Host "Running the self-test ..."
    $testArgs = @("--selftest")
    if ($NoDownload) { $testArgs += "--no-download" }
    & (Join-Path $App "Mud Music Manager CLI.exe") @testArgs
    if ($LASTEXITCODE -ne 0) { throw "The self-test failed (see above)" }
}
$size = (Get-ChildItem $App -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Done: {0} ({1:N0} MB)" -f $App, $size)
