[CmdletBinding()]
param(
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DataDir = Join-Path $ProjectRoot "data"

try {
    $Existing = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 2
    if ($Existing.status -eq "ok") {
        exit 0
    }
} catch {
    # No running manager was found; continue with startup.
}

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Runtime is not installed. Run scripts\setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
Write-Host "Xianyu manager starting: http://127.0.0.1:8765" -ForegroundColor Green
if ($Background) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    $OutLog = Join-Path $DataDir "server.out.log"
    $ErrorLog = Join-Path $DataDir "server.err.log"
    $ServerProcess = Start-Process `
        -FilePath $VenvPython `
        -ArgumentList @("-m", "uvicorn", "xianyu_manager.app:app", "--host", "127.0.0.1", "--port", "8765") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrorLog `
        -Wait `
        -PassThru
    exit $ServerProcess.ExitCode
} else {
    Write-Host "Keep this window open; press Ctrl+C to stop."
    & $VenvPython -m uvicorn xianyu_manager.app:app --host 127.0.0.1 --port 8765
}
