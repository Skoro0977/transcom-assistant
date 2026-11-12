$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-env.ps1')
$projectPath = (Get-Location).Path
if (-not $projectPath.StartsWith('D:\')) { throw 'Project must stay on D' }
if (-not (Test-Path -LiteralPath 'D:\CodexData\Runtimes\node\node-v26.13.1-win-x64\node.exe')) {
    throw 'Install the documented portable Node 26 runtime on D before setup'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectPath '.venv\Scripts\python.exe'))) {
    python -B -m venv (Join-Path $projectPath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment on D' }
}
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
& $pythonPath -B -m pip install --disable-pip-version-check -r (Join-Path $projectPath 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency install failed' }
npm --prefix (Join-Path $projectPath 'apps\web') ci
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed' }
npm --prefix (Join-Path $projectPath 'apps\web') run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
