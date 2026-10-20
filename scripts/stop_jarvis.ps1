# stop_jarvis.ps1 -- stops the JARVIS backend (8340) and front end (5173).
#
# Tree-kills each PID start_jarvis.ps1 recorded and, in case a PID file is
# stale, whatever python/node process is listening on those two ports (npm runs
# Vite as a child; the server runs the brain and Claude Code as children).

$state = Join-Path $env:LOCALAPPDATA 'JARVIS\launcher'
$ids = @()
foreach ($name in 'frontend', 'server') {
    $pidFile = Join-Path $state "$name.pid"
    if (Test-Path $pidFile) {
        $ids += Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
}
$ids += Get-NetTCPConnection -LocalPort 8340, 5173 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess

foreach ($id in ($ids | Where-Object { $_ } | Sort-Object -Unique)) {
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -in 'python', 'node', 'cmd') {
        & taskkill.exe /PID $id /T /F 2>$null | Out-Null
    }
}
