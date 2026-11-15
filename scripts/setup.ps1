param([switch]$Portable)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-env.ps1') -Portable:$Portable
$projectPath = (Get-Location).Path
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $pythonExecutable = $launcher.Source
        $pythonArguments = @('-3.11')
    } else {
        $launcher = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $launcher) { throw 'Python 3.11 is required: https://www.python.org/downloads/release/python-3119/' }
        $pythonExecutable = $launcher.Source
        $pythonArguments = @()
    }
    $version = & $pythonExecutable @pythonArguments -B -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
    if ($LASTEXITCODE -ne 0 -or $version -ne '3.11') { throw 'Install Python 3.11, reopen PowerShell and rerun setup.ps1' }
    & $pythonExecutable @pythonArguments -B -m venv (Join-Path $projectPath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the project Python environment' }
}
$version = & $pythonPath -B -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
if ($LASTEXITCODE -ne 0 -or $version -ne '3.11') { throw 'The project .venv must use Python 3.11' }
if ($Portable) {
    $runtimeRoot = Join-Path $projectPath 'data\local\runtime'
    $nodeRuntime = Join-Path $runtimeRoot 'node-v26.13.1-win-x64'
    if (-not (Test-Path -LiteralPath (Join-Path $nodeRuntime 'node.exe'))) {
        if (-not [Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows system is required' }
        $downloads = Join-Path $projectPath 'data\local\downloads'
        New-Item -ItemType Directory -Path $downloads -Force | Out-Null
        New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
        $archive = Join-Path $downloads 'node-v26.13.1-win-x64.zip'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri 'https://nodejs.org/dist/v26.13.1/node-v26.13.1-win-x64.zip' -OutFile $archive
        $expected = 'e2edda75bfb089654705cf65a13b58933bfd5f57148e8ebda916f58a2474f1f7'
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw 'Node archive SHA256 does not match the pinned official release'
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $runtimeRoot -Force
    }
} else {
    $nodeRuntime = 'D:\CodexData\Runtimes\node\node-v26.13.1-win-x64'
}
$nodePath = Join-Path $nodeRuntime 'node.exe'
if (-not (Test-Path -LiteralPath $nodePath)) { throw 'Node runtime is missing; use -Portable to install it inside the project' }
if ((& $nodePath --version) -ne 'v26.13.1') { throw 'Node 26.13.1 is required' }
. (Join-Path $PSScriptRoot 'dev-env.ps1') -Portable:$Portable
& $pythonPath -B -m pip install --disable-pip-version-check -r (Join-Path $projectPath 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Python dependency install failed' }
$npmPath = Join-Path $nodeRuntime 'npm.cmd'
& $npmPath --prefix (Join-Path $projectPath 'apps\web') ci --include=optional
if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency install failed' }
& $npmPath --prefix (Join-Path $projectPath 'apps\web') run build
if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
& $npmPath --prefix (Join-Path $projectPath 'tools\ocr') ci --ignore-scripts
if ($LASTEXITCODE -ne 0) { throw 'Local OCR dependency install failed' }
$envPath = Join-Path $projectPath '.env'
if (-not (Test-Path -LiteralPath $envPath)) { Copy-Item -LiteralPath (Join-Path $projectPath '.env.example') -Destination $envPath }
Write-Output 'Setup complete. Run scripts/start.ps1 with the same -Portable option used for setup.'
