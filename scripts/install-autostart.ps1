<#
.SYNOPSIS
Start JARVIS whenever you sign in to Windows -- or stop doing so (-Remove).

.DESCRIPTION
Registers a scheduled task, "JARVIS start at sign-in", for the current user.
When that user signs in it waits a moment, then runs scripts\start-jarvis.ps1
in a hidden window and appends that script's output to
data\logs\autostart.log, stamped with the time.

At sign-in, not at boot. JARVIS needs your desktop: your Claude login,
Chrome's microphone, your screen and your windows. None of that exists
before you sign in, and a task there needs no administrator rights. Measured
before this was written: a process Start-Process launches from a scheduled
task's action outlives the task, so the backend and frontend keep running
after the task that started them has finished.

Safe to run again: it replaces the task. Safe at sign-in with JARVIS already
running: start-jarvis.ps1 leaves a half that is listening alone. The task
runs at normal priority and on battery -- Task Scheduler's defaults would run
it below normal priority, which the backend and the brain inherit, and would
not start it on a laptop that is unplugged.

.PARAMETER Root
The JARVIS checkout to start. Defaults to the one this script is in.

.PARAMETER DelaySeconds
How long after sign-in to wait before starting, so the network and the
desktop are up first. Default 30.

.PARAMETER Remove
Unregister the task. JARVIS will no longer start at sign-in.

.PARAMETER PrintXml
Print the task definition Windows would be given, and register nothing.

.EXAMPLE
.\scripts\install-autostart.ps1

.EXAMPLE
.\scripts\install-autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    # Resolved below, not here: Windows PowerShell 5.1 leaves $PSScriptRoot
    # empty while it evaluates a param() default.
    [string]$Root = "",
    [ValidateRange(0, 3600)][int]$DelaySeconds = 30,
    [switch]$Remove,
    [switch]$PrintXml
)

$ErrorActionPreference = "Stop"
$TaskName = "JARVIS start at sign-in"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed: JARVIS will no longer start at sign-in"
    } else {
        Write-Host "nothing to remove: there is no '$TaskName' task"
    }
    return
}

if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
    throw "No such folder: $Root (it must hold scripts\start-jarvis.ps1)"
}
$Root = (Resolve-Path -LiteralPath $Root).ProviderPath
$start = Join-Path $Root "scripts\start-jarvis.ps1"
if (-not (Test-Path -LiteralPath $start -PathType Leaf)) {
    throw "$Root is not a JARVIS checkout: there is no scripts\start-jarvis.ps1 in it"
}
$logDir = Join-Path $Root "data\logs"
$log = Join-Path $logDir "autostart.log"
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# A PowerShell single-quoted literal: nothing inside is expanded, and the one
# character that ends it is doubled. A checkout at C:\Users\o'brien\jarvis is
# a real path, not a hypothetical.
function Quote([string]$text) { "'" + $text.Replace("'", "''") + "'" }

# The action's own command. No double quote anywhere in it, so it rides inside
# the one pair that -Command's argument is wrapped in. Every write goes
# through Add-Content -Encoding UTF8: Windows PowerShell 5.1's `*>>` appends
# UTF-16, and a log with one line in each encoding reads as noise.
$command = "New-Item -ItemType Directory -Force -Path $(Quote $logDir) | Out-Null; " +
           "Add-Content -LiteralPath $(Quote $log) -Encoding UTF8 -Value ('--- sign-in start ' + (Get-Date -Format s)); " +
           "& $(Quote $start) *>&1 | Add-Content -LiteralPath $(Quote $log) -Encoding UTF8"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$command`""

function Esc([string]$text) { [System.Security.SecurityElement]::Escape($text) }

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts JARVIS (backend and frontend) when $(Esc $user) signs in, by running $(Esc $start). Installed by scripts\install-autostart.ps1; remove with that script's -Remove.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(Esc $user)</UserId>
      <Delay>PT$($DelaySeconds)S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(Esc $user)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$(Esc $arguments)</Arguments>
      <WorkingDirectory>$(Esc $Root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

if ($PrintXml) {
    Write-Output $xml
    return
}

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Host "installed: JARVIS starts $DelaySeconds s after $user signs in"
Write-Host "  runs:  $start"
Write-Host "  log:   $log"
Write-Host "  remove with: .\scripts\install-autostart.ps1 -Remove"
