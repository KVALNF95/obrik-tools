@echo off
setlocal
cd /d "%~dp0"
title obrik-tools Windows installer

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\setup.ps1"
if errorlevel 1 (
  echo.
  echo INSTALLATION FAILED
  pause
  exit /b 1
)

echo.
echo INSTALLATION COMPLETE. Start RUN_WINDOWS.cmd to flash a drone.
pause
