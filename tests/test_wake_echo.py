"""Telling a wake from a turn, exactly: each of JARVIS's messages carries a
tag, the CLI echoes it back (`--replay-user-messages`), and until that echo
whatever the process says is somebody else's.

The Claude process accepts messages from the user's other sessions
(`crossSessionInbound: "accept"`), and each starts a turn JARVIS never
asked for — a wake. Its stream and JARVIS's share one stdout. Attributing
by "the turn in flight" gave a wake still running when the user spoke to
the user: its words spoken, its `result` ending the turn, its tool calls
made with the user's origin, and the user's own message answered to
nobody. Inferring a wake's extent from silence was tried three times and
broken three ways — a wake silent before its first token, one queued
behind another, and the user's message queued behind a wake — which are
the cases driven here, through the real reader, against the fake CLI
(tests/fixtures/fake_brain.py), which queues, folds and echoes as
`claude` 2.1.270 does (docs/chatgpt-fallback.md, "Telling a wake from a
turn").
"""
import asyncio
import importlib
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

FAKE = Path(__file__).parent / "fixtures" / "fake_brain.py"


def _brain(tmp_path, **kw):
    import brain
    return brain.Brain(brain.BrainConfig(
        home=tmp_path / "jarvis", claude_path=f"{sys.executable} {FAKE}",
        turn_timeout=kw.pop("turn_timeout", 5.0), warmup_timeout=10.0, **kw))


async def _peer(b, text, *, sender="another-session"):
    """A message from another of the user's sessions, as the CLI queues
    one: a frame on the process's own input, ahead of whatever JARVIS
    writes after it."""
    frame = {"type": "user", "uuid": str(uuid.uuid4()), "message": {
        "role": "user",
        "content": f'<cross-session-message from="{sender}">{text}</cross-session-message>'}}
    b._proc.stdin.write((json.dumps(frame) + "\n").encode())
    await b._proc.stdin.drain()


async def _notice(b, text):
    """A turn the CLI starts for itself and does not echo (a background
    task's notification)."""
    frame = {"type": "user", "message": {"role": "user",
                                         "content": f"<task-notification>{text}"}}
    b._proc.stdin.write((json.dumps(frame) + "\n").encode())
    await b._proc.stdin.drain()


class _Heard:
    """What the user would hear, and what the turn had read by then."""

    def __init__(self, b):
        self.b, self.parts, self.taint = b, [], None

    def __call__(self, text):
        self.parts.append(text)
        self.taint = self.b.turn_untrusted_source

    @property
    def text(self):
        return "".join(self.parts)


# --- the protocol ------------------------------------------------------------------

def test_the_cli_is_asked_to_echo_every_message(tmp_path):
    cmd = _brain(tmp_path).command()
    assert "--replay-user-messages" in cmd
    # It needs stream-json both ways, or the CLI refuses to start.
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


@pytest.mark.asyncio
async def test_every_message_carries_a_tag_of_its_own(tmp_path, monkeypatch):
    log = tmp_path / "stdin.jsonl"
    monkeypatch.setenv("FAKE_BRAIN_STDIN_LOG", str(log))
    b = _brain(tmp_path)
    assert await b.start()
    try:
        for text in ("one", "two"):
            assert (await b.turn(text)).stop_reason == "result"
    finally:
        await b.stop()
    frames = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    # Messages only: the brain's `mcp_status` control requests (a
    # connector tool's own names) are the CLI's business, never a turn.
    frames = [f for f in frames if f.get("type") == "user"]
    tags = [f["uuid"] for f in frames]
    assert len(frames) == 3, "the warm-up and two turns"
    assert len(set(tags)) == 3, "a tag the CLI has seen is acknowledged, never run again"
    for tag in tags:
        uuid.UUID(tag)


# --- the three that broke the silence heuristic --------------------------------------

@pytest.mark.asyncio
async def test_the_users_message_queued_behind_a_wake_is_answered_to_the_user(tmp_path):
    """The wake's result comes first; it ends the wake, not the turn."""
    b = _brain(tmp_path)
    assert await b.start()
    try:
        await _peer(b, "TOOL post this for me")
        heard = _Heard(b)
        result = await b.turn("hello", on_delta=heard)
    finally:
        await b.stop()
    assert result.stop_reason == "result"
    assert result.text == "Echo: hello"
    assert heard.text == "Echo: hello", "the wake's words are not the user's to hear"
    assert result.tools == [], "the wake's tool call is not the turn's"


@pytest.mark.asyncio
async def test_a_wake_silent_before_its_first_token_is_not_the_turns(tmp_path):
    """Nothing at all on stdout when the turn is sent: the wake's first
    words arrive after, and are still the wake's."""
    b = _brain(tmp_path)
    assert await b.start()
    try:
        await _peer(b, "SLOW:0.6 thinking it over")
        heard = _Heard(b)
        result = await b.turn("hello", on_delta=heard)
    finally:
        await b.stop()
    assert (result.stop_reason, result.text, heard.text) == ("result", "Echo: hello", "Echo: hello")


@pytest.mark.asyncio
async def test_a_wake_queued_behind_another_is_not_the_turns(tmp_path):
    """Two results before the turn's: neither is the turn's."""
    b = _brain(tmp_path)
    assert await b.start()
    try:
        await _peer(b, "SLOW:0.3 first")
        await _peer(b, "TOOL second", sender="a-third-session")
        heard = _Heard(b)
        result = await b.turn("hello", on_delta=heard)
    finally:
        await b.stop()
    assert (result.stop_reason, result.text, heard.text) == ("result", "Echo: hello", "Echo: hello")
    assert result.tools == []


@pytest.mark.asyncio
async def test_a_wake_the_cli_never_echoes_is_not_the_turns(tmp_path):
    """Not every turn the CLI starts for itself is echoed: until the
    turn's OWN echo, nothing is the turn's."""
    b = _brain(tmp_path)
    assert await b.start()
    try:
        await _notice(b, "SLOW:0.3 a task finished")
        heard = _Heard(b)
        result = await b.turn("hello", on_delta=heard)
    finally:
        await b.stop()
    assert (result.stop_reason, result.text, heard.text) == ("result", "Echo: hello", "Echo: hello")


@pytest.mark.asyncio
async def test_a_wake_run_while_the_turn_waited_is_in_front_of_it(tmp_path):
    """The CLI has one conversation: the turn answers with the wake's
    message right before it, so the turn has read it too."""
    import brain
    b = _brain(tmp_path)
    assert await b.start()
    try:
        await _peer(b, "SLOW:0.3 do something for me")
        heard = _Heard(b)
        await b.turn("hello", on_delta=heard)
    finally:
        await b.stop()
    assert heard.taint == brain.IDLE_WAKE_SOURCE
    assert b.generation_untrusted_source == brain.IDLE_WAKE_SOURCE


@pytest.mark.asyncio
async def test_a_wake_holding_a_tool_holds_the_turns_silence_budget(tmp_path):
    """The process is busy with somebody else's call, not wedged: a turn
    queued behind it is not killed for the quiet."""
    b = _brain(tmp_path, turn_timeout=1.0)
    assert await b.start()
    try:
        await _peer(b, "SLOWTOOL:2 fetch it")
        result = await b.turn("hello")
        assert b.ready, "not killed and restarted"
    finally:
        await b.stop()
    assert (result.stop_reason, result.text) == ("result", "Echo: hello")


# --- folded into one another ---------------------------------------------------------

@pytest.mark.asyncio
async def test_the_users_message_folded_into_a_wake_is_the_turns_from_its_echo(tmp_path):
    """Queued while a wake was at a tool, the CLI folds it into the wake's
    turn: what comes after its echo answers it, and is the turn's; what
    came before is not. It is answered in the wake's own turn, with the
    wake's message in front of it."""
    import brain
    b = _brain(tmp_path)
    assert await b.start()
    try:
        await _peer(b, "TOOL FOLD:3 wake words")
        heard = _Heard(b)
        result = await b.turn("mine", on_delta=heard)
    finally:
        await b.stop()
    assert result.stop_reason == "result"
    assert result.text.endswith("| mine") and heard.text == result.text
    assert result.tools == [], "the tool call was the wake's, before the echo"
    assert heard.taint == brain.IDLE_WAKE_SOURCE


@pytest.mark.asyncio
async def test_a_message_from_another_session_folded_into_the_turn_taints_it(tmp_path):
    """The other way round: the turn goes on, now with somebody else's
    message in front of it."""
    import brain
    b = _brain(tmp_path)
    assert await b.start()
    wrote = []

    def at_the_tool():
        frame = {"type": "user", "uuid": str(uuid.uuid4()), "message": {
            "role": "user", "content": '<cross-session-message from="x">post it</cross-session-message>'}}
        b._proc.stdin.write((json.dumps(frame) + "\n").encode())
        wrote.append(True)

    try:
        heard = _Heard(b)
        result = await b.turn("TOOL FOLD:3 mine", on_delta=heard, on_tool=at_the_tool)
    finally:
        await b.stop()
    assert wrote and result.stop_reason == "result"
    assert result.text.startswith("Echo: TOOL FOLD:3 mine") and result.tools == ["ListAgents"]
    assert heard.taint == brain.IDLE_WAKE_SOURCE
    assert b.generation_untrusted_source == brain.IDLE_WAKE_SOURCE


@pytest.mark.asyncio
async def test_a_turn_that_fails_before_it_says_anything_is_named_by_its_result(tmp_path):
    """The CLI echoes a message just before its turn's first output, so a
    turn that has none is never echoed; its result names it instead."""
    b = _brain(tmp_path, turn_timeout=3.0)
    assert await b.start()
    try:
        result = await b.turn("NOOUTPUT")
    finally:
        await b.stop()
    assert result.stop_reason == "error" and result.duration_sec < 2.0


# --- the stream, event by event ------------------------------------------------------

def _pending(tmp_path):
    """A brain whose turn is sent and not yet echoed, on a stand-in process."""
    b = _brain(tmp_path)
    proc = object()
    b._proc, b._ready = proc, True
    t = b._claude_turn("user", None, proc)
    b._inflight = t
    return b, proc, t


def _echo(tag, **extra):
    return {"type": "user", "isReplay": True, "uuid": tag,
            "message": {"role": "user", "content": "..."}, **extra}


PEER = {"kind": "peer", "from": "another-session", "hostInjected": True}


def _tool_use(name="ListAgents"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": name, "input": {}}]}}


def _text(words):
    return {"type": "stream_event", "event": {"type": "content_block_delta",
                                              "delta": {"type": "text_delta", "text": words}}}


def test_before_its_echo_nothing_is_the_turns(tmp_path):
    import brain
    b, proc, t = _pending(tmp_path)
    for ev in (_echo("someone-elses", isSynthetic=True, origin=PEER), _text("wake words"),
               _tool_use(), {"type": "result", "subtype": "success", "result": "wake words",
                             "origin": PEER}):
        b._handle(ev, proc)
    assert t.parts == [] and t.tools == [] and not t.done.is_set()
    assert isinstance(b.call_owner(None), brain.IdleClaude)
    assert b.turn_answering is False
    b._handle(_echo(t.tag), proc)
    assert b.call_owner(None) is t and b.turn_answering is True
    b._handle(_text("mine"), proc)
    b._handle({"type": "result", "subtype": "success", "result": "mine"}, proc)
    assert t.parts == ["mine"] and t.done.is_set() and t.stop_reason == "result"


def test_after_its_echo_the_next_result_is_the_turns(tmp_path):
    """Whatever it names: a folded turn's result can name the wake's
    message first."""
    b, proc, t = _pending(tmp_path)
    b._handle(_echo(t.tag), proc)
    b._handle({"type": "result", "subtype": "success", "result": "", "origin": PEER}, proc)
    assert t.done.is_set()


def test_a_result_naming_the_turn_ends_it_without_an_echo(tmp_path):
    b, proc, t = _pending(tmp_path)
    b._handle({"type": "result", "subtype": "error_during_execution", "is_error": True,
               "errors": ["failed before it began"], "user_message_uuid": t.tag,
               "user_message_uuids": [t.tag]}, proc)
    assert t.done.is_set() and t.stop_reason == "error"


def test_a_result_naming_another_message_does_not_end_the_turn(tmp_path):
    b, proc, t = _pending(tmp_path)
    b._handle({"type": "result", "subtype": "success", "result": "",
               "user_message_uuid": "not-this-one", "user_message_uuids": ["not-this-one"]}, proc)
    assert not t.done.is_set()


def test_a_wakes_tool_call_holds_the_pending_turns_clock(tmp_path):
    b, proc, t = _pending(tmp_path)
    t.last_activity -= 1000                        # long quiet
    assert t.wait_slice(silence=90, ceiling=10_000) == 0.0
    b._handle(_tool_use(), proc)
    t.last_activity -= 1000
    assert t.wait_slice(silence=90, ceiling=10_000) > 0, "the wake's call owns the clock"
    b._handle({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}, proc)
    t.last_activity -= 1000
    assert t.wait_slice(silence=90, ceiling=10_000) == 0.0
    assert t.tools_outstanding == 0, "never the turn's own count"


def test_a_wake_that_ended_holds_nothing(tmp_path):
    b, proc, t = _pending(tmp_path)
    b._handle(_tool_use(), proc)
    b._handle({"type": "result", "subtype": "error_during_execution", "is_error": True}, proc)
    t.last_activity -= 1000
    assert t.wait_slice(silence=90, ceiling=10_000) == 0.0


def test_a_wake_that_began_before_the_turn_still_holds_its_clock(tmp_path):
    import brain
    b = _brain(tmp_path)
    proc = object()
    b._proc, b._ready = proc, True
    b._handle(_tool_use(), proc)                   # no turn in flight yet
    t = b._claude_turn("user", None, proc)
    b._inflight = t
    t.last_activity -= 1000
    assert t.wait_slice(silence=90, ceiling=10_000) > 0


def test_the_echo_ends_the_hold(tmp_path):
    """Folded at a tool boundary, every call of the wake's has come back;
    from the echo on, the turn's own count is the one that holds."""
    b, proc, t = _pending(tmp_path)
    b._handle(_tool_use(), proc)
    b._handle(_echo(t.tag), proc)
    t.last_activity -= 1000
    assert t.wait_slice(silence=90, ceiling=10_000) == 0.0


def test_echoed_into_a_wake_the_turn_has_read_the_wake(tmp_path):
    import brain
    b = _brain(tmp_path)
    proc = object()
    b._proc, b._ready = proc, True
    b._handle(_echo("someone-elses", isSynthetic=True, origin=PEER), proc)
    b._handle(_tool_use(), proc)
    b._handle({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}, proc)
    b._generation_untrusted = None                  # only the fold, below, may mark it
    t = b._claude_turn("user", None, proc)
    b._inflight = t
    b._handle(_echo(t.tag), proc)
    assert b.turn_untrusted_source == brain.IDLE_WAKE_SOURCE


def test_a_message_from_elsewhere_echoed_mid_turn_taints_the_turn(tmp_path):
    import brain
    b, proc, t = _pending(tmp_path)
    b._handle(_echo(t.tag), proc)
    assert b.turn_untrusted_source is None
    b._handle(_echo("someone-elses", isSynthetic=True, origin=PEER), proc)
    assert b.turn_untrusted_source == brain.IDLE_WAKE_SOURCE
    assert b.call_owner(None) is t, "still the turn's: the result will be"


def test_a_message_from_elsewhere_marks_the_idle_process_before_it_answers(tmp_path):
    """The echo is the first thing a wake says; a wake that fails before
    its model says a word has still put the message in the conversation."""
    import brain
    b = _brain(tmp_path)
    proc = object()
    b._proc, b._ready = proc, True
    b._handle(_echo("someone-elses", isSynthetic=True, origin=PEER), proc)
    assert b.generation_untrusted_source == brain.IDLE_WAKE_SOURCE


def test_the_process_is_still_heard_from_before_the_echo(tmp_path):
    """The turn's heartbeat: the process is alive while it answers
    somebody else."""
    b, proc, t = _pending(tmp_path)
    t.last_activity -= 1000
    b._handle(_text("wake words"), proc)
    assert t.wait_slice(silence=90, ceiling=10_000) > 80


def test_what_the_process_says_of_itself_is_read_before_the_echo(tmp_path):
    """Its init and its usage are the process's, not a turn's."""
    b, proc, t = _pending(tmp_path)
    b._handle({"type": "system", "subtype": "init", "session_id": "s-1", "model": "m-1",
               "mcp_servers": [{"name": "jarvis", "status": "connected"}], "tools": ["x"]}, proc)
    b._handle({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "utilization": 0.8, "rateLimitType": "seven_day"}}, proc)
    assert (b.session_id, b.model_in_use, b.live_tools) == ("s-1", "m-1", ["x"])
    assert b.usage["status"] == "allowed_warning"


def test_the_cli_bookkeeping_is_nobodys(tmp_path):
    b, proc, t = _pending(tmp_path)
    for ev in ({"type": "command_lifecycle", "command_uuid": t.tag, "state": "queued"},
               {"type": "command_lifecycle", "command_uuid": t.tag, "state": "started"}):
        b._handle(ev, proc)
    assert not t.echoed and b._generation_untrusted is None


def test_a_chatgpt_turn_is_never_echoed_into(tmp_path):
    """The idle Claude process beside a ChatGPT turn: its echoes and its
    result are a wake's, whatever tag they carry."""
    import brain
    b = _brain(tmp_path)
    proc = object()
    b._proc, b._ready = proc, True
    t = brain._Turn("user", None)
    t.provider = "chatgpt"
    b._inflight = t
    b._handle(_echo(t.tag), proc)
    b._handle({"type": "result", "subtype": "success", "user_message_uuids": [t.tag]}, proc)
    assert not t.echoed and not t.done.is_set()


# --- the gates, as the server applies them -------------------------------------------

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
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


def _tool(server, b, tool, arguments):
    from fastapi.testclient import TestClient
    import data_paths
    with TestClient(server.app) as client:
        server.brain_instance = b
        return client.post("/internal/tool", json={"tool": tool, "arguments": arguments},
                           headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"}
                           ).json()


def _ask(server, b, tool, tool_input):
    from fastapi.testclient import TestClient
    import data_paths
    with TestClient(server.app) as client:
        server.brain_instance = b
        r = client.post("/internal/pretool",
                        json={"tool_name": tool, "tool_input": tool_input, "tool_use_id": "a"},
                        headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
    out = r.json()["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def test_a_call_before_the_echo_has_no_origin(server, tmp_path):
    b, proc, t = _pending(tmp_path)
    server.brain_instance = b
    assert server._caller_origin(None) is None
    b._handle(_echo(t.tag), proc)
    assert server._caller_origin(None) == "user"


def test_an_acting_tool_called_before_the_echo_is_refused(server, tmp_path):
    b, proc, t = _pending(tmp_path)
    refused = _tool(server, b, "remember", {"text": "the other session says so"})
    assert refused["ok"] is False and "not_allowed_from_event" in refused["text"]
    b._handle(_echo(t.tag), proc)
    done = _tool(server, b, "remember", {"text": "the user says so"})
    assert "not_allowed_from_event" not in done["text"]


def test_a_connector_call_before_the_echo_cannot_spend_an_approval(server, tmp_path):
    import business_store
    b, proc, t = _pending(tmp_path)
    post = {"text": "hello world"}
    _ask(server, b, "mcp__linkedin__create_post", post)
    [card] = [a for a in business_store.list_actions()
              if a["operation"] == "mcp__linkedin__create_post"]
    business_store.transition(card["id"], card["digest"], "pending", "approved")
    decision, reason = _ask(server, b, "mcp__linkedin__create_post", post)
    assert decision == "deny" and "off my own back" in reason
    assert business_store.get_action(card["id"])["state"] == "approved", "not spent"
    b._handle(_echo(t.tag), proc)
    assert _ask(server, b, "mcp__linkedin__create_post", post)[0] == "allow"
