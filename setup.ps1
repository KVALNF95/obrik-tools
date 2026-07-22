$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"

Write-Host "=== obrik-tools: Windows setup ==="

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher (py.exe) not found. Install Python 3 from python.org."
}

if (-not (Get-Command dfu-util.exe -ErrorAction SilentlyContinue)) {
    Write-Warning "dfu-util.exe is not in PATH. Install it before DFU steps 0/1/2."
}

& py -3 -m venv (Join-Path $ScriptDir ".venv")
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install pymavlink pyserial

Write-Host ""
Write-Host "Check:"
Write-Host "  & `"$VenvPython`" `"$ScriptDir\obrik_flash.py`" --dry-run"
Write-Host "Run:"
Write-Host "  & `"$VenvPython`" `"$ScriptDir\obrik_flash.py`" --steps all"

