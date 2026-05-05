@echo off
rem ===============================================================
rem  WorkScope Collector Installer (ASCII-only for max compatibility)
rem  - Right-click -> Run as administrator (admin not strictly required,
rem    but recommended to suppress UAC prompts on subsequent steps)
rem  - Installs to %APPDATA%\WorkScope (per-user, no admin needed)
rem  - Registers HKCU\...\Run for autostart at logon
rem ===============================================================
setlocal

set "APP_NAME=WorkScope"
set "APP_ROOT=%APPDATA%\%APP_NAME%"
set "BIN_DIR=%APP_ROOT%\bin"
set "DATA_DIR=%APP_ROOT%\data"
set "DOCS_DIR=%APP_ROOT%\docs"
set "LOGS_DIR=%APP_ROOT%\logs"
set "SCRIPT_DIR=%~dp0"

if not exist "%LOGS_DIR%" mkdir "%LOGS_DIR%" >nul 2>&1
set "INSTALL_LOG=%LOGS_DIR%\install.log"

echo [%DATE% %TIME%] WorkScope Collector install start >> "%INSTALL_LOG%"
echo [%DATE% %TIME%] User: %USERNAME% / Computer: %COMPUTERNAME% >> "%INSTALL_LOG%"
echo [%DATE% %TIME%] Script dir: %SCRIPT_DIR% >> "%INSTALL_LOG%"

echo.
echo ===============================================================
echo   WorkScope Collector  Installer
echo ===============================================================
echo.

rem --- Verify bundled WorkScope.exe ---
if not exist "%SCRIPT_DIR%WorkScope.exe" (
    echo [ERROR] WorkScope.exe not found: %SCRIPT_DIR%WorkScope.exe
    echo         Place install.bat in the same folder as WorkScope.exe.
    echo [%DATE% %TIME%] ERROR: WorkScope.exe missing >> "%INSTALL_LOG%"
    pause
    exit /b 1
)

rem --- Stop existing process if running ---
tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" >nul
if %errorlevel% equ 0 (
    echo [INFO] Stopping existing WorkScope.exe...
    taskkill /F /IM WorkScope.exe /T >nul 2>&1
    echo [%DATE% %TIME%] Stopped existing process >> "%INSTALL_LOG%"
    timeout /t 2 /nobreak >nul
)

rem --- Create folders ---
echo [1/5] Creating folders...
if not exist "%BIN_DIR%"                  mkdir "%BIN_DIR%"
if not exist "%DATA_DIR%\screenshots"     mkdir "%DATA_DIR%\screenshots"
if not exist "%DATA_DIR%\events"          mkdir "%DATA_DIR%\events"
if not exist "%DOCS_DIR%"                 mkdir "%DOCS_DIR%"

rem --- Copy executable ---
echo [2/5] Copying WorkScope.exe...
copy /Y "%SCRIPT_DIR%WorkScope.exe" "%BIN_DIR%\WorkScope.exe" >nul
if not exist "%BIN_DIR%\WorkScope.exe" (
    echo [ERROR] Failed to copy WorkScope.exe.
    echo [%DATE% %TIME%] ERROR: copy failed >> "%INSTALL_LOG%"
    pause
    exit /b 1
)

rem --- Copy documents ---
echo [3/5] Copying documents...
if exist "%SCRIPT_DIR%consent_form.html"        copy /Y "%SCRIPT_DIR%consent_form.html"        "%DOCS_DIR%\" >nul
if exist "%SCRIPT_DIR%operation_guide.html"     copy /Y "%SCRIPT_DIR%operation_guide.html"     "%DOCS_DIR%\" >nul
if exist "%SCRIPT_DIR%data_handling_policy.html" copy /Y "%SCRIPT_DIR%data_handling_policy.html" "%DOCS_DIR%\" >nul
if exist "%SCRIPT_DIR%pharmacy_brief.html"      copy /Y "%SCRIPT_DIR%pharmacy_brief.html"      "%DOCS_DIR%\" >nul
if exist "%SCRIPT_DIR%README_install.txt"       copy /Y "%SCRIPT_DIR%README_install.txt"       "%DOCS_DIR%\" >nul

rem --- Register autostart ---
echo [4/5] Registering autostart (HKCU)...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope /t REG_SZ /d "\"%BIN_DIR%\WorkScope.exe\"" /f >nul

rem --- Launch ---
echo [5/5] Launching WorkScope.exe...
start "" "%BIN_DIR%\WorkScope.exe"
echo [%DATE% %TIME%] Install complete >> "%INSTALL_LOG%"

echo.
echo ===============================================================
echo   Install Complete
echo ===============================================================
echo.
echo   Bin     : %BIN_DIR%
echo   Data    : %DATA_DIR%
echo   Docs    : %DOCS_DIR%
echo   Logs    : %LOGS_DIR%
echo.
echo   * Check the system tray (bottom right corner) for the GREEN dot.
echo   * WorkScope will auto-start on next logon.
echo.
pause
endlocal
exit /b 0
