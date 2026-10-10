"""What a Claude Code child of JARVIS is, and is not, given to inherit.

Measured 2026-09-26: JARVIS started from inside a Claude Code session
(`scripts/start-jarvis.ps1` run by an agent) handed that session's own
variables to the brain's `claude -p`. The scrub removed CLAUDE_CODE_* and
ANTHROPIC_*, and nothing else a session exports. `SESSION_EXPORTS` below is
that environment, by name, with made-up values: every name in it was read
off a live session (desktop app 2.7032.0, CLI 2.1.280), off the desktop
app's own env builder, or off the CLI's env for its Bash and hook children.

Which of them the CLI acts on was measured against CLI 2.1.270 and 2.1.280
with `scripts/probe_child_env.py` (a real `claude -p` against a local fake
API, no login). Honoured: CLAUDE_CODE_EFFORT_LEVEL (overrides `--effort`),
CLAUDE_CODE_ENTRYPOINT and CLAUDE_AGENT_SDK_VERSION (rewrite the
User-Agent), API_TIMEOUT_MS (at 1.5 s a turn the API answered in 4 s never
finished), MCP_SERVER_CONNECTION_BATCH_SIZE (how many MCP servers
start at once), OTEL_* (exported to the inherited collector as soon as
telemetry is on). Ignored: CLAUDE_EFFORT and CLAUDE_PID, which the CLI
overwrites for its own children rather than reads, and a Claude Code
AI_AGENT, which it replaces with its own.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_env  # noqa: E402

# name -> made-up value. The groups say where each was observed.
SESSION_EXPORTS = {
    # The CLI, for every Bash and hook child it starts.
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "0f5d-session",
    "CLAUDE_CODE_CHILD_SESSION": "1",
    "CLAUDE_CODE_SESSION_ATTENDED": "1",
    "CLAUDE_PID": "4242",
    "CLAUDE_EFFORT": "xhigh",
    "AI_AGENT": "claude-code_2-1-280_agent",
    "CLAUDE_CODE_EXECPATH": r"C:\claude\claude.exe",
    "CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\cc-inbox",
    "CLAUDE_CODE_MESSAGING_TOKEN": "child-token",
    "TRACEPARENT": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "TRACESTATE": "vendor=opaque",
    # The desktop app, for the sessions it hosts.
    "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
    "CLAUDE_CODE_DESKTOP_APP_VERSION": "2.7032.0",
    "CLAUDE_CODE_HOST_SESSION_ID": "host-session",
    "CLAUDE_CODE_OAUTH_SCOPES": "user:inference",
    "CLAUDE_CODE_EAGER_FLUSH": "1",
    "CLAUDE_AGENT_SDK_VERSION": "0.3.280",
    "CLAUDE_PREVIEW_CLASSIFIER_FLOOR": "1",
    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    "USE_LOCAL_OAUTH": "",
    "USE_STAGING_OAUTH": "",
    "MCP_CONNECTION_NONBLOCKING": "true",
    "MCP_SERVER_CONNECTION_BATCH_SIZE": "8",
    "API_TIMEOUT_MS": "900000",
    "DISABLE_MICROCOMPACT": "1",
    "BAGGAGE": "sentry-trace_id=abc",
    "SENTRY-TRACE": "abc-def-1",
    # A host whose telemetry export is configured hands that on too.
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example",
    "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer collector-secret",
    "OTEL_RESOURCE_ATTRIBUTES": "host.session=abc",
}

# What any process on the machine has, and a child must keep.
ORDINARY = {
    "PATH": "path",
    "HOME": "home",
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": "temp",
    "HTTPS_PROXY": "http://proxy.example:8080",
    "NODE_EXTRA_CA_CERTS": "ca.pem",
    "FISH_API_KEY": "fish",
    "JARVIS_DATA_DIR": "data",
    "USER_OWN_SETTING": "kept",
    # Set by a session, but for git, and harmless to any child that runs git.
    "GIT_EDITOR": "true",
    # Opt-outs a user sets on purpose: they can only make a child do less.
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
}

# Machine facts that happen to live under the CLAUDE_ prefix.
KEPT = {
    "CLAUDE_CONFIG_DIR": r"C:\Users\me\.claude",
    # Where the CLI keeps the login when it is set (CLI 2.1.270 reads the
    # credentials from here and falls back to CLAUDE_CONFIG_DIR only when it
    # is not). Kept for the same reason: scrubbing one and keeping the other
    # sends a child to look for the login somewhere it is not.
    "CLAUDE_SECURESTORAGE_CONFIG_DIR": r"C:\Users\me\.claude",
    "CLAUDE_CODE_GIT_BASH_PATH": r"C:\Program Files\Git\bin\bash.exe",
}

# JARVIS's own secrets: in its environment on purpose, never a child's.
PRIVATE = {
    "GOOGLE_ADS_REFRESH_TOKEN": "private",
    "META_ACCESS_TOKEN": "private",
    "TWILIO_AUTH_TOKEN": "private",
    "OPENAI_ADS_API_KEY": "private",
    "JARVIS_OWNER_PHONE": "private",
    # The WhatsApp line: the key that could message anyone, and the number
    # of the one person it may. The brain reaches the phone only through
    # `whatsapp_message`, which the server performs.
    "KAPSO_API_KEY": "private",
    "WHATSAPP_PHONE_NUMBER_ID": "private",
    "WHATSAPP_OWNER_NUMBER": "private",
    "TELEGRAM_BOT_TOKEN": "private",
    "TELEGRAM_OWNER_ID": "private",
    # The LinkedIn apps' secrets. Only the backend (`linkedin_api`) signs in;
    # the brain reaches LinkedIn through the business desk, never with a key.
    "LINKEDIN_CLIENT_SECRET": "private",
    "LINKEDIN_ORG_CLIENT_SECRET": "private",
}


def _env(*groups):
    out = {}
    for g in groups:
        out.update(g)
    return out


# ── the scrub every Claude Code child gets ───────────────────────────────────

def test_nothing_a_claude_code_session_exports_reaches_a_child():
    child = claude_env.child_env(_env(SESSION_EXPORTS, ORDINARY, KEPT))
    leaked = sorted(set(child) & set(SESSION_EXPORTS))
    assert leaked == [], f"a Claude Code child would inherit the launching session's {leaked}"


def test_the_ordinary_environment_and_the_machine_facts_pass_untouched():
    child = claude_env.child_env(_env(SESSION_EXPORTS, ORDINARY, KEPT, PRIVATE))
    assert child == _env(ORDINARY, KEPT)


@pytest.mark.parametrize("name, why", [
    ("CLAUDE_CODE_EFFORT_LEVEL", "overrides the brain's --effort (measured: low became high)"),
    ("CLAUDE_AGENT_SDK_VERSION", "rewrites the User-Agent to claim the Agent SDK"),
    ("CLAUDE_CODE_ENTRYPOINT", "rewrites the User-Agent to claim the desktop app"),
    ("API_TIMEOUT_MS", "replaces the CLI's API timeout (1500 ms: retried until killed)"),
    ("MCP_SERVER_CONNECTION_BATCH_SIZE", "changes how many MCP servers start at once"),
    ("MCP_TIMEOUT", "changes how long an MCP server may take to start"),
    ("OTEL_EXPORTER_OTLP_ENDPOINT", "where telemetry goes once it is on"),
    ("OTEL_EXPORTER_OTLP_HEADERS", "the collector's credentials"),
    ("USE_STAGING_OAUTH", "points the Claude in Chrome bridge at staging"),
    ("USE_LOCAL_OAUTH", "points the Claude in Chrome bridge at localhost"),
    ("CLAUDE_TRUSTED_DEVICE_TOKEN", "a credential of the launching session"),
    ("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "changes when the brain compacts"),
    ("ANTHROPIC_API_KEY", "moves billing off the subscription"),
])
def test_each_variable_the_cli_acts_on_is_scrubbed(name, why):
    assert name not in claude_env.child_env({name: "x", "PATH": "p"}), why


def test_a_scrubbed_name_is_scrubbed_whatever_its_value():
    """An empty value is still a value: `USE_STAGING_OAUTH=""` is how the
    desktop app says "not staging", and the next host may say otherwise."""
    assert claude_env.child_env({"USE_STAGING_OAUTH": "", "PATH": "p"}) == {"PATH": "p"}


def test_is_scrubbed_is_the_rule_child_env_applies():
    for name in _env(SESSION_EXPORTS, ORDINARY, KEPT, PRIVATE):
        assert claude_env.is_scrubbed(name) == (name not in claude_env.child_env({name: "x"}))


# ── what whoever started JARVIS handed down ──────────────────────────────────

def test_inherited_names_are_the_session_exports_and_never_jarvis_own_secrets():
    """The launcher clears these from the backend's own environment. It must
    not clear the business credentials: the backend reads those itself."""
    names = claude_env.inherited_names(_env(SESSION_EXPORTS, ORDINARY, KEPT, PRIVATE))
    assert names == sorted(SESSION_EXPORTS)


def test_inherited_names_is_empty_for_a_plain_terminal():
    assert claude_env.inherited_names(_env(ORDINARY, KEPT, PRIVATE)) == []


def test_session_markers_name_a_launch_from_inside_claude_code():
    assert claude_env.session_markers(_env(SESSION_EXPORTS, ORDINARY)) == [
        "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"]


def test_a_user_setting_alone_is_not_a_session():
    """MCP_TIMEOUT in a user's own profile is scrubbed from children, but it
    is not evidence that a Claude Code session started JARVIS."""
    assert claude_env.session_markers({"MCP_TIMEOUT": "60000", "PATH": "p"}) == []


# ── the launcher's question, asked of the one copy of the rule ──────────────

def _inherited_cli(env: dict[str, str]) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not claude_env.is_scrubbed(k)}
    base.update(env)
    return subprocess.run([sys.executable, str(ROOT / "claude_env.py"), "--inherited"],
                          env=base, capture_output=True, text=True, timeout=60)


def test_the_launcher_gets_names_one_per_line_and_never_a_value():
    out = _inherited_cli(_env(SESSION_EXPORTS, PRIVATE, KEPT))
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == sorted(SESSION_EXPORTS)
    for value in ("child-token", "collector-secret", "private"):
        assert value not in out.stdout


def test_the_launcher_gets_nothing_at_all_from_a_clean_environment():
    """Not even a blank line: the launcher treats every line as a name."""
    out = _inherited_cli({})
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


def test_the_launcher_entry_point_refuses_anything_else():
    out = subprocess.run([sys.executable, str(ROOT / "claude_env.py")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode != 0
    assert "--inherited" in out.stderr


# ── scripts/start-jarvis.ps1: the backend itself starts clean ────────────────

LAUNCHER = ROOT / "scripts" / "start-jarvis.ps1"

# Runs the launcher's OWN function, taken out of the script by PowerShell's
# parser, around a child that writes down the environment it was given;
# then writes down the caller's environment after the function returned.
_LAUNCHER_HARNESS = r'''
param([string]$Launcher, [string]$Python, [string]$Root, [string]$Dumper,
      [string]$ChildOut, [string]$AfterOut)
$ErrorActionPreference = "Stop"
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$tokens, [ref]$errors)
if ($errors) { throw "start-jarvis.ps1 does not parse: $($errors -join '; ')" }
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                  $n.Name -eq "Invoke-WithoutInheritedEnv" }, $true)
if (-not $fn) { throw "start-jarvis.ps1 defines no Invoke-WithoutInheritedEnv" }
. ([scriptblock]::Create($fn.Extent.Text))
Invoke-WithoutInheritedEnv $Python $Root {
    Start-Process -FilePath $Python -ArgumentList ('"{0}" "{1}"' -f $Dumper, $ChildOut) -Wait -NoNewWindow
}
& $Python $Dumper $AfterOut
'''


@pytest.mark.skipif(os.name != "nt", reason="the launcher is Windows PowerShell")
def test_the_launcher_starts_jarvis_without_the_launching_session(tmp_path):
    import shutil
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell is not on PATH")
    harness = tmp_path / "harness.ps1"
    harness.write_text(_LAUNCHER_HARNESS, encoding="utf-8")
    dumper = tmp_path / "dump_env.py"
    dumper.write_text("import json, os, sys\n"
                      "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n",
                      encoding="utf-8")
    child_out, after_out = tmp_path / "child.json", tmp_path / "after.json"
    env = {k: v for k, v in os.environ.items() if not claude_env.is_inherited(k)}
    env.update(_env(SESSION_EXPORTS, PRIVATE, KEPT))

    out = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness),
         "-Launcher", str(LAUNCHER), "-Python", sys.executable, "-Root", str(ROOT),
         "-Dumper", str(dumper), "-ChildOut", str(child_out), "-AfterOut", str(after_out)],
        env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr

    child = json.loads(child_out.read_text(encoding="utf-8"))
    assert sorted(set(child) & set(SESSION_EXPORTS)) == []
    # The backend reads its own business credentials; the launcher leaves them.
    assert {k: child.get(k) for k in PRIVATE} == PRIVATE
    assert {k: child.get(k) for k in KEPT} == KEPT
    # And the shell that ran the launcher gets every one of them back. An
    # empty value is the exception: .NET cannot set one, so it comes back
    # absent, which every reader of these treats as the same thing.
    after = json.loads(after_out.read_text(encoding="utf-8"))
    assert {k: after.get(k) for k, v in SESSION_EXPORTS.items() if v} == \
           {k: v for k, v in SESSION_EXPORTS.items() if v}


def test_the_launcher_starts_both_halves_inside_the_clean_environment():
    """A process started outside the block would inherit everything."""
    text = LAUNCHER.read_text(encoding="utf-8")
    body = text.split("Invoke-WithoutInheritedEnv $python $root {", 1)
    assert len(body) == 2, "start-jarvis.ps1 no longer wraps its starts"
    assert text.count('Start-Detached "') == 2
    assert 'Start-Detached "backend"' in body[1] and 'Start-Detached "frontend"' in body[1]



# ── the one `claude` JARVIS opens in a window of its own ────────────────────

@pytest.mark.asyncio
async def test_the_login_terminal_starts_claude_without_the_backends_secrets(
        monkeypatch, tmp_path):
    """The diagnostics "open login" repair runs `claude` in a new terminal
    so the user can sign in again. On Windows that terminal inherited the
    backend's whole environment: an ANTHROPIC_API_KEY loaded from .env — the
    one the CLI prefers over the subscription login this repair exists to
    fix — and, for a backend started from inside a Claude Code session,
    that session's variables. It gets the same scrubbed environment as
    every other Claude Code child."""
    import importlib
    import types
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-dotenv")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "high")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"C:\Users\me\.claude")
    seen = {}

    async def fake_open_terminal(command="", env=None):
        seen.update(command=command, env=env)
        return {"success": True}

    monkeypatch.setattr(server.actions, "open_terminal", fake_open_terminal)
    monkeypatch.setattr(server, "run_executor_instance",
                        types.SimpleNamespace(_closing=False))

    await server._repair_service("open-login")

    assert seen["command"] == "claude"
    env = seen["env"]
    assert env is not None, "the login terminal must not inherit the backend's environment"
    for name in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_EFFORT_LEVEL"):
        assert name not in env, name
    assert env.get("CLAUDE_CONFIG_DIR") == r"C:\Users\me\.claude", "the login's own home stays"
    assert env.get("PATH") == os.environ.get("PATH")
