<#
Builds the packaged Mud Music Manager (v0.19.1, Checklist item 60c).

    powershell -ExecutionPolicy Bypass -File packaging\build.ps1

1. Makes (once) a separate build environment, .venv-build, and installs the
   app's libraries (requirements.txt) and PyInstaller into it. Your own
   Python installation is not changed.
2. Runs PyInstaller with packaging\mud_music_manager.spec, producing
   dist\Mud Music Manager\ (both programs plus _internal\).
3. Puts FFmpeg (ffmpeg.exe, ffprobe.exe) and Deno (deno.exe) in its tools\
   folder. By default (v0.22.0) the NEWEST official builds are downloaded
   (gyan.dev FFmpeg essentials, Deno's GitHub release), each checked against
   its published SHA-256. -UseLocalTools copies this computer's instead
   (-FfmpegDir / -DenoExe name others). Writes _internal\bundled_versions.json
   and (v0.28.0) _internal\bundled_libraries.json, every library in the build.
   Their licence texts go beside them (FFMPEG_LICENSE.txt, FFMPEG_README.txt
   from the FFmpeg build's folder; DENO_LICENSE.txt from packaging\licenses).
4. Runs the build's self-test (Mud Music Manager CLI.exe --selftest).
5. (v0.20.0) Makes the release files in release\:
     MudMusicManager-Setup-<version>.exe     the installer (Inno Setup 6)
     MudMusicManager-Portable-<version>.zip  the same program + portable.txt
     SHA256SUMS.txt                           fingerprints of both
   The version is app_info.VERSION.

Options: -Clean (start the build environment again), -SkipSelfTest,
-NoDownload (self-test without the test download), -NoRelease (stop after
the program folder), -BuildRoot <folder> (where .venv-build, build\, dist\
and release\ go; default: the project folder; keep its path short, Windows
limits paths to 260 characters), -Iscc <path to ISCC.exe>.
#>
param(
    [string]$FfmpegDir = "",
    [string]$DenoExe = "",
    [string]$BuildRoot = "",
    [string]$Iscc = "",
    [switch]$UseLocalTools,
    [switch]$Clean,
    [switch]$SkipSelfTest,
    [switch]$NoDownload,
    [switch]$NoRelease
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
Write-Host "Installing the newest versions of the app's libraries and PyInstaller into the build environment ..."
& $Py -m pip install --quiet --upgrade pip
# --upgrade (v0.21.0): every build ships the newest library versions, not the
# ones the build environment happened to get the first time (Michael:
# everything the app uses must keep updating).
& $Py -m pip install --quiet --upgrade -r (Join-Path $Project "requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "Building with PyInstaller ..."
& $Py -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work (Join-Path $PSScriptRoot "mud_music_manager.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Tools = Join-Path $App "tools"
New-Item -ItemType Directory -Force $Tools | Out-Null
if (-not ($UseLocalTools -or $FfmpegDir -or $DenoExe)) {
    # v0.22.0 (Checklist item 60g): the newest official FFmpeg (gyan.dev essentials)
    # and Deno, each checked against its published SHA-256 - the same code the app
    # uses to update them between releases (component_updates.py).
    Write-Host "Downloading the newest FFmpeg and Deno (checked against their published fingerprints) ..."
    $savedData = $env:MMM_DATA_DIR
    $env:MMM_DATA_DIR = Join-Path $BuildRoot "build\helper-data"   # the helper's log, not your data folder
    & $Py -c "import sys; sys.path.insert(0, r'$Project'); import component_updates as c; print(c.download_latest_tools(r'$Tools'))"
    $code = $LASTEXITCODE
    $env:MMM_DATA_DIR = $savedData
    if ($code -ne 0) { throw "Downloading FFmpeg / Deno failed (use -UseLocalTools to copy this PC's instead)" }
} else {
    Write-Host "Copying this PC's FFmpeg and Deno into tools\ ..."
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
    $FfmpegRoot = Split-Path $FfmpegDir -Parent   # the gyan.dev build: bin\ beside LICENSE and README.txt
    foreach ($pair in @(@("LICENSE", "FFMPEG_LICENSE.txt"), @("README.txt", "FFMPEG_README.txt"))) {
        $src = Join-Path $FfmpegRoot $pair[0]
        if (-not (Test-Path $src)) { throw "FFmpeg's $($pair[0]) not found in $FfmpegRoot (it must ship with FFmpeg)" }
        Copy-Item $src (Join-Path $Tools $pair[1]) -Force
    }
    Copy-Item (Join-Path $PSScriptRoot "licenses\DENO_LICENSE.txt") $Tools -Force
}

# v0.22.0: what this build ships, so the app knows when its own copies are newer
# than ones it updated earlier (component_updates.bundled_versions()).
$denoV = [regex]::Match((& (Join-Path $Tools "deno.exe") --version | Out-String), 'deno (\d+\.\d+\.\d+)').Groups[1].Value
$ffV = [regex]::Match((& (Join-Path $Tools "ffmpeg.exe") -hide_banner -version | Out-String), 'ffmpeg version (\d+(\.\d+)+)').Groups[1].Value
$pyV = & $Py -c "from importlib import metadata as m; print(m.version('yt-dlp') + ' ' + m.version('yt-dlp-ejs'))"
$ytV, $ejsV = "$pyV".Trim().Split(" ")
$pyRun = (& $Py -c "import platform; print(platform.python_version())" | Out-String).Trim()  # v0.28.0
$bundled = [ordered]@{ "yt-dlp" = $ytV; "yt-dlp-ejs" = $ejsV; "deno" = $denoV; "ffmpeg" = $ffV; "python" = $pyRun }
$bundled | ConvertTo-Json | Set-Content (Join-Path $App "_internal\bundled_versions.json") -Encoding UTF8
Write-Host ("Bundled: " + (($bundled.GetEnumerator() | ForEach-Object { "$($_.Key) $($_.Value)" }) -join ", "))
# v0.28.0 (Checklist item 60h): every library in the build (requirements.txt and what
# it needs), its version and whether it has compiled parts. The release workflow
# attaches it to each release; packaging/release_check.py compares it with PyPI.
$savedData = $env:MMM_DATA_DIR
$env:MMM_DATA_DIR = Join-Path $BuildRoot "build\helper-data"
& $Py -c "import sys; sys.path.insert(0, r'$Project'); import dependency_checker as d; print(d.write_bundled_libraries(r'$App\_internal\bundled_libraries.json'), 'libraries recorded')"
$code = $LASTEXITCODE
$env:MMM_DATA_DIR = $savedData
if ($code -ne 0) { throw "Recording the bundled libraries failed" }

if (-not $SkipSelfTest) {
    Write-Host "Running the self-test ..."
    $testArgs = @("--selftest")
    if ($NoDownload) { $testArgs += "--no-download" }
    & (Join-Path $App "Mud Music Manager CLI.exe") @testArgs
    if ($LASTEXITCODE -ne 0) { throw "The self-test failed (see above)" }
}
$size = (Get-ChildItem $App -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Program folder: {0} ({1:N0} MB)" -f $App, $size)
if ($NoRelease) { return }

# ---- Release files (v0.20.0) -------------------------------------------------
$Version = (Select-String -Path (Join-Path $Project "app_info.py") -Pattern '^VERSION = "([0-9.]+)"').Matches[0].Groups[1].Value
$Release = Join-Path $BuildRoot "release"
New-Item -ItemType Directory -Force $Release | Out-Null
$Setup = Join-Path $Release "MudMusicManager-Setup-$Version.exe"
$Zip = Join-Path $Release "MudMusicManager-Portable-$Version.zip"
Remove-Item $Setup, $Zip -ErrorAction SilentlyContinue

Write-Host "Making the portable .zip ..."
$Stage = Join-Path $BuildRoot "build\portable\Mud Music Manager"
if (Test-Path (Split-Path $Stage -Parent)) { Remove-Item (Split-Path $Stage -Parent) -Recurse -Force }
New-Item -ItemType Directory -Force (Split-Path $Stage -Parent) | Out-Null
Copy-Item $App $Stage -Recurse
@"
Mud Music Manager $Version - portable

This file makes Mud Music Manager keep all of its data (playlists, settings,
pictures, logs, backups) in this folder, next to the program, instead of in
%APPDATA%\Mud Music Manager. Move or copy the whole folder anywhere, for
example onto a USB stick. Delete this file to use %APPDATA% instead.

Start the app with "Mud Music Manager.exe".
"@ | Set-Content (Join-Path $Stage "portable.txt") -Encoding UTF8
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory((Split-Path $Stage -Parent), $Zip,
    [System.IO.Compression.CompressionLevel]::Optimal, $false)

Write-Host "Making the installer with Inno Setup ..."
if (-not $Iscc) {
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                     "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if (Test-Path $c) { $Iscc = $c; break }
    }
}
if (-not $Iscc) { throw "Inno Setup 6 (ISCC.exe) not found; install it (winget install JRSoftware.InnoSetup) or pass -Iscc" }
& $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$App" "/DOutputDir=$Release" (Join-Path $PSScriptRoot "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

Write-Host "Writing SHA256SUMS.txt ..."
$sums = foreach ($f in @($Setup, $Zip)) { "{0}  {1}" -f (Get-FileHash $f -Algorithm SHA256).Hash.ToLower(), (Split-Path $f -Leaf) }
$sums | Set-Content (Join-Path $Release "SHA256SUMS.txt") -Encoding ASCII
foreach ($f in @($Setup, $Zip)) { Write-Host ("  {0} ({1:N0} MB)" -f (Split-Path $f -Leaf), ((Get-Item $f).Length / 1MB)) }
Write-Host "Release files are in $Release"
