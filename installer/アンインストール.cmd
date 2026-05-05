@echo off
rem ===============================================================
rem  WorkScope Collector  Double-click Uninstaller Launcher
rem  Just double-click this file.
rem ===============================================================

cd /d "%~dp0"

if exist "%~dp0uninstall.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
) else (
    call "%~dp0uninstall.bat"
)

pause
