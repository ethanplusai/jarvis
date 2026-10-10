"""Windows desktop adapters. Fixed PowerShell source; inputs travel as JSON.

Screen capture is explicit and in-memory. Helpers run without console windows;
only the open-terminal action deliberately creates an interactive terminal.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess


def _windows_powershell_env():
    """The backend's environment minus PSModulePath. Started from a pwsh 7
    terminal, the backend carries 7's module path, and 5.1 then loads 7's
    module manifests ahead of its own. Measured 2026-10-10 with pwsh 7.6.6:
    Utility and Management happen to work that way, Get-Acl fails with
    CouldNotAutoloadMatchingModule. Left unset, 5.1 builds its own."""
    return {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}


async def powershell(script, payload=None, timeout=10):
    prefix = ("$ErrorActionPreference='Stop'; "
              "[Console]::InputEncoding=[Text.UTF8Encoding]::new(); "
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
              "$p=[Console]::In.ReadToEnd() | ConvertFrom-Json; ")
    encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode()
    proc = await asyncio.create_subprocess_exec(
        shutil.which("powershell.exe") or "powershell.exe", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", encoded, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_windows_powershell_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload or {}).encode()), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        raise OSError(err.decode(errors="replace")[:500])
    return out.decode("utf-8-sig").strip()


async def open_terminal(command="", env=None):
    """A new console window running `command`. `env`, when given, is the
    window's whole environment; None inherits the backend's, as a user's own
    project command should."""
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
    argv = [shell, "-NoProfile", "-NoExit"]
    if command:
        argv += ["-EncodedCommand", base64.b64encode(command.encode("utf-16-le")).decode()]
    await asyncio.to_thread(subprocess.Popen, argv, env=env,
                            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


def browser_binary(name):
    relative = {"chrome": ("Google/Chrome/Application/chrome.exe",),
                "firefox": ("Mozilla Firefox/firefox.exe",),
                "edge": ("Microsoft/Edge/Application/msedge.exe",)}.get(name)
    if not relative:
        raise ValueError("Unknown browser")
    found = shutil.which(Path(relative[0]).name)
    if found:
        return found
    for root in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        for suffix in relative:
            candidate = Path(os.getenv(root, "")) / suffix
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError(f"{name} is not installed")


async def open_browser(url, browser):
    from urllib.parse import urlsplit
    if urlsplit(url).scheme not in ("https", "http", "file"):
        raise ValueError("Unsupported URL scheme")
    await asyncio.to_thread(subprocess.Popen, [browser_binary(browser), url],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


async def open_editor(path):
    for root in ("LOCALAPPDATA", "ProgramFiles"):
        candidate = Path(os.getenv(root, "")) / (
            "Programs/Microsoft VS Code/Code.exe" if root == "LOCALAPPDATA"
            else "Microsoft VS Code/Code.exe")
        if candidate.is_file():
            await asyncio.to_thread(subprocess.Popen, [str(candidate), str(path)])
            return "VS Code"
    executable = "explorer.exe" if Path(path).is_dir() else "notepad.exe"
    await asyncio.to_thread(subprocess.Popen, [executable, str(path)])
    return "File Explorer" if Path(path).is_dir() else "Notepad"


_WINDOWS = r'''
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class JarvisWindow { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow(); }';
$front=[JarvisWindow]::GetForegroundWindow();
$rows=@(Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 12 | ForEach-Object {
    @{app=$_.ProcessName; title=$_.MainWindowTitle; frontmost=($_.MainWindowHandle -eq $front)}
}); ConvertTo-Json -InputObject $rows -Compress
'''


async def list_windows():
    return json.loads(await powershell(_WINDOWS))


_CAPTURE = r'''
Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing;
$screens=[System.Windows.Forms.Screen]::AllScreens;
$selected=[System.Windows.Forms.Screen]::PrimaryScreen;
if ($p.display) { if ($p.display -lt 1 -or $p.display -gt $screens.Length) { throw 'Display does not exist' }; $selected=$screens[$p.display-1] };
$bounds=$selected.Bounds; $bitmap=New-Object System.Drawing.Bitmap($bounds.Width,$bounds.Height);
$graphics=[System.Drawing.Graphics]::FromImage($bitmap); $small=$null; $stream=New-Object System.IO.MemoryStream;
try {
  $graphics.CopyFromScreen($bounds.Location,[System.Drawing.Point]::Empty,$bounds.Size);
  $scale=[Math]::Min(1.0,1280.0/[Math]::Max($bounds.Width,$bounds.Height));
  $w=[Math]::Max(1,[int]($bounds.Width*$scale)); $h=[Math]::Max(1,[int]($bounds.Height*$scale));
  $small=New-Object System.Drawing.Bitmap($bitmap,$w,$h);
  $colors=New-Object 'System.Collections.Generic.HashSet[int]';
  for ($x=0; $x -lt $w; $x+=[Math]::Max(1,[int]($w/32))) {
    for ($y=0; $y -lt $h; $y+=[Math]::Max(1,[int]($h/32))) { [void]$colors.Add($small.GetPixel($x,$y).ToArgb()) }
  }; if ($colors.Count -lt 2) { throw 'Screen is blank or unavailable in this desktop session' };
  $small.Save($stream,[System.Drawing.Imaging.ImageFormat]::Png);
  @{png=[Convert]::ToBase64String($stream.ToArray()); width=$w; height=$h} | ConvertTo-Json -Compress
} finally { $graphics.Dispose(); $bitmap.Dispose(); if ($small) {$small.Dispose()}; $stream.Dispose() }
'''


async def capture_screen(display=None):
    result = json.loads(await powershell(_CAPTURE, {"display": display}))
    result["png"] = base64.b64decode(result["png"], validate=True)
    if len(result["png"]) > 4_000_000:
        raise ValueError("Screenshot exceeds the image size limit")
    return result


_NOTIFY = r'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;
$xml=New-Object Windows.Data.Xml.Dom.XmlDocument;
$title=[System.Security.SecurityElement]::Escape($p.title);
$message=[System.Security.SecurityElement]::Escape($p.message);
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>'+$title+'</text><text>'+$message+'</text></binding></visual></toast>');
$toast=[Windows.UI.Notifications.ToastNotification]::new($xml);
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell').Show($toast)
'''


async def notify(title, message):
    await powershell(_NOTIFY, {"title": title[:120], "message": message[:300]})
