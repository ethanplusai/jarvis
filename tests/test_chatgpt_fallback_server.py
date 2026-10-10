"""The ChatGPT fallback, as the server wires it: what the user hears, what
the phone reply says, "start fresh", and the approval gate's handling of the
fallback's gateway.

No brain or Codex runs here: `brain_instance` and `speech` are stand-ins,
as in test_auth_failure_voice.py. The brain's own half is in
test_chatgpt_fallback.py; the gateway's in test_guarded_mcp.py.
"""
import importlib
import json
import time

import pytest


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import business_store
    importlib.reload(business_store)
    import tool_log
    importlib.reload(tool_log)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    business_store.init_db()
    tool_log.init_db()
    monkeypatch.setattr(server_module, "GATE_APPROVAL_WAIT_SEC", 0.05)
    return server_module


class Speech:
    """Enough of a SpeechScheduler for a turn to run through the server."""

    def __init__(self):
        self.said = []
        self.fed = []

    async def say(self, text, priority=None, **kw):
        self.said.append(text)

    def begin_turn(self):
        from types import SimpleNamespace
        return SimpleNamespace(first_cut_at=None, first_ready_at=None, first_sent_at=None)

    def feed(self, utt, text):
        self.fed.append(text)

    async def end_turn(self, utt):
        pass

    async def wait_for(self, utt, timeout=None):
        pass


def _result(stop_reason="result", text="", **kw):
    import brain
    return brain.TurnResult(origin="user", text=text, stop_reason=stop_reason, **kw)


class Brain:
    """A brain whose next turn is `result`, standing in for the real one."""

    def __init__(self, result=None, *, ready=True, fallback_active=False, fallback_on=True,
                 nonce=None, provider="claude", claude_limited=False, resets_at=None,
                 own_names=None):
        import brain
        self.result = result or _result(text="Hello, sir.")
        # What the CLI's `mcp_status` said each connector calls its tools,
        # by the CLI's spelling of each.
        self.own_names = {k: frozenset(v) for k, v in (own_names or {}).items()}
        self.ready = ready
        self.failed = False
        self.failure_reason = None
        self.fallback_active = fallback_active
        self.config = brain.BrainConfig(home=None, chatgpt_fallback=fallback_on)
        self.current_origin = "user"
        self.provider = provider
        self.claude_limited = claude_limited
        self.rate_limit = {"resetsAt": resets_at} if resets_at else None
        self.nonce = nonce
        self.marked = []
        self.calls = []
        self.rotation_pending = False
        self.rotation_overdue = False
        self.rotated = 0

    async def turn(self, text, origin="user", on_delta=None, on_tool=None, untrusted=None,
                   on_switch=None):
        self.calls.append(("turn", text))
        self.on_switch = on_switch
        if on_delta and self.result.text:
            on_delta(self.result.text)
        return self.result

    def fallback_nonce_is(self, nonce):
        return self.nonce is not None and nonce == self.nonce

    def own_tool_names(self, cli_name):
        return self.own_names.get(cli_name)

    def mark_gateway_call(self, nonce, tool):
        if not self.fallback_nonce_is(nonce):
            return False
        self.marked.append(tool)
        return True

    async def start_fresh_on_fallback(self):
        self.calls.append(("fresh_on_fallback",))

    async def forget_fallback_conversation(self):
        self.calls.append(("forget_fallback",))

    async def rotate(self, handover=None, *, fresh=False, **conditions):
        self.calls.append(("rotate",))
        self.rotated_fresh = fresh
        self.rotated_conditions = conditions
        self.rotated += 1
        return True

    async def stop(self):
        pass


# --- what is said ------------------------------------------------------------------

def test_claudes_limit_alone_is_the_line_it_always_was(server):
    resets = time.time() + 3600
    line = server._limit_reply(_result("rate_limited", rate_limit={"resetsAt": resets}))
    assert line == f"I've hit the usage limit until {server._fmt_reset(resets)}, sir."


def test_when_chatgpt_cannot_stand_in_the_user_hears_why(server):
    line = server._limit_reply(_result("rate_limited", rate_limit={"resetsAt": time.time() + 60},
                                       fallback_unavailable="Codex isn't signed in for me yet"))
    assert line.endswith("ChatGPT can't stand in: Codex isn't signed in for me yet.")


def test_a_limited_turn_that_had_acted_says_what_it_did(server):
    line = server._limit_reply(_result("rate_limited", rate_limit={"resetsAt": time.time() + 60},
                                       tools=["mcp__jarvis__read_file"]))
    assert "reading" in line.lower(), line


def test_both_limits_at_once_name_both_times(server):
    claude, chatgpt = time.time() + 3600, time.time() + 7200
    line = server._limit_reply(_result("chatgpt_limited", rate_limit={"resetsAt": claude},
                                       retry_at=chatgpt, provider="chatgpt"))
    assert "both" in line
    assert server._fmt_reset(claude) in line and server._fmt_reset(chatgpt) in line


def test_a_turn_no_limit_ended_has_no_limit_line(server):
    assert server._limit_reply(_result()) is None
    assert server._limit_reply(_result("timeout")) is None


def test_an_error_says_which_brain_returned_it(server):
    assert server._error_line(_result("error")).startswith("My language systems")
    assert server._error_line(_result("error", provider="chatgpt")).startswith("ChatGPT")


@pytest.mark.asyncio
async def test_the_limit_event_is_never_said_the_turn_says_it(server, monkeypatch):
    """With the fallback on or off: the turn the limit ends says it, with
    what the turn had done. Said by the event too, it was said twice."""
    speech = Speech()
    monkeypatch.setattr(server, "speech", speech)
    for on in (True, False):
        monkeypatch.setattr(server, "brain_instance", Brain(fallback_on=on))
        await server._on_brain_state("rate_limited", {"resets_at": time.time() + 60})
    assert speech.said == []


@pytest.mark.asyncio
async def test_with_the_fallback_off_a_mid_turn_limit_is_said_once(server, monkeypatch):
    speech = Speech()
    resets = time.time() + 3600
    b = Brain(_result("error", rate_limit={"resetsAt": resets}), fallback_on=False)
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._on_brain_state("rate_limited", {"resets_at": resets})
    await server._handle_utterance("post it")
    limit_lines = [s for s in speech.said if s.startswith("I've hit the usage limit")]
    assert len(limit_lines) == 1, speech.said


@pytest.mark.asyncio
async def test_the_switch_is_announced_each_way(server, monkeypatch):
    speech = Speech()
    monkeypatch.setattr(server, "speech", speech)
    resets = time.time() + 3600
    await server._on_brain_state("fallback_started", {"resets_at": resets})
    await server._on_brain_state("fallback_ended", {})
    assert speech.said == [
        f"Claude's limit is reached until {server._fmt_reset(resets)}, sir. ChatGPT is standing in.",
        "Back on Claude, sir."]
    speech.said.clear()
    await server._on_brain_state("fallback_started", {"resets_at": resets, "delivered": True})
    await server._on_brain_state("fallback_ended", {"delivered": True})
    assert speech.said == [], "said in the turn's own utterance instead"


@pytest.mark.asyncio
async def test_the_announcements_never_raise_with_no_speech(server, monkeypatch):
    monkeypatch.setattr(server, "speech", None)
    await server._on_brain_state("fallback_started", {"resets_at": None})
    await server._on_brain_state("fallback_ended", {})


# --- the voice turn ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_claude_process_that_is_down_while_limited_does_not_turn_the_user_away(
        server, monkeypatch):
    """Claude refuses the warm-up while its limit holds, so its process may
    be down; the fallback's way answers regardless."""
    speech = Speech()
    b = Brain(_result(text="ChatGPT here.", provider="chatgpt"), ready=False,
              fallback_active=True)
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._handle_utterance("are you there?")
    assert ("turn", "are you there?") in b.calls
    assert "still starting" not in " ".join(speech.said)


@pytest.mark.asyncio
async def test_the_voice_turn_speaks_the_limit_and_why(server, monkeypatch):
    speech = Speech()
    b = Brain(_result("rate_limited", rate_limit={"resetsAt": time.time() + 60},
                      fallback_unavailable="the Codex app isn't installed"))
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._handle_utterance("hello")
    assert any("ChatGPT can't stand in: the Codex app isn't installed." in s
               for s in speech.said), speech.said


async def _nothing():
    return None


# --- the phone line --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_phone_reply_carries_the_switch_each_way(server, monkeypatch):
    import conversation_store
    conversation_store.init_db()
    monkeypatch.setattr(server, "speech", None)
    resets = time.time() + 3600
    b = Brain(_result(text="ChatGPT here.", provider="chatgpt"))
    monkeypatch.setattr(server, "brain_instance", b)
    await server._on_brain_state("fallback_started", {"resets_at": resets})
    reply = await server._phone_chat("hi", "tg:1:1", line="telegram")
    assert reply.startswith(f"Claude's limit is reached until {server._fmt_reset(resets)}, sir.")
    assert reply.rstrip().endswith("ChatGPT here.")
    again = await server._phone_chat("more", "tg:1:2", line="telegram")
    assert again == "ChatGPT here.", "said once"
    await server._on_brain_state("fallback_ended", {})
    b.result = _result(text="Claude here.")
    reply = await server._phone_chat("again", "tg:1:3", line="telegram")
    assert reply.startswith("Back on Claude, sir.") and "Claude here." in reply


@pytest.mark.asyncio
async def test_a_switch_made_by_voice_still_reaches_each_phone_line(server, monkeypatch):
    """The news goes to every line on its own next reply, whichever line the
    switching turn came from — and "back on Claude" only where "standing
    in" was heard."""
    import conversation_store
    conversation_store.init_db()
    monkeypatch.setattr(server, "speech", None)
    b = Brain(_result(text="Answer.", provider="chatgpt"))
    monkeypatch.setattr(server, "brain_instance", b)
    await server._on_brain_state("fallback_started", {"resets_at": time.time() + 60})
    assert (await server._phone_chat("a", "wa:1", line="whatsapp")).startswith("Claude's limit")
    assert (await server._phone_chat("b", "tg:2:1", line="telegram")).startswith("Claude's limit")
    await server._on_brain_state("fallback_ended", {})
    b.result = _result(text="Answer.")
    assert (await server._phone_chat("c", "wa:2", line="whatsapp")).startswith("Back on Claude")
    await server._on_brain_state("fallback_started", {"resets_at": time.time() + 60})
    await server._on_brain_state("fallback_ended", {})
    # Telegram's own last news was "standing in", and nothing has told it
    # otherwise — whatever came and went while it was silent.
    assert (await server._phone_chat("d", "tg:2:2", line="telegram")).startswith(
        "Back on Claude")
    assert await server._phone_chat("e", "wa:3", line="whatsapp") == "Answer.", \
        "a line already told the way back is not told again"


@pytest.mark.asyncio
async def test_the_phone_reply_says_both_limits(server, monkeypatch):
    import conversation_store
    conversation_store.init_db()
    b = Brain(_result("chatgpt_limited", rate_limit={"resetsAt": time.time() + 60},
                      retry_at=time.time() + 120, provider="chatgpt"))
    monkeypatch.setattr(server, "brain_instance", b)
    reply = await server._phone_chat("hi", "tg:1:3", line="telegram")
    assert reply.startswith("Claude and ChatGPT have both hit their limits")


@pytest.mark.asyncio
async def test_the_phone_line_is_not_turned_away_while_the_fallback_answers(server, monkeypatch):
    import conversation_store
    conversation_store.init_db()
    b = Brain(_result(text="ChatGPT here.", provider="chatgpt"), ready=False,
              fallback_active=True)
    monkeypatch.setattr(server, "brain_instance", b)
    reply = await server._phone_chat("hi", "tg:1:4", line="telegram")
    assert reply == "ChatGPT here."


# --- start fresh -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_fresh_while_chatgpt_stands_in_clears_it_and_owes_claude(server, monkeypatch):
    speech = Speech()
    b = Brain(fallback_active=True)
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    said = await server._start_fresh()
    assert ("fresh_on_fallback",) in b.calls
    assert b.rotated == 0, "Claude's limit refuses the warm-up a rotation needs"
    assert speech.said == [server.FRESH_START_LINE_ON_FALLBACK] == [said]
    assert "before Claude next answers" in said, "it does not claim Claude's side is gone"


@pytest.mark.asyncio
async def test_start_fresh_on_claude_also_drops_what_chatgpt_said(server, monkeypatch):
    speech = Speech()
    b = Brain(fallback_active=False)
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    await server._start_fresh()
    assert b.rotated == 1 and b.rotated_fresh, "no note carried, not even from disk"
    assert b.calls.index(("forget_fallback",)) < b.calls.index(("rotate",)), \
        "dropped before the rotation, so no waiting turn carries it across"
    assert speech.said == [server.FRESH_START_LINE]


# --- the gate and the gateway ------------------------------------------------------------

def _ask(server, b, tool, tool_input=None, nonce=None):
    from fastapi.testclient import TestClient
    import data_paths
    body = {"tool_name": tool, "tool_input": tool_input or {}, "tool_use_id": "abc"}
    if nonce is not None:
        body["fallback_nonce"] = nonce
    with TestClient(server.app) as client:
        server.brain_instance = b
        r = client.post("/internal/pretool", json=body,
                        headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    assert r.status_code == 200, r.text
    out = r.json()["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


POST = {"text": "hello world", "confirm_post": True}


def test_a_gateway_call_from_a_turn_that_is_over_is_refused_and_stages_nothing(server):
    import business_store
    b = Brain(nonce="current")
    decision, reason = _ask(server, b, "mcp__linkedin__create_post", POST, nonce="stale")
    assert decision == "deny" and reason == server._GATEWAY_TURN_GONE
    assert not [a for a in business_store.list_actions()
                if a["operation"] == "mcp__linkedin__create_post"]
    decision, _ = _ask(server, b, "mcp__linkedin__get_feed", {}, nonce="stale")
    assert decision == "deny", "a read from a finished turn is refused too"
    assert b.marked == []


def test_an_allowed_gateway_call_taints_the_turn_before_it_goes(server):
    b = Brain(nonce="current")
    decision, _ = _ask(server, b, "mcp__linkedin__get_feed", {"count": 3}, nonce="current")
    assert decision == "allow"
    assert b.marked == ["mcp__linkedin__get_feed"]


def test_the_claude_paths_hook_is_untouched(server):
    """No nonce: the Claude path. It is never refused as a finished turn,
    and never marked here — its own tool_use taints its turn."""
    b = Brain(nonce="current", own_names={"mcp__linkedin__get_feed": {"get_feed"}})
    decision, _ = _ask(server, b, "mcp__linkedin__get_feed", {})
    assert decision == "allow"
    assert b.marked == []


def test_an_approval_is_not_spent_on_a_turn_that_is_over(server):
    """A yes spent on a call nobody will make is a yes the user no longer
    has. The card stays approved, to be sent when he asks again."""
    import business_store
    b = Brain(nonce="current")
    _ask(server, b, "mcp__linkedin__create_post", POST, nonce="current")
    [card] = [a for a in business_store.list_actions()
              if a["operation"] == "mcp__linkedin__create_post"]
    business_store.transition(card["id"], card["digest"], "pending", "approved")
    b.nonce = "the-next-turn"
    decision, reason = _ask(server, b, "mcp__linkedin__create_post", POST, nonce="current")
    assert decision == "deny" and reason == server._GATEWAY_TURN_GONE
    assert business_store.get_action(card["id"])["state"] == "approved"
    decision, _ = _ask(server, b, "mcp__linkedin__create_post", POST, nonce="the-next-turn")
    assert decision == "allow"
    assert business_store.get_action(card["id"])["state"] == "submitted"
    assert b.marked == ["mcp__linkedin__create_post"]


def test_an_approval_the_user_gives_while_the_turn_ends_stays_unspent(server, monkeypatch):
    import business_store
    b = Brain(nonce="current")

    async def approved_but_the_turn_ended(action_id, digest):
        business_store.transition(action_id, digest, "pending", "approved")
        b.nonce = None
        return "approved"

    monkeypatch.setattr(server, "_wait_for_the_user", approved_but_the_turn_ended)
    decision, reason = _ask(server, b, "mcp__linkedin__create_post", POST, nonce="current")
    assert decision == "deny" and reason == server._GATEWAY_TURN_GONE
    [card] = [a for a in business_store.list_actions()
              if a["operation"] == "mcp__linkedin__create_post"]
    assert card["state"] == "approved"


# --- after the review ------------------------------------------------------------------

def test_a_limit_after_a_tool_says_the_limit_and_what_went_out(server):
    """The shape the brain really returns when the limit lands on a later
    call: an error, with the tools and the limit."""
    import tool_log
    tool_log.record(tool="mcp__linkedin__create_post", server="linkedin",
                    decision="allow", reason="You approved this exact call.")
    resets = time.time() + 3600
    line = server._limit_reply(_result("error", tools=["mcp__linkedin__create_post"],
                                       rate_limit={"resetsAt": resets}, duration_sec=60.0))
    assert line.startswith(f"I've hit the usage limit until {server._fmt_reset(resets)}, sir.")
    assert "create_post" in line and "already" in line.lower()


def test_an_error_with_no_limit_is_not_a_limit(server):
    assert server._limit_reply(_result("error", rate_limit=None)) is None
    assert server._limit_reply(_result("error", rate_limit={"resetsAt": time.time() - 5})) is None


def test_every_line_that_ends_a_turn_says_what_it_had_done(server):
    import tool_log
    tool_log.record(tool="mcp__linkedin__create_post", server="linkedin",
                    decision="allow", reason="You approved this exact call.")
    tools = ["mcp__linkedin__create_post"]
    both = server._limit_reply(_result("chatgpt_limited", tools=tools, provider="chatgpt",
                                       rate_limit={"resetsAt": time.time() + 60},
                                       retry_at=time.time() + 120, duration_sec=60.0))
    assert "create_post" in both
    failed = server._error_line(_result("error", tools=tools, provider="chatgpt",
                                        duration_sec=60.0))
    assert failed.startswith("ChatGPT returned an error") and "create_post" in failed
    assert server._error_line(_result("error")) == \
        "My language systems returned an error, sir. Check the server log."


@pytest.mark.asyncio
async def test_a_brain_waiting_out_the_limit_says_so_not_still_starting(server, monkeypatch):
    """With the fallback off, the process is down for the whole limit."""
    speech = Speech()
    resets = time.time() + 7200
    b = Brain(ready=False, fallback_on=False, claude_limited=True, resets_at=resets)
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._handle_utterance("hello?")
    assert speech.said == [f"I've hit the usage limit until {server._fmt_reset(resets)}, sir."]
    import conversation_store
    conversation_store.init_db()
    assert (await server._phone_chat("hi", "tg:9:1", line="telegram")).startswith(
        "I've hit the usage limit")


@pytest.mark.asyncio
async def test_no_rotation_is_tried_while_claudes_limit_holds(server, monkeypatch):
    b = Brain(claude_limited=True)
    b.rotation_pending = True
    b.current_origin = None
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._maybe_rotate()
    assert b.rotated == 0 and b.calls == []


@pytest.mark.asyncio
async def test_a_notice_is_said_and_sent(server, monkeypatch):
    import brain
    import conversation_store
    conversation_store.init_db()
    speech = Speech()
    b = Brain(_result(text="Answer.", notice=brain.FRESH_NOT_CLEARED))
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._handle_utterance("hi")
    assert brain.FRESH_NOT_CLEARED in speech.said
    reply = await server._phone_chat("hi", "tg:3:1", line="telegram")
    assert reply.startswith(brain.FRESH_NOT_CLEARED) and reply.endswith("Answer.")


def test_during_a_chatgpt_turn_only_its_nonce_is_the_owners(server):
    """The idle Claude process beside a ChatGPT turn cannot borrow it."""
    server.brain_instance = Brain(provider="chatgpt", nonce="the-turn")
    assert server._caller_origin(None) is None
    assert server._caller_origin("another") is None
    assert server._caller_origin("the-turn") == "user"
    server.brain_instance = Brain(provider="claude")
    assert server._caller_origin(None) == "user"


def _tool(server, b, tool, nonce=None):
    from fastapi.testclient import TestClient
    import data_paths
    body = {"tool": tool, "arguments": {"title": "x", "body": "y"}}
    if nonce is not None:
        body["fallback_nonce"] = nonce
    with TestClient(server.app) as client:
        server.brain_instance = b
        return client.post("/internal/tool", json=body,
                           headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"}).json()


def test_an_acting_tool_without_the_turns_nonce_is_refused_during_a_chatgpt_turn(server):
    b = Brain(provider="chatgpt", nonce="the-turn")
    refused = _tool(server, b, "remember")
    assert refused["ok"] is False and "not_allowed_from_event" in refused["text"]


def test_a_connector_call_without_the_nonce_cannot_spend_an_approval_during_a_chatgpt_turn(server):
    import business_store
    b = Brain(nonce="the-turn")
    _ask(server, b, "mcp__linkedin__create_post", POST, nonce="the-turn")
    [card] = [a for a in business_store.list_actions()
              if a["operation"] == "mcp__linkedin__create_post"]
    business_store.transition(card["id"], card["digest"], "pending", "approved")
    b.provider = "chatgpt"
    decision, _ = _ask(server, b, "mcp__linkedin__create_post", POST)     # no nonce
    assert decision == "deny"
    assert business_store.get_action(card["id"])["state"] == "approved", "not spent"


def test_the_jarvis_server_sends_the_turns_nonce_when_it_has_one(monkeypatch):
    import jarvis_mcp
    sent = []

    # The transport jarvis_mcp sends a call carrying the token through
    # (`loopback_http`).
    def post_json(url, body, token, *, timeout, context=None):
        sent.append(json.loads(body))
        return b'{"ok": true, "text": "fine"}'

    monkeypatch.setattr(jarvis_mcp.loopback_http, "post_json", post_json)
    monkeypatch.delenv("JARVIS_TOOL_NONCE", raising=False)
    jarvis_mcp._forward("recall", {})
    monkeypatch.setenv("JARVIS_TOOL_NONCE", "the-turn")
    jarvis_mcp._forward("recall", {})
    assert "fallback_nonce" not in sent[0]
    assert sent[1]["fallback_nonce"] == "the-turn"


# --- names every gate parses the same way ----------------------------------------------------

@pytest.mark.parametrize("name", ["jarvis_", "jarvis.", "a..b", "linkedin_"])
def test_a_server_name_that_would_split_somewhere_else_is_refused(server, monkeypatch, tmp_path,
                                                                    name):
    """`mcp__jarvis___post` reads as JARVIS's own tool `_post`, which no gate
    holds — on the Claude path as well as the fallback's."""
    import data_paths
    path = data_paths.connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {name: {"command": "x"}, "linkedin.work": {
        "command": "y"}}}), encoding="utf-8")
    report = server.declared_connections()
    assert name not in report.servers
    assert "linkedin.work" in report.servers
    import guarded_mcp
    assert not guarded_mcp._usable_server_name(name)


def test_an_ambiguous_tool_name_is_held_by_every_gate(server):
    import re
    import brain
    import claude_env
    import pretool_gate
    assert pretool_gate.classify("mcp__jarvis___send_message") == "outward"
    assert pretool_gate.classify("mcp__jarvis__read_file") == "read"
    assert brain.untrusted_tool_source("mcp__jarvis___send_message") == "jarvis"
    assert brain.untrusted_tool_source("mcp__jarvis__read_page") is None
    matcher = claude_env.pretool_hook_settings("http://x")["PreToolUse"][0]["matcher"]
    assert re.match(matcher, "mcp__jarvis___send_message")
    assert not re.match(matcher, "mcp__jarvis__remember")


def test_the_gateway_asks_the_gate_with_the_name_the_claude_cli_would_use(server):
    import guarded_mcp
    import pretool_gate
    name = guarded_mcp.gated_tool_name("linkedin.personal", "search_and.reply")
    assert name == "mcp__linkedin_personal__search_and_reply"
    assert pretool_gate.classify(name) == "outward", "a dot no longer hides the second verb"


# --- preflight -----------------------------------------------------------------------------

FAKE_CODEX = [__import__("sys").executable,
              str(__import__("pathlib").Path(__file__).parent / "fixtures" / "fake_codex.py")]


@pytest.fixture
def fake_codex(monkeypatch):
    import chatgpt_fallback
    monkeypatch.setenv("JARVIS_CHATGPT_FALLBACK", "1")
    real = chatgpt_fallback.check_readiness
    monkeypatch.setattr(chatgpt_fallback, "check_readiness",
                        lambda command=None: real(command=FAKE_CODEX))


@pytest.mark.asyncio
async def test_preflight_says_the_fallback_is_off_when_it_is(monkeypatch):
    import preflight
    check = await preflight._check_chatgpt_fallback(timeout=5)
    assert check.status == preflight.STATUS_OK and "off" in check.message


@pytest.mark.asyncio
async def test_preflight_says_it_is_ready_with_the_version_and_model(fake_codex):
    import preflight
    check = await preflight._check_chatgpt_fallback(timeout=20)
    assert check.status == preflight.STATUS_OK
    assert "fake" in check.message and "gpt-5.5" in check.message


@pytest.mark.asyncio
async def test_preflight_says_why_not_and_what_to_do(fake_codex, monkeypatch):
    import preflight
    monkeypatch.setenv("FAKECODEX_LOGIN", "none")
    check = await preflight._check_chatgpt_fallback(timeout=20)
    assert check.status == preflight.STATUS_WARN
    assert "isn't signed in" in check.message
    assert check.remedy.startswith("Run `python scripts/chatgpt_setup.py login`")
    assert preflight._phrase_for(check) == \
        "ChatGPT can't stand in for me when Claude's limit is reached"


@pytest.mark.asyncio
async def test_preflight_does_not_lose_a_slow_check(monkeypatch):
    import threading
    import chatgpt_fallback
    import preflight
    monkeypatch.setenv("JARVIS_CHATGPT_FALLBACK", "1")
    release = threading.Event()

    def slow(refresh=False):
        release.wait(5)
        return chatgpt_fallback.Readiness(True, version="v")
    monkeypatch.setattr(chatgpt_fallback, "readiness", slow)
    check = await preflight._check_chatgpt_fallback(timeout=0.2)
    release.set()
    assert check.name == "chatgpt_fallback" and check.status == preflight.STATUS_WARN
    assert "did not finish" in check.message
    assert preflight._phrase_for(check) == "the ChatGPT fallback couldn't be checked in time"


@pytest.mark.asyncio
async def test_preflight_warns_when_connections_would_be_left_out(fake_codex, monkeypatch):
    import data_paths
    import preflight
    path = data_paths.connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"notion": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_BIND_HOST", "192.168.1.20")
    check = await preflight._check_chatgpt_fallback(timeout=20)
    assert check.status == preflight.STATUS_WARN and "left out" in check.message



# --- round two -------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_phone_start_fresh_replies_with_what_actually_happened(server, monkeypatch):
    import conversation_store
    conversation_store.init_db()
    speech = Speech()
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", Brain(fallback_active=True))
    reply = await server._phone_chat("start fresh", "tg:5:1", line="telegram")
    assert reply == server.FRESH_START_LINE_ON_FALLBACK
    assert speech.said == [], "the room is not told what the phone asked"

    class Refuses(Brain):
        async def rotate(self, handover=None, *, fresh=False, **_):
            return False
    monkeypatch.setattr(server, "brain_instance", Refuses())
    reply = await server._phone_chat("start fresh", "tg:5:2", line="telegram")
    assert reply == server.FRESH_START_FAILED_LINE


@pytest.mark.asyncio
async def test_the_phone_is_not_told_standing_in_on_a_reply_saying_it_cant(server, monkeypatch):
    import conversation_store
    conversation_store.init_db()
    monkeypatch.setattr(server, "speech", None)
    b = Brain(_result("rate_limited", rate_limit={"resetsAt": time.time() + 60},
                      fallback_unavailable="Codex isn't signed in for me yet"))
    monkeypatch.setattr(server, "brain_instance", b)
    await server._on_brain_state("fallback_started", {"resets_at": time.time() + 60})
    reply = await server._phone_chat("hi", "tg:6:1", line="telegram")
    assert "standing in" not in reply and reply.startswith("I've hit the usage limit")


def test_chatgpts_first_failed_turn_says_claudes_limit(server):
    resets = time.time() + 3600
    line = server._limit_reply(_result("error", provider="chatgpt", limit_unannounced=True,
                                       rate_limit={"resetsAt": resets}))
    assert line.startswith(f"I've hit the usage limit until {server._fmt_reset(resets)}, sir")
    assert "ChatGPT couldn't answer" in line
    assert server._limit_reply(_result("error", provider="chatgpt",
                                       rate_limit={"resetsAt": resets})) is None


@pytest.mark.asyncio
async def test_a_breach_is_said_in_its_own_words(server, monkeypatch):
    import brain
    speech = Speech()
    b = Brain(_result("error", provider="chatgpt", notice=brain.FALLBACK_BREACH_LINE,
                      breach="command_execution"))
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._handle_utterance("hi")
    assert speech.said == [brain.FALLBACK_BREACH_LINE]


@pytest.mark.asyncio
async def test_a_voice_turn_hears_the_switch_inside_its_own_utterance(server, monkeypatch):
    speech = Speech()
    b = Brain(_result(text="ChatGPT here.", provider="chatgpt"))
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())

    async def turn(text, origin="user", on_delta=None, on_tool=None, untrusted=None,
                   on_switch=None):
        on_switch("to_chatgpt", time.time() + 3600)
        on_delta("ChatGPT here.")
        return b.result
    b.turn = turn
    await server._handle_utterance("hi")
    assert speech.fed and speech.fed[0].startswith("Claude's limit is reached")


def test_a_stale_nonce_is_nobodys_even_during_a_claude_turn(server):
    server.brain_instance = Brain(provider="claude", nonce=None)
    assert server._caller_origin("a-dead-turns-nonce") is None
    assert server._caller_origin(None) == "user"


def test_two_verbs_joined_by_a_symbol_are_both_verbs_at_the_gate(server):
    """`get&delete` is written `get_delete` by the CLI; the gateway sends the
    raw name too, and the gate reads both verbs."""
    import business_store
    from fastapi.testclient import TestClient
    import data_paths
    b = Brain(nonce="n")
    with TestClient(server.app) as client:
        server.brain_instance = b
        r = client.post("/internal/pretool", json={
            "tool_name": "mcp__files__get_delete", "tool_input": {"id": 1},
            "tool_use_id": "t", "fallback_nonce": "n", "raw_tool_name": "get&delete"},
            headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    assert r.json()["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [a for a in business_store.list_actions()
            if a["operation"] == "mcp__files__get_delete"], "held for the user"


def _staged(tool):
    import business_store
    return [a for a in business_store.list_actions() if a["operation"] == tool]


def test_two_verbs_joined_by_a_symbol_are_both_verbs_on_the_claude_path_too(server):
    """The Claude CLI's hook sends only its own spelling, `get_delete`, and
    no nonce. The brain heard from the CLI's `mcp_status` that the connector
    calls it `get&delete`, and the gate reads that as the gateway's is read."""
    b = Brain(own_names={"mcp__files__get_delete": {"get&delete"}})
    decision, _ = _ask(server, b, "mcp__files__get_delete", {"id": 1})
    assert decision == "deny"
    assert _staged("mcp__files__get_delete"), "held for the user"


def test_a_dot_is_still_one_verb_on_the_claude_path(server):
    b = Brain(own_names={"mcp__files__list_issues": {"list.issues"}})
    assert _ask(server, b, "mcp__files__list_issues", {}) == ("allow", "Read-only.")
    assert not _staged("mcp__files__list_issues")


def test_a_name_whose_own_spelling_was_never_reported_is_held(server, caplog):
    """Any `_` in the CLI's spelling may have been a `&` it replaced, and
    nothing says which: held, as the gate holds every name it cannot read.
    Said in the log once, and only where that is why it was held."""
    b = Brain()
    decision, _ = _ask(server, b, "mcp__files__list_issues", {})
    assert decision == "deny"
    assert _staged("mcp__files__list_issues")
    _ask(server, b, "mcp__files__list_issues", {})
    _ask(server, b, "mcp__linkedin__create_post", POST)
    said = [r.getMessage() for r in caplog.records if "never reported" in r.getMessage()]
    assert len(said) == 1 and "mcp__files__list_issues" in said[0]


def test_a_name_the_cli_wrote_unchanged_needs_no_own_spelling(server):
    """No `_` in the tool's part, so no symbol was replaced in it: the CLI's
    spelling is the connector's own. JARVIS's own tools are not this gate's."""
    b = Brain()
    assert _ask(server, b, "mcp__paperclip__paperclipListIssues", {}) == ("allow", "Read-only.")
    assert _ask(server, b, "mcp__jarvis__read_file", {})[0] == "allow"


def test_one_own_name_that_acts_holds_every_tool_the_cli_writes_the_same(server):
    """`get_delete` and `get&delete` are both `mcp__files__get_delete` to the
    CLI; the hook cannot say which was called, so the one that acts decides."""
    b = Brain(own_names={"mcp__files__get_delete": {"get_delete", "get&delete"}})
    assert _ask(server, b, "mcp__files__get_delete", {"id": 2})[0] == "deny"


def test_the_gateway_reads_its_own_raw_name_not_the_brains(server):
    """The fallback's gateway knows exactly which tool it is relaying."""
    b = Brain(nonce="n", own_names={"mcp__files__list_issues": {"list&issues"}})
    from fastapi.testclient import TestClient
    import data_paths
    with TestClient(server.app) as client:
        server.brain_instance = b
        r = client.post("/internal/pretool", json={
            "tool_name": "mcp__files__list_issues", "tool_input": {},
            "tool_use_id": "t", "fallback_nonce": "n", "raw_tool_name": "list.issues"},
            headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    assert r.json()["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_two_connections_the_cli_writes_the_same_are_refused(server):
    import data_paths
    path = data_paths.connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"acme.slack": {"command": "x"},
                                               "acme_slack": {"command": "y"}}}),
                    encoding="utf-8")
    report = server.declared_connections()
    assert list(report.servers) == ["acme.slack"]
    assert any("acme_slack" in p for p in report.problems)


def test_the_connections_roster_uses_the_clis_spelling(server):
    server.LAST_CONNECTIONS = server.ConnectionsReport(servers={"my.notion": {"command": "x"}})

    class Roster(Brain):
        connected_servers = ["my.notion"]
        failed_servers = []
        live_tools = ["mcp__my_notion__search"]

        def tools_from(self, name):
            import claude_env
            prefix = f"mcp__{claude_env.mcp_name_part(name)}__"
            return [t[len(prefix):] for t in self.live_tools if t.startswith(prefix)]
    server.brain_instance = Roster()
    said = server.tool_connections({})
    assert "NOT permitted" not in said


@pytest.mark.asyncio
async def test_a_rotation_waits_for_no_generation_that_is_gone(server, monkeypatch):
    """A fresh start replaced the generation while its journal was asked:
    the successor is not rotated too."""
    b = Brain()
    b.rotation_pending = True
    b.current_origin = None
    b.generation = 1

    async def journal(text, origin="user", **kw):
        b.generation = 2                    # a fresh start happened meanwhile
        return _result(text="note")
    b.turn = journal
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    monkeypatch.setattr(server, "speech", None)
    await server._maybe_rotate()
    assert b.rotated == 0


@pytest.mark.asyncio
async def test_preflight_says_the_bind_even_when_codex_is_slow(monkeypatch):
    import threading
    import chatgpt_fallback
    import data_paths
    import preflight
    monkeypatch.setenv("JARVIS_CHATGPT_FALLBACK", "1")
    monkeypatch.setenv("JARVIS_BIND_HOST", "192.168.1.20")
    path = data_paths.connections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"notion": {"command": "x"}}}), encoding="utf-8")
    release = threading.Event()
    monkeypatch.setattr(chatgpt_fallback, "readiness",
                        lambda refresh=False: release.wait(5) or chatgpt_fallback.Readiness(True))
    check = await preflight._check_chatgpt_fallback(timeout=0.2)
    release.set()
    assert "left out" in check.message


# --- round three: what is said ----------------------------------------------------------------

def test_a_breach_after_the_turn_acted_says_what_it_had_done(server):
    """"So I stopped it" is not the whole story when the turn had already
    sent something: what it had done is said beside the breach."""
    import brain
    acted = _result("error", provider="chatgpt", breach="command_execution",
                    notice=brain.FALLBACK_BREACH_LINE,
                    tools=["mcp__jarvis__spawn_run", "mcp__notion__create_page"])
    line = server._error_line(acted)
    assert "create_page on notion" in line and "spawn_run" in line
    assert "returned an error" not in line, "the notice says that part"
    read = _result("error", provider="chatgpt", breach="command_execution",
                   notice=brain.FALLBACK_BREACH_LINE, tools=["mcp__jarvis__recall"])
    assert "nothing was changed" not in (server._error_line(read) or ""), \
        "nobody knows what Codex's own tool changed"


def test_a_notice_that_is_not_a_breach_is_said_beside_the_error(server):
    import brain
    failed = _result("error", provider="chatgpt", notice=brain.FRESH_NOT_CLEARED,
                     tools=["mcp__notion__create_page"])
    line = server._error_line(failed)
    assert line.startswith("ChatGPT returned an error") and "create_page on notion" in line


@pytest.mark.asyncio
async def test_a_breach_is_said_first_then_what_the_turn_had_done(server, monkeypatch):
    import brain
    speech = Speech()
    b = Brain(_result("error", provider="chatgpt", notice=brain.FALLBACK_BREACH_LINE,
                      breach="command_execution", tools=["mcp__notion__create_page"]))
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._handle_utterance("hi")
    assert speech.said[0] == brain.FALLBACK_BREACH_LINE
    assert "create_page on notion" in speech.said[1]


def test_an_acting_tool_the_gate_named_by_its_own_name_is_not_called_reading(server):
    """`get&delete` is written `get_delete`: the gate held it for approval,
    and the turn's account of itself must not call it a read."""
    r = _result("timeout", provider="chatgpt", tools=["mcp__files__get_delete"],
                acting_tools=["mcp__files__get_delete"], duration_sec=5)
    did = server._what_the_turn_had_done(r)
    assert "get_delete on files" in did and "only been reading" not in did


@pytest.mark.parametrize("own_names", [
    {"mcp__files__get_delete": {"get&delete"}, "mcp__files__list_issues": {"list.issues"}},
    {},        # never reported: held for want of its own name
])
def test_on_claude_an_approved_acting_call_is_not_called_reading_once_its_process_is_gone(
        server, own_names):
    """The account of a killed turn is asked after a timeout, and a timeout
    kills the process — whose own names go with it. So the account asks what
    the gate DID, which outlives the process: `get&delete`, held, approved
    and sent, is reported sent, and a call let through as a read is a read."""
    import business_store
    b = Brain(own_names=own_names)
    _ask(server, b, "mcp__files__get_delete", {"id": 1})
    [card] = _staged("mcp__files__get_delete")
    business_store.transition(card["id"], card["digest"], "pending", "approved")
    assert _ask(server, b, "mcp__files__get_delete", {"id": 1})[0] == "allow"
    if own_names:
        assert _ask(server, b, "mcp__files__list_issues", {})[0] == "allow"
    server.brain_instance = Brain()           # the timed-out process, and its names, gone

    r = _result("timeout", tools=["mcp__files__get_delete"], duration_sec=60)
    did = server._what_the_turn_had_done(r)
    assert "I had already sent get_delete on files" in did, did
    if own_names:
        r = _result("timeout", tools=["mcp__files__list_issues"], duration_sec=60)
        assert "only been reading" in server._what_the_turn_had_done(r)


@pytest.mark.asyncio
async def test_back_on_claude_is_heard_after_claudes_held_answer(server, monkeypatch):
    """A tool turn's answer is held to the turn's end; the way back is told
    inside the turn. Fed straight away, it came first."""
    speech = Speech()
    b = Brain(_result(text="Your calendar is clear, sir."))
    monkeypatch.setattr(server, "speech", speech)
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())

    async def turn(text, origin="user", on_delta=None, on_tool=None, untrusted=None,
                   on_switch=None):
        on_tool()
        on_delta("Your calendar is clear, sir.")
        on_switch("to_claude", None)
        return b.result
    b.turn = turn
    await server._handle_utterance("what's on?")
    assert speech.fed == ["Your calendar is clear, sir.", " Back on Claude, sir. "]


def test_back_on_claude_is_not_stacked_on_a_reply_saying_claude_is_limited_again(server):
    server._fallback_news.update(episode=7, kind="ended", resets_at=None)
    server._fallback_news_told["telegram"] = (7, "started")
    limited = _result("rate_limited", rate_limit={"resetsAt": time.time() + 60})
    assert server._switch_note("telegram", limited) == ""
    assert server._switch_note("telegram", _result(text="Hi.")) == \
        server.FALLBACK_ENDED_LINE + "\n\n"


def test_a_line_whose_chatgpt_turn_fails_is_told_claudes_limit(server):
    """The room, or another line, heard the switch; this line did not, and
    its first ChatGPT turn failed."""
    resets = time.time() + 3600
    server._fallback_news.update(episode=3, kind="started", resets_at=resets)
    failed = _result("error", provider="chatgpt")
    note = server._switch_note("whatsapp", failed)
    assert note.startswith(f"Claude's limit is reached until {server._fmt_reset(resets)}")
    assert "standing in" not in note
    assert server._switch_note("whatsapp", failed) == "", "said once"
    assert "standing in" in server._switch_note("whatsapp", _result(text="Hi.",
                                                                     provider="chatgpt"))
    server._fallback_news["kind"] = "ended"
    assert server._switch_note("whatsapp", _result(text="Hi.")) == \
        server.FALLBACK_ENDED_LINE + "\n\n"


def test_a_failed_turn_already_saying_the_limit_gets_no_second_limit_line(server):
    server._fallback_news.update(episode=4, kind="started", resets_at=time.time() + 60)
    first = _result("error", provider="chatgpt", limit_unannounced=True)
    assert server._switch_note("telegram", first) == ""


# --- round three: rotation at the pause ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rotation_asks_again_under_the_brains_locks(server, monkeypatch):
    b = Brain()
    b.rotation_pending, b.current_origin, b.generation = True, None, 4
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    monkeypatch.setattr(server, "speech", None)
    monkeypatch.setattr(server, "_write_journal", lambda *a, **k: True)
    await server._maybe_rotate()
    assert b.rotated_conditions == {"expected_generation": 4, "only_if_pending": True}


@pytest.mark.asyncio
async def test_no_rotation_while_the_brain_is_not_serving(server, monkeypatch):
    """Another rotation, an owed fresh start or a restart is under way: its
    journal would be refused, and a rotation queued behind it would replace
    the generation it produces."""
    b = Brain(ready=False)
    b.rotation_pending, b.current_origin = True, None
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    await server._maybe_rotate()
    assert b.rotated == 0 and b.calls == []


@pytest.mark.asyncio
async def test_a_journal_turn_that_meets_the_limit_latches_nothing(server, monkeypatch):
    b = Brain()
    b.rotation_pending, b.current_origin, b.generation = True, None, 2

    async def journal(text, origin="user", **kw):
        b.claude_limited = True                # this turn met the limit
        return _result("rate_limited")
    b.turn = journal
    written = []
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", lambda msg: _nothing())
    monkeypatch.setattr(server, "speech", None)
    monkeypatch.setattr(server, "_write_journal", lambda *a, **k: written.append(a) or True)
    await server._maybe_rotate()
    assert b.rotated == 0 and written == []
    assert not server._handover_collected, "asked again after the reset"


@pytest.mark.asyncio
async def test_the_banner_comes_back_however_the_rotation_ends(server, monkeypatch):
    emitted = []

    async def emit(msg):
        emitted.append(msg)
    b = Brain()
    b.rotation_pending, b.current_origin, b.generation = True, None, 1

    async def journal(text, origin="user", **kw):
        b.generation = 2                       # replaced meanwhile: an early return
        return _result(text="note")
    b.turn = journal
    monkeypatch.setattr(server, "brain_instance", b)
    monkeypatch.setattr(server, "_voice_emit", emit)
    monkeypatch.setattr(server, "speech", None)
    await server._maybe_rotate()
    assert b.rotated == 0
    assert emitted[-2:] == [{"type": "notice", "text": ""}, {"type": "status", "state": "idle"}]


# --- round three: whose a read is, at the route ----------------------------------------------------

def _real_brain(tmp_path):
    import brain
    import chatgpt_fallback
    b = brain.Brain(brain.BrainConfig(home=tmp_path / "home"))
    b._codex = chatgpt_fallback.CodexSession(tmp_path / "codex-home")
    b._proc = object()          # the Claude process an idle call comes from
    return b


def _chatgpt_turn(b, nonce="the-turn"):
    import brain
    t = brain._Turn("user", None)
    t.provider, t.codex_epoch = "chatgpt", b._codex.epoch
    b._inflight, b._fallback_nonce = t, nonce
    return t


def _call(server, b, tool, arguments, nonce=None):
    from fastapi.testclient import TestClient
    import data_paths
    body = {"tool": tool, "arguments": arguments}
    if nonce is not None:
        body["fallback_nonce"] = nonce
    with TestClient(server.app) as client:
        server.brain_instance = b
        return client.post("/internal/tool", json=body, headers={
            "Authorization": f"Bearer {data_paths.ensure_tool_token()}"}).json()


def test_the_idle_claude_beside_a_chatgpt_turn_taints_claude_not_the_turn(server, tmp_path):
    b = _real_brain(tmp_path)
    t = _chatgpt_turn(b)
    _call(server, b, "business_status", {})               # no nonce: the idle CLI
    assert b._generation_untrusted == "business records"
    assert t.untrusted_label is None and b._codex.untrusted is None


def test_the_chatgpt_turns_own_read_taints_the_turn_and_its_thread(server, tmp_path):
    b = _real_brain(tmp_path)
    t = _chatgpt_turn(b)
    _call(server, b, "business_status", {}, nonce="the-turn")
    assert t.untrusted_label == "business records"
    assert b._codex.untrusted == "business records"
    assert b._generation_untrusted is None


def test_a_stopped_codexs_read_taints_nobody(server, tmp_path):
    b = _real_brain(tmp_path)
    t = _chatgpt_turn(b)
    _call(server, b, "business_status", {}, nonce="a-dead-turns")
    assert t.untrusted_label is None and b._codex.untrusted is None
    assert b._generation_untrusted is None


def test_the_idle_claude_with_no_turn_in_flight_taints_its_generation(server, tmp_path):
    """Woken by another session between turns: nothing in flight to mark,
    and the generation that read it was left clean."""
    b = _real_brain(tmp_path)
    _call(server, b, "business_status", {})
    assert b.generation_untrusted_source == "business records"


def test_a_read_that_lands_after_its_turn_marks_that_turns_thread(server, tmp_path, monkeypatch):
    """Not whichever turn is in flight when the handler returns."""
    import brain
    b = _real_brain(tmp_path)
    _chatgpt_turn(b)
    claude = brain._Turn("user", None)

    async def scanned():
        b._inflight = claude                  # the ChatGPT turn ended; another began
    monkeypatch.setattr(server, "_ensure_projects_scanned", scanned)
    _call(server, b, "business_status", {}, nonce="the-turn")
    assert b._codex.untrusted == "business records"
    assert claude.untrusted_label is None and b._generation_untrusted is None


def test_an_acting_call_whose_turn_ended_during_the_wait_is_refused(server, tmp_path,
                                                                    monkeypatch):
    """Its taint went with it: the gate after the wait would read another
    turn's, or none."""
    b = _real_brain(tmp_path)
    _chatgpt_turn(b)
    b._codex.untrusted = "a web page"

    async def scanned():
        b._inflight = None                    # the turn ended: a ceiling, a breach
    monkeypatch.setattr(server, "_ensure_projects_scanned", scanned)
    refused = _call(server, b, "remember", {"text": "x"}, nonce="the-turn")
    assert refused["ok"] is False and "not_allowed_from_event" in refused["text"]


def test_the_idle_claudes_connector_read_through_the_hook_taints_its_generation(server,
                                                                               tmp_path):
    from fastapi.testclient import TestClient
    import data_paths
    b = _real_brain(tmp_path)
    _chatgpt_turn(b)
    with TestClient(server.app) as client:
        server.brain_instance = b
        r = client.post("/internal/pretool", json={
            "tool_name": "mcp__notion__search", "tool_input": {"q": "x"}, "tool_use_id": "u"},
            headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"}).json()
    assert r["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert b._generation_untrusted == "notion"
    assert b._codex.untrusted is None


# --- round three: names ---------------------------------------------------------------------------

def test_only_a_joining_symbol_is_read_as_and(server):
    """`get&delete` is two verbs; `list.issues` and `issues/list` are one,
    as the CLI's own spelling reads them — one policy for both brains."""
    import pretool_gate
    assert pretool_gate.classify_call("mcp__files__get_delete", "files", "get&delete") == "outward"
    assert pretool_gate.classify_call("mcp__files__search_reply", "files",
                                      "search + reply") == "outward"
    assert pretool_gate.classify_call("mcp__gh__list_issues", "gh", "list.issues") == "read"
    assert pretool_gate.classify_call("mcp__gh__list_issues", "gh", "list/issues") == "read"
    assert pretool_gate.classify_call("mcp__gh__list_issues", "gh", None) == "read"


def test_a_dotted_read_tool_is_not_held_on_chatgpt(server):
    from fastapi.testclient import TestClient
    import data_paths
    b = Brain(nonce="n")
    with TestClient(server.app) as client:
        server.brain_instance = b
        r = client.post("/internal/pretool", json={
            "tool_name": "mcp__gh__list_issues", "tool_input": {}, "tool_use_id": "t",
            "fallback_nonce": "n", "raw_tool_name": "list.issues"},
            headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    assert r.json()["hookSpecificOutput"]["permissionDecision"] == "allow"


# --- round four -------------------------------------------------------------------------------

def test_a_line_told_before_a_renewal_hears_the_way_back(server):
    """A second start inside one episode, with no end between: the way back
    is owed to a line told of either."""
    server._note_fallback_news("fallback_started", {"resets_at": time.time() + 60})
    server._fallback_news_told["telegram"] = (server._fallback_news["episode"], "started")
    server._note_fallback_news("fallback_started", {"resets_at": time.time() + 3600})
    server._note_fallback_news("fallback_ended", {})
    assert server._switch_note("telegram", _result(text="Hi.")) == \
        server.FALLBACK_ENDED_LINE + "\n\n"


def test_an_idle_read_that_lands_in_a_claude_turn_closes_its_acting_gate(server, tmp_path,
                                                                         monkeypatch):
    """The idle CLI read; the user spoke while it ran; the CLI goes on with
    the text in its one conversation — so the turn now in flight has read
    it too."""
    import brain
    b = _real_brain(tmp_path)
    proc = object()
    b._proc, b._ready = proc, True
    live = brain._Turn("user", None, proc)

    async def scanned():
        b._inflight = live                  # the user spoke while the read ran
    monkeypatch.setattr(server, "_ensure_projects_scanned", scanned)
    _call(server, b, "business_status", {})
    assert live.untrusted_label == "business records"
    server.brain_instance = b
    assert server._writer_untrusted_source("run_command") == "business records"


def test_a_claude_turn_that_ended_during_its_read_marks_its_generation(server, tmp_path,
                                                                      monkeypatch):
    import brain
    b = _real_brain(tmp_path)
    proc = object()
    b._proc = proc
    turn = brain._Turn("user", None, proc)
    turn.echoed = True                      # answering it: the call is the turn's
    b._inflight = turn

    async def ended():
        b._inflight = None
    monkeypatch.setattr(server, "_ensure_projects_scanned", ended)
    _call(server, b, "business_status", {})
    assert b._generation_untrusted == "business records"


def test_a_claude_turn_whose_process_was_replaced_marks_nothing(server, tmp_path, monkeypatch):
    import brain
    b = _real_brain(tmp_path)
    b._proc = object()
    b._inflight = brain._Turn("user", None, b._proc)
    b._inflight.echoed = True               # answering it: the call is the turn's

    async def replaced():
        b._inflight, b._proc = None, object()
    monkeypatch.setattr(server, "_ensure_projects_scanned", replaced)
    _call(server, b, "business_status", {})
    assert b._generation_untrusted is None


def test_a_connector_read_the_hook_lets_through_marks_the_claude_turn_in_flight(server,
                                                                               tmp_path):
    """Even one reached for before that turn began."""
    from fastapi.testclient import TestClient
    import brain
    import data_paths
    b = _real_brain(tmp_path)
    proc = object()
    b._proc = proc
    live = brain._Turn("user", None, proc)
    b._inflight = live
    with TestClient(server.app) as client:
        server.brain_instance = b
        client.post("/internal/pretool", json={
            "tool_name": "mcp__notion__search", "tool_input": {"q": "x"}, "tool_use_id": "u"},
            headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    assert live.untrusted_label == "notion"


@pytest.mark.parametrize("reason", ["fresh start", "fresh-start", "rotation-silent",
                                    "Shutdown Silent"])
def test_the_brain_cannot_write_a_wall_or_a_tombstone(server, reason):
    import jarvis_memory
    server.tool_write_journal({"text": "a real note", "reason": reason})
    [entry] = jarvis_memory.journal_entries()
    assert entry[1] == "manual"
    assert "a real note" in jarvis_memory.latest_journal()


@pytest.mark.parametrize("own,kind", [("get|delete", "outward"), ("read,delete", "outward"),
                                      ("list;remove", "outward"), ("list.issues", "read"),
                                      ("list/issues", "read")])
def test_every_joining_symbol_joins_and_no_other_does(server, own, kind):
    import claude_env
    import pretool_gate
    cli = claude_env.mcp_tool_name("files", own)
    assert pretool_gate.classify_call(cli, "files", own) == kind


# --- round five -------------------------------------------------------------------------------

def test_a_line_told_in_an_earlier_episode_hears_the_way_back(server):
    """Whatever came and went while it was silent: its own last news was
    "standing in"."""
    server._note_fallback_news("fallback_started", {"resets_at": time.time() + 60})
    server._fallback_news_told["telegram"] = (server._fallback_news["episode"], "started")
    server._note_fallback_news("fallback_ended", {})
    server._note_fallback_news("fallback_started", {"resets_at": time.time() + 60})
    server._note_fallback_news("fallback_ended", {})
    assert server._switch_note("telegram", _result(text="Hi.")) == \
        server.FALLBACK_ENDED_LINE + "\n\n"
    assert server._switch_note("telegram", _result(text="Hi.")) == "", "once"


def test_a_long_reason_cannot_forge_a_wall(server):
    """Judged on the name the note is filed under, after it is cut to one
    line."""
    import jarvis_memory
    server.tool_write_journal({"text": "a real note",
                               "reason": "fresh start" + "!" * 230 + "abc"})
    [entry] = jarvis_memory.journal_entries()
    assert entry[1] == "manual"

