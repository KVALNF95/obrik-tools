@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\setup.ps1"
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" ".\obrik_flash.py" %*
pause

