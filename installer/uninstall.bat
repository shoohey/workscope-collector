@echo off
rem ===============================================================
rem  WorkScope Collector Uninstaller (ASCII-only)
rem ===============================================================
setlocal

set "APP_NAME=WorkScope"
set "APP_ROOT=%APPDATA%\%APP_NAME%"
set "BIN_DIR=%APP_ROOT%\bin"
set "DATA_DIR=%APP_ROOT%\data"
set "DOCS_DIR=%APP_ROOT%\docs"
set "LOGS_DIR=%APP_ROOT%\logs"

echo.
echo ===============================================================
echo   WorkScope Collector  Uninstaller
echo ===============================================================
echo.

if not exist "%APP_ROOT%" (
    echo [INFO] WorkScope is not installed: %APP_ROOT%
    pause
    exit /b 0
)

rem --- Stop process ---
echo [1/3] Stopping WorkScope process...
tasklist /FI "IMAGENAME eq WorkScope.exe" 2>nul | find /I "WorkScope.exe" >nul
if %errorlevel% equ 0 (
    taskkill /F /IM WorkScope.exe /T >nul 2>&1
    timeout /t 2 /nobreak >nul
)

rem --- Remove autostart ---
echo [2/3] Removing autostart entry...
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope >nul 2>&1
if %errorlevel% equ 0 (
    reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v WorkScope /f >nul
)

rem --- Data deletion prompt ---
echo.
echo [3/3] Data deletion confirmation
echo.
echo   Delete collected data (screenshots and event logs) too?
echo     Y = delete everything (full removal of %APP_ROOT%)
echo     N = remove program only, keep collected data
echo.
set "DELETE_DATA="
set /p DELETE_DATA="Delete? (Y/N): "

if /I "%DELETE_DATA%"=="Y" goto :full_delete
if /I "%DELETE_DATA%"=="YES" goto :full_delete
goto :partial_delete

:full_delete
echo Removing all data...
rmdir /S /Q "%APP_ROOT%" 2>nul
if exist "%APP_ROOT%" (
    echo [WARN] Some files could not be deleted. Remove manually: %APP_ROOT%
) else (
    echo [OK] %APP_ROOT% removed.
)
goto :done

:partial_delete
echo Removing program only (data kept)...
if exist "%BIN_DIR%"  rmdir /S /Q "%BIN_DIR%"
if exist "%DOCS_DIR%" rmdir /S /Q "%DOCS_DIR%"
echo [OK] Program removed. Data kept at: %DATA_DIR%

:done
echo.
echo ===============================================================
echo   Uninstall Complete
echo ===============================================================
echo.
pause
endlocal
exit /b 0
