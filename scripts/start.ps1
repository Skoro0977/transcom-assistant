$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-env.ps1')
& 'D:\CodexData\Projects\transcom-assistant\.venv\Scripts\python.exe' -B -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
