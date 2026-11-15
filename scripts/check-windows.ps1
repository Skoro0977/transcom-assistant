param([ValidateRange(1024, 65535)][int]$Port = 8002)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-env.ps1') -Portable
$pythonPath = Join-Path (Get-Location).Path '.venv\Scripts\python.exe'
$logs = Join-Path $env:TEMP 'startup-check'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw ('Port ' + $Port + ' is already in use; the check requires a fresh server')
}
$launcherPath = (Get-Process -Id $PID).Path
$startPath = Join-Path $PSScriptRoot 'start.ps1'
$server = Start-Process -FilePath $launcherPath -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $startPath + '"'), '-Portable', '-Port', ([string]$Port)) -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'stdout.log') -RedirectStandardError (Join-Path $logs 'stderr.log') -PassThru
try {
    & $pythonPath -B -m scripts.check_local_server --url ('http://127.0.0.1:' + $Port)
    if ($LASTEXITCODE -ne 0) { throw 'Local startup check failed' }
    $server.Refresh()
    if ($server.HasExited) { throw 'The created server launcher exited unexpectedly' }
    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen
    $listenerProcess = [int]$listener.OwningProcess
    $owned = $false
    for ($depth = 0; $depth -lt 12 -and $listenerProcess -gt 0; $depth++) {
        if ($listenerProcess -eq $server.Id) { $owned = $true; break }
        $entry = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $listenerProcess)
        if (-not $entry) { break }
        $listenerProcess = [int]$entry.ParentProcessId
    }
    if (-not $owned) { throw 'The responding listener does not belong to the created server' }
} finally {
    # Only terminate descendants of the specific launcher created by this check.
    function Stop-CheckProcess([int]$ProcessId) {
        $children = @(Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $ProcessId))
        foreach ($child in $children) { Stop-CheckProcess -ProcessId $child.ProcessId }
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    Stop-CheckProcess -ProcessId $server.Id
}
