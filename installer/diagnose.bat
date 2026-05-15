@echo off
rem ===============================================================
rem  WorkScope - 起動失敗の切り分け診断ツール
rem
rem  「タスクトレイに緑色アイコンが出ない」「同意画面が出ない」等の
rem  起動異常時、まずこの bat を実行してログ・状態を吐き出させる。
rem ===============================================================
setlocal EnableDelayedExpansion

set "APP_NAME=WorkScope"
set "APP_ROOT=%APPDATA%\%APP_NAME%"
set "BIN=%APP_ROOT%\bin\WorkScope.exe"
set "LOGS=%APP_ROOT%\logs"
set "CONSENT=%APP_ROOT%\consent_signed.json"
set "REPORT=%LOGS%\diagnose_%RANDOM%.txt"

if not exist "%LOGS%" mkdir "%LOGS%" >nul 2>&1

echo. > "%REPORT%"
echo === WorkScope Diagnose Report === >> "%REPORT%"
echo Generated: %DATE% %TIME%        >> "%REPORT%"
echo User    : %USERNAME%            >> "%REPORT%"
echo PC      : %COMPUTERNAME%        >> "%REPORT%"
echo OS      : %OS%                  >> "%REPORT%"
echo.                                >> "%REPORT%"

echo.
echo ===============================================================
echo   WorkScope Diagnose
echo ===============================================================
echo.

rem --- 1. 起動状態 ---
echo [1] Process check >> "%REPORT%"
echo [1] Process check
tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" > nul
if !errorlevel! equ 0 (
    echo     [OK] WorkScope.exe is RUNNING
    echo     [OK] WorkScope.exe is RUNNING >> "%REPORT%"
    tasklist /FI "IMAGENAME eq WorkScope.exe" >> "%REPORT%"
) else (
    echo     [WARN] WorkScope.exe is NOT running
    echo     [WARN] WorkScope.exe is NOT running >> "%REPORT%"
)
echo. >> "%REPORT%"

rem --- 2. インストール状態 ---
echo [2] Installation check >> "%REPORT%"
echo [2] Installation check
if exist "%BIN%" (
    echo     [OK] WorkScope.exe installed
    echo     [OK] WorkScope.exe installed >> "%REPORT%"
    for %%F in ("%BIN%") do (
        echo         size : %%~zF bytes
        echo         mtime: %%~tF
        echo         size : %%~zF bytes >> "%REPORT%"
        echo         mtime: %%~tF >> "%REPORT%"
    )
) else (
    echo     [NG]  WorkScope.exe NOT found: %BIN%
    echo     [NG]  WorkScope.exe NOT found: %BIN% >> "%REPORT%"
    echo           --^> run install.bat first
    echo           --^> run install.bat first >> "%REPORT%"
)
if exist "%CONSENT%" (
    echo     [INFO] consent_signed.json present
    echo     [INFO] consent_signed.json present >> "%REPORT%"
) else (
    echo     [INFO] consent_signed.json absent ^(unsigned^)
    echo     [INFO] consent_signed.json absent ^(unsigned^) >> "%REPORT%"
)
echo. >> "%REPORT%"

rem --- 3. ログファイル一覧 ---
echo [3] Log files >> "%REPORT%"
echo [3] Log files
if exist "%LOGS%" (
    dir /B /O-D "%LOGS%\*.log" 2>nul >> "%REPORT%"
    dir /B /O-D "%LOGS%\*.log" 2>nul
) else (
    echo     [no logs dir]
    echo     [no logs dir] >> "%REPORT%"
)
echo. >> "%REPORT%"

rem --- 4. main.log 末尾 50行 ---
echo [4] main.log tail 50 lines >> "%REPORT%"
echo. >> "%REPORT%"
echo [4] main.log tail (50 lines)
echo --------------------------------------
echo --------------------------------------  >> "%REPORT%"
if exist "%LOGS%\main.log" (
    powershell -NoProfile -Command "Get-Content -Path '%LOGS%\main.log' -Tail 50" >> "%REPORT%" 2>&1
    powershell -NoProfile -Command "Get-Content -Path '%LOGS%\main.log' -Tail 50"
) else (
    echo [no main.log -- WorkScope.exe never reached main()]
    echo [no main.log -- WorkScope.exe never reached main()] >> "%REPORT%"
)
echo --------------------------------------
echo. >> "%REPORT%"
echo -------------------------------------- >> "%REPORT%"
echo. >> "%REPORT%"

rem --- 5. crash.log ---
echo [5] crash.log >> "%REPORT%"
echo. >> "%REPORT%"
echo [5] crash.log
echo --------------------------------------
echo --------------------------------------  >> "%REPORT%"
if exist "%LOGS%\crash.log" (
    type "%LOGS%\crash.log" >> "%REPORT%"
    type "%LOGS%\crash.log"
) else (
    echo [no crash.log -- good sign]
    echo [no crash.log] >> "%REPORT%"
)
echo --------------------------------------
echo. >> "%REPORT%"
echo -------------------------------------- >> "%REPORT%"
echo. >> "%REPORT%"

rem --- 6. collector.log 末尾 30行 ---
echo [6] collector.log tail 30 lines >> "%REPORT%"
echo. >> "%REPORT%"
echo [6] collector.log tail (30 lines)
echo --------------------------------------
echo --------------------------------------  >> "%REPORT%"
if exist "%LOGS%\collector.log" (
    powershell -NoProfile -Command "Get-Content -Path '%LOGS%\collector.log' -Tail 30" >> "%REPORT%" 2>&1
    powershell -NoProfile -Command "Get-Content -Path '%LOGS%\collector.log' -Tail 30"
) else (
    echo [no collector.log]
    echo [no collector.log] >> "%REPORT%"
)
echo --------------------------------------
echo. >> "%REPORT%"
echo -------------------------------------- >> "%REPORT%"
echo. >> "%REPORT%"

rem --- 7. install.log 末尾 30行 ---
echo [7] install.log tail 30 lines >> "%REPORT%"
echo. >> "%REPORT%"
echo [7] install.log tail (30 lines)
if exist "%LOGS%\install.log" (
    powershell -NoProfile -Command "Get-Content -Path '%LOGS%\install.log' -Tail 30" >> "%REPORT%" 2>&1
) else (
    echo [no install.log] >> "%REPORT%"
)
echo. >> "%REPORT%"

rem --- 8. 試しに直接起動して 5秒後の状態確認 ---
echo [8] Try launching WorkScope.exe and wait 5 sec >> "%REPORT%"
echo.
echo [8] Try launching WorkScope.exe (wait 5 sec to see if it stays alive)
if exist "%BIN%" (
    start "" "%BIN%"
    timeout /t 5 /nobreak >nul
    tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" > nul
    if !errorlevel! equ 0 (
        echo     [OK] still alive after 5 sec
        echo     [OK] still alive after 5 sec >> "%REPORT%"
    ) else (
        echo     [NG] exited within 5 sec -- crash on startup
        echo     [NG] exited within 5 sec -- crash on startup >> "%REPORT%"
    )
)
echo. >> "%REPORT%"

echo.
echo ===============================================================
echo   Diagnose complete
echo ===============================================================
echo.
echo   Full report saved to:
echo     %REPORT%
echo.
echo   Please send this file to the support team.
echo.
pause
endlocal
exit /b 0
