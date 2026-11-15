param([switch]$Portable)

$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($taskRoot -notmatch '^[A-Za-z]:\\') { throw 'Use a local Windows drive for the project' }
if (-not $Portable -and -not $taskRoot.StartsWith('D:\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Project must stay on D; on another computer pass -Portable to setup.ps1, start.ps1 and dev-env.ps1'
}
$env:TRANSCOM_PORTABLE = if ($Portable) { 'true' } else { 'false' }
if ($Portable) {
    $tempRoot = Join-Path $taskRoot 'data\local\dev\temp'
    $cacheRoot = Join-Path $taskRoot 'data\local\dev\cache'
    $nodeRuntime = Join-Path $taskRoot 'data\local\runtime\node-v26.13.1-win-x64'
} else {
    $tempRoot = 'D:\CodexData\Temp\transcom-build'
    $cacheRoot = 'D:\CodexData\Caches\transcom'
    $nodeRuntime = 'D:\CodexData\Runtimes\node\node-v26.13.1-win-x64'
}
foreach ($folder in @($tempRoot, $cacheRoot)) {
    if (-not (Test-Path -LiteralPath $folder)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
}
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:TMPDIR = $tempRoot
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PIP_CACHE_DIR = Join-Path $cacheRoot 'pip'
$env:UV_CACHE_DIR = Join-Path $cacheRoot 'uv'
$env:npm_config_cache = Join-Path $cacheRoot 'npm'
$env:npm_config_prefix = Join-Path $cacheRoot 'npm-global'
$env:npm_config_engine_strict = 'true'
$env:NODE_COMPILE_CACHE = Join-Path $cacheRoot 'node-compile'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $cacheRoot 'browsers'
$env:TRANSCom_ROOT = $taskRoot
if (Test-Path -LiteralPath (Join-Path $nodeRuntime 'node.exe')) {
    $env:PATH = $nodeRuntime + ';' + $env:PATH
}
Set-Location -LiteralPath $taskRoot
