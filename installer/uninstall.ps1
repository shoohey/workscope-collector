<#
.SYNOPSIS
  WorkScope Collector PowerShell uninstaller (ASCII-only).
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host "  WorkScope Collector  PowerShell Uninstaller" -ForegroundColor Cyan
Write-Host "===============================================================" -ForegroundColor Cyan
Write-Host ""

$AppRoot = Join-Path $env:APPDATA "WorkScope"
if (-not (Test-Path $AppRoot)) {
    Write-Host "[INFO] WorkScope is not installed: $AppRoot"
    Read-Host "Press Enter to exit"
    exit 0
}

$existing = Get-Process -Name WorkScope -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[1/3] Stopping running WorkScope..."
    Stop-Process -Name WorkScope -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
} else {
    Write-Host "[1/3] No running WorkScope process."
}

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$value  = Get-ItemProperty -Path $runKey -Name WorkScope -ErrorAction SilentlyContinue
if ($value) {
    Write-Host "[2/3] Removing autostart entry..."
    Remove-ItemProperty -Path $runKey -Name WorkScope -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "[2/3] No autostart entry."
}

Write-Host ""
Write-Host "[3/3] Data deletion confirmation"
Write-Host ""
Write-Host "  Delete collected data (screenshots and event logs) too?"
Write-Host "    Y = delete everything (full removal of $AppRoot)"
Write-Host "    N = remove program only, keep collected data"
Write-Host ""
$ans = Read-Host "Delete? (Y/N)"

if ($ans -eq "Y" -or $ans -eq "y") {
    Write-Host "      [INFO] Removing all data..."
    try {
        Remove-Item -Path $AppRoot -Recurse -Force -ErrorAction Stop
        Write-Host "      [OK] Removed: $AppRoot"
    } catch {
        Write-Host "      [WARN] Some files could not be deleted: $_" -ForegroundColor Yellow
        Write-Host "             Remove manually: $AppRoot" -ForegroundColor Yellow
    }
} else {
    $BinDir  = Join-Path $AppRoot "bin"
    $DocsDir = Join-Path $AppRoot "docs"
    Write-Host "      [INFO] Removing program only (data kept)..."
    if (Test-Path $BinDir)  { Remove-Item -Path $BinDir  -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $DocsDir) { Remove-Item -Path $DocsDir -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Host "      [OK] Program removed."
    Write-Host "      [INFO] Data kept at: $(Join-Path $AppRoot 'data')"
}

Write-Host ""
Write-Host "===============================================================" -ForegroundColor Green
Write-Host "  Uninstall Complete" -ForegroundColor Green
Write-Host "===============================================================" -ForegroundColor Green
Write-Host ""
