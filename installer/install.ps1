<#
.SYNOPSIS
  WorkScope Collector PowerShell installer (ASCII-only).

.DESCRIPTION
  Encoding-safe rewrite. All Write-Host strings are ASCII so the script
  loads correctly regardless of console code page. Japanese-facing
  documentation lives in operation_guide.html / pharmacy_brief.html.
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host "  WorkScope Collector  PowerShell Installer" -ForegroundColor Cyan
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Write-Host "[INFO] Script dir: $ScriptDir"

$SourceExe = Join-Path $ScriptDir "WorkScope.exe"
if (-not (Test-Path $SourceExe)) {
    Write-Host "[ERROR] WorkScope.exe not found: $SourceExe" -ForegroundColor Red
    Write-Host "        Place this script next to WorkScope.exe." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

$existing = Get-Process -Name WorkScope -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[INFO] Stopping existing WorkScope.exe..."
    Stop-Process -Name WorkScope -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

$AppRoot = Join-Path $env:APPDATA "WorkScope"
$BinDir  = Join-Path $AppRoot "bin"
$DocsDir = Join-Path $AppRoot "docs"
$LogsDir = Join-Path $AppRoot "logs"
$DataDir = Join-Path $AppRoot "data"

Write-Host ""
Write-Host "[1/5] Creating folders..."
$null = New-Item -ItemType Directory -Force -Path $BinDir
$null = New-Item -ItemType Directory -Force -Path $DocsDir
$null = New-Item -ItemType Directory -Force -Path $LogsDir
$null = New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "screenshots")
$null = New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "events")

Write-Host "[2/5] Copying WorkScope.exe..."
Copy-Item -Path $SourceExe -Destination (Join-Path $BinDir "WorkScope.exe") -Force

Write-Host "[3/5] Copying documents..."
Get-ChildItem -Path $ScriptDir -Filter "*.html" -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $DocsDir -Force
    Write-Host "      copied: $($_.Name)"
}
$readme = Join-Path $ScriptDir "README_install.txt"
if (Test-Path $readme) {
    Copy-Item -Path $readme -Destination $DocsDir -Force
    Write-Host "      copied: README_install.txt"
}

Write-Host "[4/5] Registering autostart (HKCU\Run)..."
$exePath = Join-Path $BinDir "WorkScope.exe"
$runKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$null = New-ItemProperty -Path $runKey -Name "WorkScope" `
    -Value "`"$exePath`"" -PropertyType String -Force
Write-Host "      registry: $runKey\WorkScope"

Write-Host "[5/5] Launching WorkScope.exe..."
Start-Process -FilePath $exePath
Start-Sleep -Seconds 2

$running = Get-Process -Name WorkScope -ErrorAction SilentlyContinue
if (-not $running) {
    Write-Host ""
    Write-Host "[WARN] WorkScope.exe launched but the process is not running." -ForegroundColor Yellow
    Write-Host "       Check logs: $LogsDir" -ForegroundColor Yellow
    if (Test-Path (Join-Path $LogsDir "main.log")) {
        Write-Host ""
        Write-Host "--- main.log tail ---" -ForegroundColor Yellow
        Get-Content -Path (Join-Path $LogsDir "main.log") -Tail 20
    }
}

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Green
Write-Host "  Install Complete" -ForegroundColor Green
Write-Host "===============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Bin     : $BinDir"
Write-Host "  Data    : $DataDir"
Write-Host "  Docs    : $DocsDir"
Write-Host "  Logs    : $LogsDir"
Write-Host ""
Write-Host "  * Check the system tray (bottom right) for the GREEN dot."
Write-Host "  * WorkScope will auto-start on next logon."
Write-Host ""
