# SPDX-License-Identifier: GPL-3.0-only
# Builds the Windows bundle and installer of Ostinato:
#   dist\Ostinato\                         the programs (Ostinato.exe: the tray; ostinato-cli.exe: everything else)
#   dist\Ostinato-Setup-<version>.exe      the installer (needs Inno Setup 6: winget install JRSoftware.InnoSetup)
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
# Options: -SkipInstaller (only the bundle)
param([switch]$SkipInstaller)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# a build environment of its own (the developer's .venv may be in use by a running device server)
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Root "build\venv"
Write-Host "== build environment" -ForegroundColor Cyan
uv sync --frozen --no-dev --group build
if ($LASTEXITCODE) { throw "uv sync failed" }
$Py = Join-Path $env:UV_PROJECT_ENVIRONMENT "Scripts\python.exe"
$Version = & $Py -c "import importlib.metadata as m; print(m.version('ostinato'))"

Write-Host "== icon" -ForegroundColor Cyan
New-Item -ItemType Directory -Force (Join-Path $Root "build") | Out-Null
& $Py -m ostinato.icon (Join-Path $Root "build\ostinato.ico")
if ($LASTEXITCODE) { throw "icon failed" }

Write-Host "== bundle (PyInstaller) $Version" -ForegroundColor Cyan
& $Py -m PyInstaller installer\ostinato.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
if ($LASTEXITCODE) { throw "PyInstaller failed" }

Write-Host "== check the bundle" -ForegroundColor Cyan
$Cli = Join-Path $Root "dist\Ostinato\ostinato-cli.exe"
& $Cli --help | Out-Null
if ($LASTEXITCODE) { throw "the bundled ostinato-cli.exe does not start" }

if ($SkipInstaller) { Write-Host "Bundle: dist\Ostinato\"; exit 0 }

$Iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    Write-Warning "Inno Setup 6 not found (winget install JRSoftware.InnoSetup): the bundle is in dist\Ostinato\, no installer."
    exit 0
}
Write-Host "== installer (Inno Setup)" -ForegroundColor Cyan
& $Iscc "/DAppVersion=$Version" "/DRoot=$Root" installer\ostinato.iss
if ($LASTEXITCODE) { throw "Inno Setup failed" }
Write-Host "Installer: dist\Ostinato-Setup-$Version.exe" -ForegroundColor Green
