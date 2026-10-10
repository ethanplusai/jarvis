"""Starting JARVIS when the user signs in to Windows.

JARVIS does not start by itself. After the machine rebooted at 21:57 on
2026-09-26 it stayed down until somebody ran `scripts/start-jarvis.ps1` by
hand at 23:10. `scripts/install-autostart.ps1` registers a per-user scheduled
task that runs that script at sign-in.

At sign-in, not at boot: JARVIS needs the user's desktop — his Claude login,
Chrome's microphone, his screen and windows — none of which exist before he
signs in, and running there needs no administrator rights. Measured before
this was written: a process `Start-Process` launches from a scheduled task's
action outlives the task, so the start script's two detached halves keep
running after the task that ran it has finished.

The installer builds the task definition itself and `-PrintXml` prints it
without registering anything, so these tests read exactly what Windows would
be given — and run the action's own command line against a stand-in start
script, which is the part a quoting mistake would break.
"""
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Windows Task Scheduler only")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "install-autostart.ps1"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

STUB = (
    'Write-Output "stub start-jarvis ran"\n'
    "Set-Content -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) "
    "'started.txt') -Value ok\n"
)


def _checkout(tmp_path, name="jarvis"):
    """A stand-in checkout: only what the installer and the task touch."""
    root = tmp_path / name
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "start-jarvis.ps1").write_text(STUB, encoding="utf-8")
    return root


def _installer(*args):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=120)


def _task(root, *args):
    out = _installer("-Root", str(root), "-PrintXml", *args)
    assert out.returncode == 0, out.stderr
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", out.stdout.strip())
    return ET.fromstring(text)


def _one(task, path):
    found = task.find(path, NS)
    assert found is not None, path
    return (found.text or "").strip()


def _this_user():
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name"],
        capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def test_the_task_runs_at_this_users_sign_in_not_at_boot(tmp_path):
    task = _task(_checkout(tmp_path))
    user = _this_user()
    assert _one(task, "t:Triggers/t:LogonTrigger/t:UserId").lower() == user.lower()
    assert _one(task, "t:Triggers/t:LogonTrigger/t:Delay") == "PT30S"
    assert task.find("t:Triggers/t:BootTrigger", NS) is None, (
        "at boot there is no desktop, no Claude login and no microphone")


def test_the_task_runs_as_the_user_on_his_desktop_and_never_elevated(tmp_path):
    task = _task(_checkout(tmp_path))
    assert _one(task, "t:Principals/t:Principal/t:UserId").lower() == _this_user().lower()
    assert _one(task, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
    assert _one(task, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"


def test_battery_and_priority_do_not_hold_jarvis_back(tmp_path):
    """Task Scheduler's defaults would not start the task on a laptop running
    on battery, would stop it when the charger came out, and would run it at
    priority 7 — below normal, which the backend and the brain inherit."""
    task = _task(_checkout(tmp_path))
    assert _one(task, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert _one(task, "t:Settings/t:StopIfGoingOnBatteries") == "false"
    assert 4 <= int(_one(task, "t:Settings/t:Priority")) <= 6, "normal priority"
    assert _one(task, "t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
    assert _one(task, "t:Settings/t:ExecutionTimeLimit") == "PT10M", (
        "the start script returns in seconds; its two halves outlive the task")


def test_the_action_starts_this_checkout_in_a_hidden_window(tmp_path):
    root = _checkout(tmp_path)
    task = _task(root)
    assert _one(task, "t:Actions/t:Exec/t:Command").lower() == "powershell.exe"
    args = _one(task, "t:Actions/t:Exec/t:Arguments")
    for flag in ("-NoProfile", "-ExecutionPolicy Bypass", "-WindowStyle Hidden"):
        assert flag in args, flag
    assert str(root / "scripts" / "start-jarvis.ps1") in args
    assert str(root / "data" / "logs" / "autostart.log") in args
    assert _one(task, "t:Actions/t:Exec/t:WorkingDirectory") == str(root)


def test_the_action_really_runs_the_start_script_and_logs_it(tmp_path):
    """The command line Windows will run, run: in a checkout whose path has
    a space and an apostrophe, the two characters that break quoting."""
    root = _checkout(tmp_path, "o'brien jarvis")
    task = _task(root)
    command = _one(task, "t:Actions/t:Exec/t:Command")
    args = _one(task, "t:Actions/t:Exec/t:Arguments")

    done = subprocess.run(f"{command} {args}", cwd=str(root),
                          capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stderr
    assert (root / "started.txt").exists(), "the start script did not run"
    log = (root / "data" / "logs" / "autostart.log").read_text(encoding="utf-8", errors="replace")
    assert "sign-in start" in log, log
    assert "stub start-jarvis ran" in log, log


def test_the_delay_is_adjustable(tmp_path):
    task = _task(_checkout(tmp_path), "-DelaySeconds", "90")
    assert _one(task, "t:Triggers/t:LogonTrigger/t:Delay") == "PT90S"


def test_a_folder_that_is_not_a_checkout_is_refused(tmp_path):
    out = _installer("-Root", str(tmp_path), "-PrintXml")
    assert out.returncode != 0
    assert "start-jarvis.ps1" in (out.stderr + out.stdout)


def test_the_real_checkout_is_the_default_root():
    """Run from the repository, the task starts the repository it is in."""
    out = _installer("-PrintXml")
    assert out.returncode == 0, out.stderr
    assert str(ROOT / "scripts" / "start-jarvis.ps1") in out.stdout
