param(
    [switch]$Portable,
    [ValidateRange(1024, 65535)][int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-env.ps1') -Portable:$Portable
$env:TRANSCOM_LOCAL_PORT = [string]$Port
$pythonPath = Join-Path (Get-Location).Path '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Run scripts/setup.ps1 with the same -Portable option first' }
& $pythonPath -B -m apps.api.manage
if ($LASTEXITCODE -ne 0) { throw 'Database preparation failed' }
& $pythonPath -B -m uvicorn apps.api.main:app --host 127.0.0.1 --port $Port
if ($LASTEXITCODE -ne 0) { throw 'Local server stopped with an error; read the message above' }
