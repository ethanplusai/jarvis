<#
.SYNOPSIS
Stop JARVIS — whatever start-jarvis.ps1 started, and whatever holds its ports.

.DESCRIPTION
Two ways to find each half, because one is not enough:

1. The PID start-jarvis.ps1 recorded under data\logs\. Its whole process
   TREE is stopped — npm spawns node, the backend spawns the brain and its
   MCP children.
2. The process that OWNS the port (8340 for the backend, 5173 for the
   frontend). The recorded PID is a launcher — the venv's python.exe is a
   stub around the real interpreter, npm.cmd is a shell around node — and a
   launcher can exit while its child keeps the port. Measured live: both
   PID files named processes that were gone, and both servers were still
   up. The port owner is stopped only when its command line is ours
   (server.py, or vite/npm under frontend), never an unrelated program that
   happens to sit on the same port.
#>
[CmdletBinding()]
param()

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "data\logs"

function Stop-Tree([int]$procId, [string]$why) {
    if ($procId -le 0) { return $false }
    if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { return $false }
    & taskkill /PID $procId /T /F 2>$null | Out-Null
    Write-Host "  stopped pid $procId and its children ($why)"
    return $true
}

function Get-OurPortOwner([int]$port, [string[]]$marks) {
    $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($owner in $owners) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        $cmd = [string]$proc.CommandLine
        foreach ($mark in $marks) {
            if ($cmd -like "*$mark*") { return [int]$owner }
        }
    }
    return 0
}

$halves = @(
    @{ name = "frontend"; port = 5173; marks = @("vite", "npm", "frontend") },
    @{ name = "backend";  port = 8340; marks = @("server.py") }
)

foreach ($half in $halves) {
    $name = $half.name
    Write-Host "${name}:"
    $stopped = $false

    $pidFile = Join-Path $logs "$name.pid"
    if (Test-Path $pidFile) {
        $recorded = 0
        try { $recorded = [int](Get-Content $pidFile | Select-Object -First 1) } catch { $recorded = 0 }
        if (Stop-Tree $recorded "recorded by start-jarvis.ps1") { $stopped = $true }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }

    # Whatever still owns the port after that — the launcher's orphaned child,
    # or a copy started by hand — as long as it is ours.
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        $owner = Get-OurPortOwner $half.port $half.marks
        if ($owner -le 0) { break }
        if (Stop-Tree $owner "owns port $($half.port)") { $stopped = $true }
        Start-Sleep -Milliseconds 300
    }

    if (Get-NetTCPConnection -LocalPort $half.port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "  port $($half.port) is still held by a process that is not JARVIS; left alone"
    } elseif (-not $stopped) {
        Write-Host "  not running"
    }
}
