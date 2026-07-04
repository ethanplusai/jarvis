# JARVIS Windows launcher — starts backend and frontend in separate PowerShell windows.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

$Python = if (Get-Command python -ErrorAction SilentlyContinue) { "python" }
          elseif (Get-Command py -ErrorAction SilentlyContinue) { "py -3" }
          else { $null }
if (-not $Python) {
  Write-Error "Python 3 is required but was not found. Install it from https://www.python.org/downloads/ and re-run start.ps1"
  exit 1
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  Write-Error "npm is required but was not found. Install Node.js from https://nodejs.org and re-run start.ps1"
  exit 1
}

if (-not (Test-Path "$Root\.env") -and (Test-Path "$Root\.env.example")) {
  Copy-Item "$Root\.env.example" "$Root\.env"
  Write-Host "Created .env from .env.example. Add your API keys in onboarding or edit the file manually."
}

$ShortcutScript = Join-Path $Root "scripts\install_desktop_shortcut.ps1"
if ($IsWindows -or $env:OS -eq "Windows_NT") {
  try {
    if (Test-Path $ShortcutScript) {
      & $ShortcutScript | Out-Host
    }
  } catch {
    Write-Warning "Could not install desktop shortcut: $($_.Exception.Message)"
  }
}

Start-Process powershell -ArgumentList @("-NoExit", "-Command", "cd '$Root'; $Python -m pip install -r requirements.txt; $Python server.py")
Start-Process powershell -ArgumentList @("-NoExit", "-Command", "cd '$Root\frontend'; npm install; npm run dev")
Write-Host "JARVIS is starting. Open Chrome at http://localhost:5180"

# Best-effort: give the dev server a moment, then open the HUD.
Start-Sleep -Seconds 8
try { Start-Process "http://localhost:5180" } catch { }
