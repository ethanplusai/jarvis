"""A second button beside the microphone: his VOICE, off or on.

The mic button pauses listening and nothing else — muted, he still talks.
There was no way to have him answer in text: the Conversation panel already
showed his replies, the composer already took typed ones, but every reply
was also synthesized and played. So: two independent toggles. The mic one is
unchanged; this one silences him, and while it is off the server does not
synthesize at all (no Fish call, no audio bytes) and each sentence goes out
as a `text` frame the page renders in the Conversation panel at once.

Asserted against the source the way tests/test_hush.py is, because the
websocket handler lives inline in the loop and the page has no test runner
beyond node:test for its pure modules (see frontend/test/voicepref.test.ts).
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# --- the server ------------------------------------------------------------

def test_the_voice_frame_reaches_the_scheduler_and_nothing_else():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    branch = src[src.index('if kind == "voice":'):]
    branch = branch[:branch.index('if kind == "hush":')]
    assert "speech.set_voice(" in branch, branch
    assert "continue" in branch, "must not fall through to the other handlers"


def test_the_protocol_comment_documents_the_frame():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    assert '{"type": "voice", "on": true|false}' in src, \
        "the client→server frame list above the websocket handler must name it"


# --- the page ---------------------------------------------------------------

def _main() -> str:
    return (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8")


def test_there_is_a_second_button_and_it_is_not_the_mic_one():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    assert 'id="btn-voice"' in html
    assert 'id="btn-mute"' in html, "the microphone button is unchanged"
    main = _main()
    assert 'getElementById("btn-voice")' in main
    assert 'getElementById("btn-mute")' in main


def test_the_preference_is_told_to_the_server_on_every_connection():
    """A server restart forgets; the page does not. `socket.onOpen` is the
    one hook that fires on every (re)connection."""
    main = _main()
    assert "voiceFrame(" in main
    open_hook = main[main.index("socket.onOpen("):]
    open_hook = open_hook[:open_hook.index("\n\n")]
    assert "voiceFrame(" in open_hook, open_hook


def test_the_preference_is_loaded_and_saved_through_the_pure_module():
    main = _main()
    assert 'from "./voicepref"' in main
    assert "loadVoiceOn(" in main and "saveVoiceOn(" in main


def test_audio_that_arrives_while_his_voice_is_off_is_dropped_and_acked():
    """A chunk in flight when the toggle lands must neither play nor wedge
    the scheduler's pacing: it is acked as played and thrown away."""
    main = _main()
    audio = main[main.index('type === "audio"'):]
    audio = audio[:audio.index('type === "stop"')]
    assert "voiceOn" in audio
    assert '"played"' in audio


def test_his_text_replies_reach_the_conversation_panel_at_once():
    """The panel polls history every ten seconds; a text conversation cannot
    wait that long. Each `text` frame is shown immediately as a provisional
    reply, replaced by the durable record when the turn ends."""
    main = _main()
    text = main[main.index('type === "text"'):]
    text = text[:text.index('type === "notice"')]
    assert "conversation.showReply(" in text
    conv = (ROOT / "frontend/src/conversation.ts").read_text(encoding="utf-8")
    assert "function showReply(" in conv
    assert "function endReply(" in conv
    assert "return { submit, showReply, endReply }" in conv


def test_the_speaker_button_has_the_same_muted_style_as_the_mic():
    css = (ROOT / "frontend/src/style.css").read_text(encoding="utf-8")
    assert "#controls button.muted" in css, "one rule covers both buttons"


# --- the launcher ------------------------------------------------------------
#
# The servers I run from a Claude preview pane die with the pane. That looked
# like "disabling the mic shut the servers down"; it was the pane. These two
# scripts start and stop JARVIS as detached processes of the user's own.

SCRIPTS = ("scripts/start-jarvis.ps1", "scripts/stop-jarvis.ps1")


@pytest.mark.parametrize("rel", SCRIPTS)
def test_the_launcher_scripts_exist(rel):
    assert (ROOT / rel).is_file(), rel


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell parser")
@pytest.mark.parametrize("rel", SCRIPTS)
def test_the_launcher_scripts_parse(rel):
    probe = ("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
             f"'{(ROOT / rel).as_posix()}', [ref]$null, [ref]$e); $e.Count")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", probe],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "0", out.stdout + out.stderr


def test_the_launcher_is_idempotent_and_leaves_a_trail():
    start = (ROOT / "scripts/start-jarvis.ps1").read_text(encoding="utf-8")
    assert "8340" in start and "5173" in start
    assert "Get-NetTCPConnection" in start, "a held port means already running, not a second copy"
    assert "data" in start and "logs" in start, "stdout/stderr land under data/logs"


def test_stop_finds_the_port_owner_when_the_recorded_pid_is_gone():
    """The recorded PID is a launcher (the venv python.exe stub, npm.cmd)
    and can exit while its child keeps the port. Measured live: both PID
    files were stale and both servers were still up, and `stop` said "not
    running". So it also asks who owns 8340 and 5173 — and stops that only
    when its command line is ours."""
    stop = (ROOT / "scripts/stop-jarvis.ps1").read_text(encoding="utf-8")
    assert "OwningProcess" in stop
    assert "8340" in stop and "5173" in stop
    assert "server.py" in stop and "vite" in stop, "the port owner must be recognised as JARVIS's"
    assert "CommandLine" in stop, "never kill an unrelated program that sits on the port"


def test_the_launcher_is_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/start-jarvis.ps1" in readme
    assert "scripts/stop-jarvis.ps1" in readme
