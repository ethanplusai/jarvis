"""The held call WAITS for you, and goes out with the bytes you approved.

The obvious way to resume an approved call is to start a fresh turn and ask
the brain to do it again. That is a lottery against a one-shot gate:
approval is a sha256 over the tool name and the exact arguments, so a single
re-worded sentence misses, stages a SECOND pending card, and tells the brain
again that nothing was sent. The user would be looking at two approval cards
for one post, believing both were queued.

So the call does not restart — it waits. Verified against CLI 2.1.270: a
PreToolUse hook given `timeout` in `--settings` blocks the call for as long
as it takes, and a hook that slept 25s and then allowed was honoured, with
the tool call proceeding afterwards. The bytes that go out are the bytes
that were approved, because they never left the CLI's hands.

The budget is layered so the innermost thing gives up first:

  server waits for you   GATE_APPROVAL_WAIT_SEC   120s
  hook's HTTP call       pretool_hook TIMEOUT_SEC 150s
  CLI's hook timeout     settings `timeout`       180s
  the whole turn         turn_ceiling             300s

`tools_outstanding` holds the silence watchdog while this runs, so waiting
is not mistaken for a stuck brain.
"""

import asyncio
import importlib

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
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    business_store.init_db()
    return server_module


class _Mouth:
    """`speech` is built by `lifespan`, so it does not exist until the client
    starts. Swapped in once the server is up, which is also the only moment
    the real one exists."""

    def __init__(self, boom=False):
        self.lines, self.boom = [], boom

    async def say(self, text, priority=None, immediate=None):
        if self.boom:
            raise RuntimeError("no voice client")
        self.lines.append(text)

    async def stop(self):
        """`lifespan` shuts the real one down on the way out."""


def _ask(server, tool="mcp__linkedin__create_post", payload=None, mouth=None, brain=None):
    from fastapi.testclient import TestClient
    import data_paths
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        if mouth is not None:
            server.speech = mouth
        if brain is not None:
            server.brain_instance = brain
        return client.post("/internal/pretool",
                           headers={"Authorization": f"Bearer {token}"},
                           json={"tool_name": tool,
                                 "tool_input": payload or {"text": "hello"},
                                 "tool_use_id": "t"}).json()


def _decision(body):
    return body["hookSpecificOutput"]["permissionDecision"]


def test_the_budgets_nest_so_the_innermost_gives_up_first(server):
    import brain
    import pretool_hook
    assert server.GATE_APPROVAL_WAIT_SEC < pretool_hook.TIMEOUT_SEC, \
        "the hook would hang up while JARVIS was still waiting for the user"
    hook = brain.Brain(brain.BrainConfig(home=server.data_paths.brain_home())) \
        .settings()["hooks"]["PreToolUse"][0]["hooks"][0]
    assert pretool_hook.TIMEOUT_SEC < hook["timeout"], \
        "the CLI would abandon the hook while it was still asking"
    assert hook["timeout"] < brain.BrainConfig(home=server.data_paths.brain_home()).turn_ceiling, \
        "the turn would be killed while the user was still deciding"


def test_it_waits_rather_than_refusing_immediately(server, monkeypatch):
    """The whole point. A gate that says no and hangs up makes the user go
    and ask again, which is where a duplicate comes from."""
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 1.5)
    import time
    started = time.monotonic()
    body = _ask(server, mouth=_Mouth())
    waited = time.monotonic() - started
    assert _decision(body) == "deny"
    assert waited > 1.0, f"gave up in {waited:.2f}s without waiting for anyone"


def test_approving_while_it_waits_lets_the_original_call_through(server, monkeypatch):
    """No second turn, no re-derived payload: these are the approved bytes."""
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 8.0)
    import business_store
    import threading

    def approve_soon():
        import time
        for _ in range(60):
            rows = [a for a in business_store.list_actions()
                    if a["state"] == "pending"
                    and a["operation"] == "mcp__linkedin__create_post"]
            if rows:
                business_store.transition(rows[0]["id"], rows[0]["digest"],
                                          "pending", "approved")
                return
            time.sleep(0.05)
    threading.Thread(target=approve_soon, daemon=True).start()

    body = _ask(server, mouth=_Mouth())
    assert _decision(body) == "allow", body
    assert "approved" in body["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_rejecting_while_it_waits_says_so_and_does_not_linger(server, monkeypatch):
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 8.0)
    import business_store
    import threading, time

    def reject_soon():
        for _ in range(60):
            rows = [a for a in business_store.list_actions()
                    if a["state"] == "pending"
                    and a["operation"] == "mcp__linkedin__create_post"]
            if rows:
                business_store.transition(rows[0]["id"], rows[0]["digest"],
                                          "pending", "rejected")
                return
            time.sleep(0.05)
    threading.Thread(target=reject_soon, daemon=True).start()

    started = time.monotonic()
    body = _ask(server, mouth=_Mouth())
    assert _decision(body) == "deny"
    assert "declined" in body["hookSpecificOutput"]["permissionDecisionReason"].lower()
    assert time.monotonic() - started < 7.0, "it waited out the clock after a no"


def test_he_says_out_loud_that_it_is_waiting(server, monkeypatch):
    """The brain is blocked mid-call and cannot narrate this itself, so the
    server says it. Silence for two minutes is indistinguishable from a
    hang, which is the failure this whole day was about."""
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 1.0)
    mouth = _Mouth()
    _ask(server, mouth=mouth)
    assert mouth.lines, "nothing was said while the user was being waited for"
    spoken = " ".join(mouth.lines).lower()
    assert "approv" in spoken, mouth.lines


def test_a_read_never_waits_and_never_speaks(server, monkeypatch):
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 30.0)

    class Heard:
        """The brain heard from the CLI's `mcp_status` what `get_feed` is
        called by its own server; without that, its `_` is held."""
        current_origin = "user"

        def own_tool_names(self, cli_name):
            return frozenset({"get_feed"}) if cli_name == "mcp__linkedin__get_feed" else None

        async def stop(self):
            pass
    mouth = _Mouth()
    import time
    started = time.monotonic()
    body = _ask(server, tool="mcp__linkedin__get_feed", payload={}, mouth=mouth, brain=Heard())
    assert _decision(body) == "allow"
    assert time.monotonic() - started < 2.0
    assert not mouth.lines


def test_a_mouth_that_will_not_work_does_not_break_the_gate(server, monkeypatch):
    """No tab connected is the COMMON case: the user approves from the
    Business page. A failed announcement must not turn into an allow."""
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 1.0)
    assert _decision(_ask(server, mouth=_Mouth(boom=True))) == "deny"
