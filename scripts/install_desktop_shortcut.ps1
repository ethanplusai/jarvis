# Installs a JARVIS desktop shortcut for the current Windows user.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "JARVIS by AI Evolution Labs.lnk"
$Launcher = Join-Path $Root "start.ps1"
$IconCandidate = Join-Path $Root "jarvis.ico"

# Best-effort brand icon (writes jarvis.ico at the repo root).
if (-not (Test-Path $IconCandidate)) {
  $Python = Get-Command python -ErrorAction SilentlyContinue
  if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
  if ($Python) {
    try { & $Python.Source (Join-Path $Root "scripts\generate_icon.py") | Out-Null } catch { }
  }
}

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-ExecutionPolicy Bypass -NoProfile -File `"$Launcher`""
$Shortcut.WorkingDirectory = $Root
$Shortcut.WindowStyle = 1
$Shortcut.Description = "Launch JARVIS — Virtual AI Assistant by AI Evolution Labs"
if (Test-Path $IconCandidate) {
  $Shortcut.IconLocation = $IconCandidate
} else {
  $Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
}
$Shortcut.Save()
Write-Host "Installed shortcut: $ShortcutPath"
