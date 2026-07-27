$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"

Write-Host "=== obrik-tools: Windows setup ==="

$PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $PyLauncher) {
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "Python 3 not found and winget is unavailable. Install Python 3 from python.org."
    }
    Write-Host "Installing Python 3.13 with winget..."
    & winget.exe install --exact --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install Python (exit code $LASTEXITCODE)."
    }
    $Candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"),
        (Join-Path $env:WINDIR "py.exe")
    )
    $PyPath = $Candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $PyPath) {
        throw "Python installed, but py.exe was not found. Reopen the installer."
    }
} else {
    $PyPath = $PyLauncher.Source
}

$BundledDfu = Join-Path $ScriptDir "dfu-util.exe"
if (-not (Test-Path $BundledDfu) -and -not (Get-Command dfu-util.exe -ErrorAction SilentlyContinue)) {
    Write-Warning "dfu-util.exe not found. DFU steps 0/1/2 will be unavailable."
}

& $PyPath -3 -m venv (Join-Path $ScriptDir ".venv")
if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv." }
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $VenvPython -m pip install -r (Join-Path $ScriptDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Failed to install requirements.txt." }

Write-Host ""
Write-Host "Check:"
Write-Host "  & `"$VenvPython`" `"$ScriptDir\obrik_flash.py`" --dry-run"
Write-Host "Run:"
Write-Host "  & `"$VenvPython`" `"$ScriptDir\obrik_flash.py`" --steps all"
