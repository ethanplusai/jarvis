<#
.SYNOPSIS
Start JARVIS — the backend and the Vite frontend — as detached processes of your own.

.DESCRIPTION
Two processes, each in its own minimized window, each logging to data\logs\,
each with its PID recorded there for stop-jarvis.ps1. Idempotent: a port
that is already listening means that half is already running, and it is
left alone rather than started twice.

Why this exists: servers started from inside a Claude Code preview pane die
when the pane closes. That looked like "disabling the browser mic shut the
servers down"; it was the pane. These are yours and outlive any session.

They are also started without that session's environment. Run from inside
Claude Code, this script would otherwise hand JARVIS the session's own
identity, effort, MCP start-up settings, API timeout and trace; claude_env.py
decides which variables those are, and the shell that ran this gets every one
of them back afterwards.

.PARAMETER NoFrontend
Start the backend only (you are serving the built dashboard some other way).

.EXAMPLE
.\scripts\start-jarvis.ps1
#>
[CmdletBinding()]
param([switch]$NoFrontend)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Listening([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# Logs would otherwise grow without a ceiling: each start moves the previous
# run's files aside under a timestamp and keeps the newest $KEEP_LOGS of each.
$KEEP_LOGS = 5
function Rotate-Log([string]$name) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    foreach ($kind in @("out", "err")) {
        $current = Join-Path $logs "$name.$kind.log"
        if ((Test-Path $current) -and ((Get-Item $current).Length -gt 0)) {
            Move-Item -Path $current -Destination (Join-Path $logs "$name.$kind.$stamp.log") -Force
        }
        Get-ChildItem -Path $logs -Filter "$name.$kind.*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip $KEEP_LOGS |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

function Start-Detached([string]$name, [string]$file, [string[]]$argList, [string]$cwd) {
    Rotate-Log $name
    $proc = Start-Process -FilePath $file -ArgumentList $argList -WorkingDirectory $cwd `
        -WindowStyle Minimized -PassThru `
        -RedirectStandardOutput (Join-Path $logs "$name.out.log") `
        -RedirectStandardError (Join-Path $logs "$name.err.log")
    Set-Content -Path (Join-Path $logs "$name.pid") -Value $proc.Id -Encoding ascii
    Write-Host "$name started (pid $($proc.Id)); logs in $logs"
}

# A Claude Code session hands every process it starts its own identity,
# wiring and tuning, and an agent that runs this script passes all of it on:
# measured 2026-09-26, the backend carried the agent's CLAUDECODE, session id,
# effort, MCP settings, API timeout and trace. claude_env.py owns the list --
# the same rule keeps it from every Claude Code child JARVIS spawns -- so this
# asks it instead of keeping a second copy. Cleared only while $Action runs:
# Start-Process in Windows PowerShell has no environment of its own to give,
# and the shell that ran this script keeps its variables.
function Invoke-WithoutInheritedEnv([string]$Python, [string]$Root, [scriptblock]$Action) {
    $names = @()
    try {
        $names = @(& $Python (Join-Path $Root "claude_env.py") --inherited | Where-Object { $_ })
        if ($LASTEXITCODE -ne 0) { throw "claude_env.py exited with $LASTEXITCODE" }
    } catch {
        Write-Warning "could not list inherited variables ($_); starting with the environment as it is"
        $names = @()
    }
    $saved = @{}
    foreach ($name in $names) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
    if ($names.Count -gt 0) {
        Write-Host "starting without $($names.Count) inherited variable(s): $($names -join ', ')"
    }
    try {
        & $Action
    } finally {
        # An empty value comes back absent: .NET cannot set one, and every
        # reader of these treats the two alike.
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process")
        }
    }
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

Invoke-WithoutInheritedEnv $python $root {
    if (Test-Listening 8340) {
        Write-Host "backend: already listening on 8340"
    } else {
        Start-Detached "backend" $python @("server.py", "--host", "127.0.0.1") $root
    }

    if (-not $NoFrontend) {
        if (Test-Listening 5173) {
            Write-Host "frontend: already listening on 5173"
        } else {
            $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
            if (-not $npm) { $npm = "npm" }
            Start-Detached "frontend" $npm @("run", "dev") (Join-Path $root "frontend")
        }
    }
}

Write-Host "Open http://localhost:5173 in Chrome (the microphone works there only)."
