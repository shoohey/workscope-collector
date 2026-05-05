@echo off
rem ===============================================================
rem  WorkScope Collector  Double-click Installer Launcher
rem
rem  This .cmd is the recommended entry point for non-technical users.
rem  Just double-click this file. No right-click required.
rem  It calls install.ps1 with -ExecutionPolicy Bypass so users do
rem  not need to change system policy.
rem ===============================================================

cd /d "%~dp0"

if exist "%~dp0install.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
) else (
    rem Fallback: if install.ps1 missing, run install.bat
    call "%~dp0install.bat"
)

pause
