<#
.SYNOPSIS
  WorkScope Collector PowerShell アンインストーラ

.DESCRIPTION
  WorkScope を完全に削除する。データの保持/削除を選択可能。
  USBから .\uninstall.ps1 で実行する想定。
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "  WorkScope Collector  PowerShell Uninstaller" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""

$AppRoot = Join-Path $env:APPDATA "WorkScope"
if (-not (Test-Path $AppRoot)) {
    Write-Host "[INFO] WorkScope はインストールされていないようです: $AppRoot"
    Read-Host "Press Enter to exit"
    exit 0
}

# プロセス停止
$existing = Get-Process -Name WorkScope -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[1/3] 起動中の WorkScope を停止しています..."
    Stop-Process -Name WorkScope -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# スタートアップ解除
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$value  = Get-ItemProperty -Path $runKey -Name WorkScope -ErrorAction SilentlyContinue
if ($value) {
    Write-Host "[2/3] スタートアップ登録を解除しています..."
    Remove-ItemProperty -Path $runKey -Name WorkScope -Force -ErrorAction SilentlyContinue
}

# データ削除確認
Write-Host ""
Write-Host "[3/3] データ削除の確認"
Write-Host ""
Write-Host "  収集データ（スクリーンショット・イベントログ）も削除しますか？"
Write-Host "    Y = すべて削除（$AppRoot を完全削除）"
Write-Host "    N = プログラムだけ削除し、収集データは残す"
Write-Host ""
$ans = Read-Host "削除しますか？ (Y/N)"

if ($ans -eq "Y" -or $ans -eq "y") {
    Write-Host "      [INFO] 完全削除を実行..."
    try {
        Remove-Item -Path $AppRoot -Recurse -Force -ErrorAction Stop
        Write-Host "      [OK] $AppRoot を削除しました。"
    } catch {
        Write-Host "      [WARN] 一部のファイルが削除できませんでした: $_" -ForegroundColor Yellow
        Write-Host "             手動で削除してください: $AppRoot" -ForegroundColor Yellow
    }
} else {
    $BinDir  = Join-Path $AppRoot "bin"
    $DocsDir = Join-Path $AppRoot "docs"
    Write-Host "      [INFO] プログラムのみ削除（データは保持）..."
    if (Test-Path $BinDir)  { Remove-Item -Path $BinDir  -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $DocsDir) { Remove-Item -Path $DocsDir -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Host "      [OK] プログラムを削除しました。"
    Write-Host "      [INFO] 収集データは残っています: $(Join-Path $AppRoot 'data')"
}

Write-Host ""
Write-Host "===================================================" -ForegroundColor Green
Write-Host "  アンインストール完了" -ForegroundColor Green
Write-Host "===================================================" -ForegroundColor Green
Write-Host ""
