""""Sent, sir" was not true, and JARVIS never mentioned why.

Live: he staged a steer, said "Sent, sir", and confirmed it again when asked.
The message was in fact sitting in the other window waiting to be approved,
because `crossSessionInbound` is absent from the user's settings.json.

    "are you sure cuz I'm looking at it and it actually asked for my approval"

`post_to_session` returns SENT after a successful `sendall`. That proves the
bytes left this process — nothing more. No acknowledgement is ever read back,
so delivery is not observable from here and is therefore no longer asserted:
the wording is "passed to", and when the setting says the message WILL need
approving, that is said in the same breath.

Turning the setting on is offered and never done silently — and the write
preserves the rest of a file holding the user's hooks, plugins, marketplaces
and status line.
"""

import importlib
import json

import pytest

import preflight


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """A settings.json of our own. Never the developer's real one."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path / "claude" / "settings.json"


@pytest.fixture
def wired(monkeypatch, tmp_path, settings):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module, settings


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# --- reading it -----------------------------------------------------------

def test_no_settings_file_means_it_will_need_approving(settings):
    assert preflight.cross_session_inbound_accepted() is False


def test_the_setting_missing_means_it_will_need_approving(settings):
    _write(settings, {"model": "sonnet"})
    assert preflight.cross_session_inbound_accepted() is False


def test_accept_means_it_lands_as_a_turn(settings):
    _write(settings, {"crossSessionInbound": "accept"})
    assert preflight.cross_session_inbound_accepted() is True


def test_unreadable_json_means_it_will_need_approving(settings):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ not json", encoding="utf-8")
    assert preflight.cross_session_inbound_accepted() is False


# --- writing it -----------------------------------------------------------

def test_everything_else_in_the_file_survives(settings):
    original = {
        "hooks": {"SessionStart": [{"matcher": "*", "hooks": [{"type": "command",
                                                               "command": "x"}]}]},
        "enabledPlugins": {"superpowers@obra": True},
        "extraKnownMarketplaces": {"obra": {"source": {"source": "github"}}},
        "statusLine": {"type": "command", "command": "statusline.sh"},
        "model": "sonnet",
    }
    _write(settings, original)

    ok, _detail = preflight.enable_cross_session_inbound()

    assert ok
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["crossSessionInbound"] == "accept"
    for key, value in original.items():
        assert after[key] == value, f"{key} was disturbed"


def test_a_file_that_will_not_parse_is_never_clobbered(settings):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ hooks: this is broken JSON but it is theirs", encoding="utf-8")

    ok, detail = preflight.enable_cross_session_inbound()

    assert ok is False
    assert "readable JSON" in detail
    assert settings.read_text(encoding="utf-8").startswith("{ hooks:"), "left exactly as it was"


def test_a_missing_file_is_created_with_just_the_one_setting(settings):
    ok, _detail = preflight.enable_cross_session_inbound()
    assert ok
    assert json.loads(settings.read_text(encoding="utf-8")) == {"crossSessionInbound": "accept"}


def test_it_is_idempotent(settings):
    _write(settings, {"crossSessionInbound": "accept", "model": "sonnet"})
    ok, detail = preflight.enable_cross_session_inbound()
    assert ok and detail == "already set"
    assert json.loads(settings.read_text(encoding="utf-8"))["model"] == "sonnet"


# --- what JARVIS says -----------------------------------------------------

def test_the_caveat_is_added_when_approval_will_be_needed(wired):
    server, _settings = wired
    caveat = server._inbound_caveat()
    assert "approve" in caveat
    assert "say the word" in caveat.lower()


def test_there_is_no_caveat_when_messages_land_straight_in(wired):
    server, settings = wired
    _write(settings, {"crossSessionInbound": "accept"})
    assert server._inbound_caveat() == ""


def test_the_brain_is_told_before_it_says_sent(wired, monkeypatch):
    """The staged result is what the brain speaks from. It must carry the
    caveat, or JARVIS says "sent, sir" and is wrong again."""
    server, _settings = wired

    class _Session:
        session_id = "s1"
        voice_name = "chitauri"
        project = "chitauri"
        needs_a_human_hand = False
        steerable = True
        socket_path = "/tmp/x.sock"
        thread_part = None          # named by its folder, as SessionState says

    class _Speech:
        async def say(self, *a, **k):
            return None

    monkeypatch.setattr(server, "speech", _Speech())
    monkeypatch.setattr(server, "_resolve_or_explain",
                        lambda n: (_Session(), None, None))

    import asyncio
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        server.tool_steer_session({"name": "chitauri", "prompt": "use Postgres"}))

    assert "staged" in out
    assert "approve" in out
    assert "enable_session_inbox" in out


class _RecordingSpeech:
    def __init__(self):
        self.said = []

    async def say(self, text, *a, **k):
        self.said.append(text)

        class _Utt:
            was_cancelled = False
        return _Utt()

    async def wait_for(self, utt, timeout=60.0):
        return True

    async def open_cancel_window(self, *a, **k):
        return False


async def _perform_one(server, monkeypatch):
    import session_steer
    speech = _RecordingSpeech()
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(session_steer, "post_to_session",
                        lambda path, prompt: session_steer.SENT)
    await server._perform_steer(server._StagedSteer(
        session_id="s1", voice_name="chitauri", project="chitauri",
        prompt="use Postgres", socket_path="/tmp/x.sock"))
    return speech.said[-1]


@pytest.mark.asyncio
async def test_delivery_is_never_asserted(wired, monkeypatch):
    """Bytes leaving is not a message landing, so he no longer says it did."""
    server, _settings = wired
    said = await _perform_one(server, monkeypatch)
    assert said.startswith("Passed to chitauri, sir.")
    assert "Sent" not in said


@pytest.mark.asyncio
async def test_the_spoken_outcome_warns_that_it_needs_approving(wired,
                                                                monkeypatch):
    server, _settings = wired
    said = await _perform_one(server, monkeypatch)
    assert "approve" in said


@pytest.mark.asyncio
async def test_with_the_setting_on_there_is_no_warning(wired, monkeypatch):
    server, settings = wired
    _write(settings, {"crossSessionInbound": "accept"})
    said = await _perform_one(server, monkeypatch)
    assert said == "Passed to chitauri, sir."


# --- the tool -------------------------------------------------------------

def test_it_is_an_acting_tool(wired):
    import brain
    import jarvis_mcp
    server, _settings = wired
    assert "enable_session_inbox" in server.TOOL_HANDLERS
    assert "enable_session_inbox" in server.ACTING_TOOLS
    assert "mcp__jarvis__enable_session_inbox" in brain.ALLOWED_TOOLS
    assert "enable_session_inbox" in {t["name"] for t in jarvis_mcp.TOOL_SPECS}


@pytest.mark.asyncio
async def test_the_tool_writes_it_and_says_so(wired):
    server, settings = wired
    out = await server.tool_enable_session_inbox({})
    assert json.loads(settings.read_text(encoding="utf-8"))["crossSessionInbound"] == "accept"
    assert "Done, sir" in out


@pytest.mark.asyncio
async def test_the_tool_refuses_rather_than_wrecking_a_broken_file(wired):
    server, settings = wired
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{ broken", encoding="utf-8")

    out = await server.tool_enable_session_inbox({})

    assert "couldn't" in out.lower()
    assert settings.read_text(encoding="utf-8") == "{ broken"


@pytest.mark.asyncio
async def test_the_tool_is_honest_when_it_was_already_on(wired):
    server, settings = wired
    _write(settings, {"crossSessionInbound": "accept"})
    out = await server.tool_enable_session_inbox({})
    assert "already" in out.lower()
