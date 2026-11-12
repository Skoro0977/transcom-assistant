$taskRoot = 'D:\CodexData\Projects\transcom-assistant'
$tempRoot = 'D:\CodexData\Temp\transcom-build'
$cacheRoot = 'D:\CodexData\Caches\transcom'
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
$nodeRuntime = 'D:\CodexData\Runtimes\node\node-v26.13.1-win-x64'
if (Test-Path -LiteralPath (Join-Path $nodeRuntime 'node.exe')) {
    $env:PATH = $nodeRuntime + ';' + $env:PATH
}
Set-Location -LiteralPath $taskRoot
