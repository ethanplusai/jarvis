# start_jarvis.ps1 -- one click (or "start Jarvis") on Windows.
#
# Starts the backend (server.py, port 8340) and the Vite front end (port 5173)
# in the background unless they are already up, waits for both, then opens
# Chrome on the voice page. Logs and PIDs go to %LOCALAPPDATA%\JARVIS\launcher;
# stop_jarvis.ps1 stops what this started.
#
# Vite is pinned to 5173 (--strictPort): Chrome's microphone grant is scoped to
# the port, and OriginGuard trusts localhost:5173, so drifting to 5174 would
# silently break the mic.

param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$state = Join-Path $env:LOCALAPPDATA 'JARVIS\launcher'
New-Item -ItemType Directory -Force $state | Out-Null
$url = 'http://localhost:5173'

function Show-Error([string]$text) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($text, 'JARVIS', 'OK', 'Error') | Out-Null
    exit 1
}

# Both loopbacks: Vite binds ::1 only, the server 127.0.0.1 only.
function Test-Port([int]$port) {
    foreach ($addr in [System.Net.IPAddress]::Loopback, [System.Net.IPAddress]::IPv6Loopback) {
        $c = New-Object System.Net.Sockets.TcpClient($addr.AddressFamily)
        try { $c.Connect($addr, $port); return $true } catch { } finally { $c.Close() }
    }
    return $false
}

function Wait-Port([int]$port, [int]$seconds, $proc, [string]$log) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $port) { return }
        if ($proc -and $proc.HasExited) { Show-Error "JARVIS failed to start (port $port). See $log" }
        Start-Sleep -Milliseconds 500
    }
    Show-Error "JARVIS did not come up on port $port within $seconds s. See $log"
}

# npm: from PATH, else the portable install in ~\tools\node.
$npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
if (-not $npm) {
    $portable = Join-Path $HOME 'tools\node\npm.cmd'
    if (Test-Path $portable) { $npm = $portable; $env:Path = (Split-Path $portable) + ';' + $env:Path }
    else { Show-Error 'Node.js (npm) was not found. Install Node 18+ and try again.' }
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { Show-Error "No virtualenv at $python. Run the setup in README.md first." }

$env:PYTHONUTF8 = '1'

if (-not (Test-Port 8340)) {
    $log = Join-Path $state 'server.log'
    $p = Start-Process -FilePath $python -ArgumentList 'server.py', '--host', '127.0.0.1' `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $state 'server.err.log')
    Set-Content -Path (Join-Path $state 'server.pid') -Value $p.Id
    Wait-Port 8340 60 $p (Join-Path $state 'server.err.log')
}

if (-not (Test-Port 5173)) {
    $log = Join-Path $state 'frontend.log'
    $p = Start-Process -FilePath $npm -ArgumentList 'run', 'dev', '--', '--port', '5173', '--strictPort' `
        -WorkingDirectory (Join-Path $root 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError (Join-Path $state 'frontend.err.log')
    Set-Content -Path (Join-Path $state 'frontend.pid') -Value $p.Id
    Wait-Port 5173 60 $p (Join-Path $state 'frontend.err.log')
}

if ($NoBrowser) { exit 0 }

# Chrome specifically: the microphone needs its Web Speech API.
$chrome = $null
foreach ($key in 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
                 'HKLM:\Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe') {
    if (-not $chrome -and (Test-Path $key)) { $chrome = (Get-ItemProperty $key).'(default)' }
}
if ($chrome -and (Test-Path $chrome)) { Start-Process $chrome $url }
else { Start-Process $url }
