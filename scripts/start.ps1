$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-env.ps1')
$pythonPath = Join-Path (Get-Location).Path '.venv\Scripts\python.exe'
& $pythonPath -B -m apps.api.manage
if ($LASTEXITCODE -ne 0) { throw 'Database preparation failed' }
& $pythonPath -B -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
