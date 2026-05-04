@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

rem ============================================================
rem  WorkScope Collector  アンインストーラ
rem ============================================================

set "APP_NAME=WorkScope"
set "APP_ROOT=%APPDATA%\%APP_NAME%"
set "BIN_DIR=%APP_ROOT%\bin"
set "DATA_DIR=%APP_ROOT%\data"
set "DOCS_DIR=%APP_ROOT%\docs"
set "LOGS_DIR=%APP_ROOT%\logs"
set "UNINSTALL_LOG=%LOGS_DIR%\uninstall.log"

if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%" >nul 2>&1

call :log "==================================================="
call :log "WorkScope Collector アンインストール開始"
call :log "実行日時: %DATE% %TIME%"
call :log "==================================================="

echo.
echo ===================================================
echo   WorkScope Collector アンインストーラ
echo ===================================================
echo.

if not exist "%APP_ROOT%" (
    echo [情報] WorkScope はインストールされていないようです。
    echo         （%APP_ROOT% が見つかりません）
    call :log "APP_ROOT が見つからない: %APP_ROOT%"
    pause
    exit /b 0
)

rem ---- 起動中プロセス停止 ----
echo [1/4] 起動中の WorkScope プロセスを停止しています...
tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" >nul
if %errorLevel% == 0 (
    taskkill /F /IM WorkScope.exe /T >nul 2>&1
    call :log "WorkScope.exe を停止"
    timeout /t 2 /nobreak >nul
) else (
    call :log "起動中の WorkScope.exe なし"
)

rem ---- スタートアップ解除 ----
echo [2/4] Windows スタートアップ登録を解除しています...
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope >nul 2>&1
if %errorLevel% == 0 (
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope /f >nul
    call :log "スタートアップ解除 完了"
) else (
    call :log "スタートアップ登録なし"
)

rem ---- データ削除確認 ----
echo.
echo [3/4] データ削除の確認
echo.
echo 収集したデータ（スクリーンショット・イベントログ）も削除しますか？
echo   Y = すべて削除（%APP_ROOT% を完全削除）
echo   N = プログラムだけ削除し、収集データは残す
echo.
set "DELETE_DATA="
set /p DELETE_DATA="削除しますか？ (Y/N): "

if /I "!DELETE_DATA!"=="Y" goto :full_delete
if /I "!DELETE_DATA!"=="YES" goto :full_delete
goto :partial_delete

:full_delete
echo [4/4] すべてのデータを削除しています...
call :log "ユーザー選択: 全削除"
rem ログファイルがロック中なら閉じる必要があるが、まずは削除を試みる
rem 自分自身のログ出力は full_delete 後は行わない
rmdir /S /Q "%APP_ROOT%" 2>nul
if exist "%APP_ROOT%" (
    echo [警告] 一部のファイルが削除できませんでした。手動で削除してください: %APP_ROOT%
) else (
    echo [完了] %APP_ROOT% を削除しました。
)
goto :done

:partial_delete
echo [4/4] プログラムのみ削除しています（データは保持）...
call :log "ユーザー選択: プログラムのみ削除（データ保持）"
if exist "%BIN_DIR%"  rmdir /S /Q "%BIN_DIR%"
if exist "%DOCS_DIR%" rmdir /S /Q "%DOCS_DIR%"
call :log "bin / docs を削除"
echo [完了] プログラムを削除しました。
echo         収集データは %DATA_DIR% に残っています。

:done
echo.
echo ===================================================
echo   アンインストール完了
echo ===================================================
echo.
pause
endlocal
exit /b 0

:log
echo [%DATE% %TIME%] %~1 >> "%UNINSTALL_LOG%" 2>nul
goto :eof
