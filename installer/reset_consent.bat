@echo off
rem ===============================================================
rem  WorkScope - 同意記録リセット & 再起動
rem
rem  目的:
rem   - %APPDATA%\WorkScope\consent_signed.json を削除
rem   - WorkScope.exe を再起動して、同意ダイアログを再表示させる
rem
rem  典型ユースケース:
rem   - 旧版で同意済の端末に新版をインストールしたら同意画面が出ない時
rem   - パイロットを一度やり直したい時
rem   - 同意書の文面が変わって再同意が必要な時
rem ===============================================================
setlocal

set "APP_NAME=WorkScope"
set "APP_ROOT=%APPDATA%\%APP_NAME%"
set "CONSENT_FILE=%APP_ROOT%\consent_signed.json"
set "BIN=%APP_ROOT%\bin\WorkScope.exe"
set "LOGS_DIR=%APP_ROOT%\logs"
set "RESET_LOG=%LOGS_DIR%\reset_consent.log"

if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%" >nul 2>&1

echo.
echo ===============================================================
echo   WorkScope - Consent Reset
echo ===============================================================
echo.

echo [%DATE% %TIME%] reset_consent start (user=%USERNAME% pc=%COMPUTERNAME%) >> "%RESET_LOG%"

rem --- 1. 起動中の WorkScope.exe を停止 ---
tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" >nul
if %errorlevel% equ 0 (
    echo [1/3] Stopping running WorkScope.exe...
    taskkill /F /IM WorkScope.exe /T >nul 2>&1
    echo [%DATE% %TIME%] killed existing process >> "%RESET_LOG%"
    timeout /t 2 /nobreak >nul
) else (
    echo [1/3] No running WorkScope.exe (skip kill)
)

rem --- 2. consent_signed.json を削除 ---
if exist "%CONSENT_FILE%" (
    echo [2/3] Deleting consent record:
    echo        %CONSENT_FILE%
    del /F /Q "%CONSENT_FILE%"
    if not exist "%CONSENT_FILE%" (
        echo        [OK] deleted.
        echo [%DATE% %TIME%] consent_signed.json deleted >> "%RESET_LOG%"
    ) else (
        echo        [ERROR] failed to delete. Check file permission.
        echo [%DATE% %TIME%] ERROR delete failed >> "%RESET_LOG%"
        pause
        exit /b 1
    )
) else (
    echo [2/3] consent_signed.json not found (already unsigned)
    echo [%DATE% %TIME%] consent file not present >> "%RESET_LOG%"
)

rem --- 3. WorkScope.exe を再起動 ---
if exist "%BIN%" (
    echo [3/3] Launching WorkScope.exe...
    start "" "%BIN%"
    echo [%DATE% %TIME%] launched WorkScope.exe >> "%RESET_LOG%"
) else (
    echo [3/3] [ERROR] WorkScope.exe not installed:
    echo        %BIN%
    echo        Run install.bat first.
    echo [%DATE% %TIME%] ERROR exe missing >> "%RESET_LOG%"
    pause
    exit /b 1
)

echo.
echo ===============================================================
echo   Reset Complete
echo ===============================================================
echo.
echo   Watch the screen for the consent dialog.
echo   ^(Look for a window titled "WorkScope - Riyou Doi"^)
echo.
echo   Logs    : %LOGS_DIR%
echo   Doc     : %APP_ROOT%\docs
echo.
pause
endlocal
exit /b 0
