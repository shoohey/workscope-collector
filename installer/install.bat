@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

rem ============================================================
rem  WorkScope Collector  インストーラ
rem  - インストール先: %APPDATA%\WorkScope\bin
rem  - スタートアップ: HKCU\...\Run （ユーザー権限でOK）
rem ============================================================

set "APP_NAME=WorkScope"
set "APP_ROOT=%APPDATA%\%APP_NAME%"
set "BIN_DIR=%APP_ROOT%\bin"
set "DATA_DIR=%APP_ROOT%\data"
set "DOCS_DIR=%APP_ROOT%\docs"
set "LOGS_DIR=%APP_ROOT%\logs"
set "INSTALL_LOG=%LOGS_DIR%\install.log"
set "SCRIPT_DIR=%~dp0"

rem ---- ログディレクトリを先に作る ----
if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%" >nul 2>&1

call :log "==================================================="
call :log "WorkScope Collector インストール開始"
call :log "実行日時: %DATE% %TIME%"
call :log "ユーザー: %USERNAME%"
call :log "コンピュータ: %COMPUTERNAME%"
call :log "スクリプト場所: %SCRIPT_DIR%"
call :log "==================================================="

echo.
echo ===================================================
echo   WorkScope Collector インストーラ
echo ===================================================
echo.

rem ---- 管理者権限チェック（任意） ----
net session >nul 2>&1
if %errorLevel% == 0 (
    echo [情報] 管理者権限で実行されています。
    call :log "管理者権限: あり"
) else (
    echo [情報] 管理者権限ではありませんが、このインストーラはユーザー権限でも動作します。
    echo        （%%APPDATA%% にインストールするためです）
    call :log "管理者権限: なし（ユーザー権限で続行）"
)
echo.

rem ---- 配布同梱の WorkScope.exe を確認 ----
if not exist "%SCRIPT_DIR%WorkScope.exe" (
    echo [エラー] WorkScope.exe が見つかりません。
    echo         このバッチと同じフォルダに WorkScope.exe を置いてください。
    call :log "エラー: WorkScope.exe が見つからない (%SCRIPT_DIR%WorkScope.exe)"
    pause
    exit /b 1
)

rem ---- 既存プロセスを停止（上書きインストール対応） ----
tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" >nul
if %errorLevel% == 0 (
    echo [情報] 既存の WorkScope.exe を停止します...
    taskkill /F /IM WorkScope.exe /T >nul 2>&1
    call :log "既存プロセス停止"
    timeout /t 2 /nobreak >nul
)

rem ---- ディレクトリ作成 ----
echo [1/5] ディレクトリを作成しています...
call :log "ディレクトリ作成開始"
if not exist "%BIN_DIR%"                  mkdir "%BIN_DIR%"
if not exist "%DATA_DIR%\screenshots"     mkdir "%DATA_DIR%\screenshots"
if not exist "%DATA_DIR%\events"          mkdir "%DATA_DIR%\events"
if not exist "%DOCS_DIR%"                 mkdir "%DOCS_DIR%"
if not exist "%LOGS_DIR%"                 mkdir "%LOGS_DIR%"
call :log "  bin: %BIN_DIR%"
call :log "  data/screenshots: %DATA_DIR%\screenshots"
call :log "  data/events: %DATA_DIR%\events"
call :log "  docs: %DOCS_DIR%"
call :log "  logs: %LOGS_DIR%"

rem ---- 実行ファイルをコピー ----
echo [2/5] 実行ファイルをコピーしています...
copy /Y "%SCRIPT_DIR%WorkScope.exe" "%BIN_DIR%\WorkScope.exe" >nul
if not exist "%BIN_DIR%\WorkScope.exe" (
    echo [エラー] WorkScope.exe のコピーに失敗しました。
    call :log "エラー: WorkScope.exe のコピーに失敗"
    pause
    exit /b 1
)
call :log "WorkScope.exe コピー完了 -> %BIN_DIR%\WorkScope.exe"

rem ---- ドキュメントをコピー（存在するものだけ） ----
echo [3/5] ドキュメントをコピーしています...
if exist "%SCRIPT_DIR%consent_form.html" (
    copy /Y "%SCRIPT_DIR%consent_form.html" "%DOCS_DIR%\consent_form.html" >nul
    call :log "consent_form.html コピー"
)
if exist "%SCRIPT_DIR%operation_guide.html" (
    copy /Y "%SCRIPT_DIR%operation_guide.html" "%DOCS_DIR%\operation_guide.html" >nul
    call :log "operation_guide.html コピー"
)
if exist "%SCRIPT_DIR%README_install.txt" (
    copy /Y "%SCRIPT_DIR%README_install.txt" "%DOCS_DIR%\README_install.txt" >nul
    call :log "README_install.txt コピー"
)

rem ---- スタートアップ登録（HKCU\Run） ----
echo [4/5] Windows スタートアップに登録しています...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope /t REG_SZ /d "\"%BIN_DIR%\WorkScope.exe\"" /f >nul
if %errorLevel% == 0 (
    call :log "スタートアップ登録 成功 (HKCU\Software\Microsoft\Windows\CurrentVersion\Run\WorkScope)"
) else (
    echo [警告] スタートアップ登録に失敗しました。手動で登録が必要です。
    call :log "警告: スタートアップ登録に失敗"
)

rem ---- 完了 ----
echo [5/5] インストールが完了しました。
call :log "インストール完了"
echo.
echo ===================================================
echo   インストール完了
echo ===================================================
echo.
echo インストール先 : %BIN_DIR%
echo データ保存先   : %DATA_DIR%
echo ドキュメント   : %DOCS_DIR%
echo ログ           : %INSTALL_LOG%
echo.
echo --- 次にやること ---
echo  1. %DOCS_DIR%\consent_form.html を印刷し、
echo     薬剤師さんに署名してもらってください。
echo  2. 同意取得後にこの画面の任意のキーを押すと
echo     WorkScope が起動し、次回ログオン時から自動起動します。
echo.
pause
echo.
echo WorkScope を起動しています...
call :log "WorkScope 起動"
start "" "%BIN_DIR%\WorkScope.exe"

endlocal
exit /b 0

:log
rem 引数 1 を install.log に追記
echo [%DATE% %TIME%] %~1 >> "%INSTALL_LOG%"
goto :eof
