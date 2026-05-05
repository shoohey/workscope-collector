<#
.SYNOPSIS
  WorkScope Collector PowerShell インストーラ

.DESCRIPTION
  install.bat の文字エンコーディング互換問題を回避するための PowerShell 版。
  USBから .\install.ps1 で実行する想定。管理者権限は不要（%APPDATA% に
  インストールするため）。

.NOTES
  実行方法:
    PowerShell を開く → USBに cd → 以下のいずれかで実行
      .\install.ps1
      または
      powershell -ExecutionPolicy Bypass -File .\install.ps1
#>

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "  WorkScope Collector  PowerShell Installer" -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host ""

# このスクリプトの場所
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Write-Host "[INFO] Script dir: $ScriptDir"

# 配布同梱の WorkScope.exe を確認
$SourceExe = Join-Path $ScriptDir "WorkScope.exe"
if (-not (Test-Path $SourceExe)) {
    Write-Host "[ERROR] WorkScope.exe が見つかりません: $SourceExe" -ForegroundColor Red
    Write-Host "        このスクリプトと同じフォルダに WorkScope.exe を置いてください。" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# 既存プロセスを停止
$existing = Get-Process -Name WorkScope -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[INFO] 既存の WorkScope.exe を停止します..."
    Stop-Process -Name WorkScope -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# インストール先
$AppRoot = Join-Path $env:APPDATA "WorkScope"
$BinDir  = Join-Path $AppRoot "bin"
$DocsDir = Join-Path $AppRoot "docs"
$LogsDir = Join-Path $AppRoot "logs"
$DataDir = Join-Path $AppRoot "data"

Write-Host ""
Write-Host "[1/5] フォルダを作成しています..."
$null = New-Item -ItemType Directory -Force -Path $BinDir
$null = New-Item -ItemType Directory -Force -Path $DocsDir
$null = New-Item -ItemType Directory -Force -Path $LogsDir
$null = New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "screenshots")
$null = New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "events")

Write-Host "[2/5] WorkScope.exe をコピーしています..."
Copy-Item -Path $SourceExe -Destination (Join-Path $BinDir "WorkScope.exe") -Force

Write-Host "[3/5] ドキュメントをコピーしています..."
Get-ChildItem -Path $ScriptDir -Filter "*.html" -ErrorAction SilentlyContinue | ForEach-Object {
    Copy-Item -Path $_.FullName -Destination $DocsDir -Force
    Write-Host "      copied: $($_.Name)"
}
$readme = Join-Path $ScriptDir "README_install.txt"
if (Test-Path $readme) {
    Copy-Item -Path $readme -Destination $DocsDir -Force
    Write-Host "      copied: README_install.txt"
}

Write-Host "[4/5] Windows スタートアップに登録しています..."
$exePath = Join-Path $BinDir "WorkScope.exe"
$runKey  = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$null = New-ItemProperty -Path $runKey -Name "WorkScope" `
    -Value "`"$exePath`"" -PropertyType String -Force
Write-Host "      registry: $runKey\WorkScope"

Write-Host "[5/5] WorkScope を起動しています..."
Start-Process -FilePath $exePath
Start-Sleep -Seconds 2

# プロセスが起動したか確認
$running = Get-Process -Name WorkScope -ErrorAction SilentlyContinue
if (-not $running) {
    Write-Host ""
    Write-Host "[WARN] WorkScope.exe を起動しましたが、プロセスが見つかりません。" -ForegroundColor Yellow
    Write-Host "       ログを確認してください: $LogsDir" -ForegroundColor Yellow
    if (Test-Path (Join-Path $LogsDir "main.log")) {
        Write-Host ""
        Write-Host "--- main.log の末尾 ---" -ForegroundColor Yellow
        Get-Content -Path (Join-Path $LogsDir "main.log") -Tail 20
    }
}

Write-Host ""
Write-Host "===================================================" -ForegroundColor Green
Write-Host "  インストール完了" -ForegroundColor Green
Write-Host "===================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Bin     : $BinDir"
Write-Host "  Data    : $DataDir"
Write-Host "  Docs    : $DocsDir"
Write-Host "  Logs    : $LogsDir"
Write-Host ""
Write-Host "★ タスクトレイ右下の上向き矢印 (^) をクリックして"
Write-Host "  緑色の丸が出ていればインストール成功です。"
Write-Host ""
Write-Host "★ 次回以降、Windowsログオン時に自動起動します。"
Write-Host ""
