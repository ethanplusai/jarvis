"""Finding things on the web, and the gate a poisoned page must not walk through.

The user, to JARVIS: "can you search that open SEO GitHub and read it yourself
so you can see what the license says" — and JARVIS had no way to find a URL he
did not already have. `WebSearch` and `WebFetch` are the CLI's own tools and
work inside the brain's exact flag set, so the capability is one line of
allowlist.

The cost of that line is the whole of this file. Everything those two tools
return is written by whoever owns the page, and it lands in the brain's context
with NO `_wrap_untrusted` around it — the CLI puts it there, not JARVIS, so
the wrapper JARVIS uses for `read_page` cannot reach it. The brain holds tools
that spawn processes. So a turn that has read the open web may not also act
unsupervised in that same turn.

Nothing here touches the network: the brain is the stand-in in tests/fixtures,
and the endpoint is driven through TestClient.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

FAKE = Path(__file__).parent / "fixtures" / "fake_brain.py"


def _config(tmp_path, **kw):
    import brain
    return brain.BrainConfig(home=tmp_path / "jarvis",
                             claude_path=f"{sys.executable} {FAKE}",
                             turn_timeout=kw.pop("turn_timeout", 5.0),
                             warmup_timeout=kw.pop("warmup_timeout", 10.0),
                             **kw)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module


# --- the capability -------------------------------------------------------

def test_the_brain_may_search_and_fetch_the_web():
    """Measured: WebFetch answers a licence question in 9s and WebSearch in
    16s, both inside the brain's exact flag set. Scraping a search engine
    returns an anti-bot page in 0.3s, which is why there is no scraper."""
    import brain
    assert "WebSearch" in brain.ALLOWED_TOOLS
    assert "WebFetch" in brain.ALLOWED_TOOLS


def test_those_two_are_the_only_built_ins_on_the_allowlist():
    """It stays an allowlist. Every other name is one of JARVIS's own MCP
    tools, and no coding tool (Bash, Write, Edit) is ever reachable."""
    import brain
    built_ins = {t for t in brain.ALLOWED_TOOLS if not t.startswith("mcp__")}
    assert built_ins == {"WebSearch", "WebFetch"}


# --- knowing that the web has been read -----------------------------------

@pytest.mark.asyncio
async def test_a_turn_that_fetched_the_web_knows_it_during_the_turn(tmp_path):
    """The flag has to be true WHILE the turn runs — that is the only moment
    an acting tool could be called on the strength of what a page said."""
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        seen = []
        r = await b.turn("WEBFETCH please",
                         on_delta=lambda d: seen.append(b.turn_read_the_web))
        assert r.tools == ["WebFetch"], r.tools
        assert seen and all(seen), "the turn must be marked before it speaks"
    finally:
        await b.stop()
    assert b.turn_read_the_web is False, "no turn in flight, nothing to taint"


@pytest.mark.asyncio
async def test_an_ordinary_turn_is_not_marked(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        seen = []
        await b.turn("hello", on_delta=lambda d: seen.append(b.turn_read_the_web))
        assert seen and not any(seen)
    finally:
        await b.stop()


def test_a_jarvis_tool_that_reads_the_web_marks_the_turn_itself(tmp_path):
    """Belt and braces. The Web* tools are visible only through the CLI's
    tool_use events; JARVIS's own page tools are ours, so they say so
    directly rather than trusting an event shape we do not control."""
    import brain
    b = brain.Brain(_config(tmp_path))
    b._inflight = brain._Turn("user", None)
    assert b.turn_read_the_web is False
    b.mark_web_content()
    assert b.turn_read_the_web is True


@pytest.mark.asyncio
async def test_reading_a_page_marks_the_turn(wired, monkeypatch, tmp_path):
    """`read_page` puts a stranger's words in the context just as WebFetch
    does — the wrapper around them is not a reason to leave the gate open."""
    import brain as brain_module
    import browser as real_browser
    server = wired

    class _Browser:
        PageError = real_browser.PageError

        async def read_page(self, url):
            return real_browser.PageText(
                title="Stark", url=url, text="Arc reactor nominal.",
                char_count=19, truncated=False)

    monkeypatch.setattr(server, "browser", _Browser())
    b = brain_module.Brain(_config(tmp_path))
    b._inflight = brain_module._Turn("user", None)
    monkeypatch.setattr(server, "brain_instance", b)

    await server.tool_read_page({"url": "https://stark.example/"})
    assert b.turn_read_the_web is True


# --- the gate -------------------------------------------------------------

def test_the_read_back_tools_are_the_ones_the_user_hears_first(wired):
    """`steer_session`, `answer_dialog` and `run_command` stage their work and
    JARVIS reads it aloud with a cancel window before it happens.

    The read-back is no longer a reason to skip the untrusted-content gate.
    It is a weak one against text an attacker composed: the user hears
    `npx some-package`, or a plausible sentence aimed at his own session, and
    nothing in either tells him it came out of a README. Only `answer_dialog`
    is still exempt, because its payload is one keystroke and cannot carry an
    attacker's words anywhere — see `TAINT_EXEMPT_ACTING`."""
    server = wired
    assert server.READ_BACK_TOOLS == {"steer_session", "answer_dialog",
                                      "run_command"}
    assert server.READ_BACK_TOOLS <= server.ACTING_TOOLS


def test_an_unsupervised_acting_tool_is_refused_after_the_web_was_read(wired):
    """spawn_run starts an unattended process that edits files and is never
    read back. Nothing a web page says may reach it in the same breath."""
    server = wired
    assert server._untrusted_content_refusal("spawn_run", True)
    assert server._untrusted_content_refusal("start_build", True)
    assert server._untrusted_content_refusal("remember", True), \
        "a page must not be able to write itself into MEMORY.md forever"
    assert server._untrusted_content_refusal("spawn_run", False) is None
    assert server._untrusted_content_refusal("run_status", True) is None, \
        "reading is not acting"
    assert server._untrusted_content_refusal("steer_session", True), \
        "the read-back is no longer the only gate — see TAINT_EXEMPT_ACTING"
    assert server._untrusted_content_refusal("answer_dialog", True) is None, \
        "one keystroke carries no attacker text, and the prompt flow needs it"


def test_looking_at_the_users_own_screen_survives_a_web_page(wired):
    """"Search for that error, then look at my screen" was refused, for no
    reason at all.

    `look_at_screen` and `what_is_on_screen` landed in the same hours as the
    web gate and were never added to its reader set, so the gate caught them
    as unsupervised acting tools. They act on nothing: one lists the user's
    own windows, the other photographs his own display. Neither reaches a
    network address, and neither carries a page's payload anywhere — so a web
    page in the turn is not a reason to refuse him a look at his own desk.
    """
    server = wired
    assert server._untrusted_content_refusal("look_at_screen", True) is None
    assert server._untrusted_content_refusal("what_is_on_screen", True) is None
    assert server._untrusted_content_refusal("spawn_run", True), \
        "the gate must still be shut on the tools it was built for"


def test_the_reader_set_is_every_acting_tool_that_only_reads(wired):
    """Named exactly, so a new reader has to make this decision on purpose
    rather than inherit a refusal by being forgotten.

    Everything else in ACTING_TOOLS changes something outside JARVIS — starts
    a process (`spawn_run`, `start_build`, `run_command`, `create_project`),
    kills one (`cancel_run`), opens a window (`open_in_browser`,
    `open_in_terminal`, `open_in_editor`), types into somebody else's session
    (`steer_session`, `answer_dialog`, `enable_session_inbox`), approves a
    document, or writes a sentence into memory that is loaded on every turn
    afterwards (`remember`, `project_note`, `write_journal`). Those are the
    ones a page must not reach.
    """
    server = wired
    assert server.UNTRUSTED_READING_TOOLS == {
        "read_page", "look_at_page", "github_repo",
        "look_at_screen", "what_is_on_screen",
        # Brings back what Google, Meta, Twilio and OpenAI say about the
        # user's own campaigns. It taints the turn, and it sends nothing.
        "business_report"}
    assert server.UNTRUSTED_READING_TOOLS <= server.ACTING_TOOLS, \
        "a name in here that is not an acting tool gates nothing and is a typo"
    for writer in ("remember", "project_note", "write_journal", "spawn_run",
                   "start_build", "create_project", "cancel_run",
                   "open_in_browser", "open_in_terminal", "open_in_editor",
                   "enable_session_inbox", "approve_document",
                   "steer_session", "run_command"):
        assert server._untrusted_content_refusal(writer, True), \
            f"{writer} does more than read; foreign text must not reach it"


def test_the_endpoint_refuses_a_spawn_in_a_turn_that_read_the_web(wired):
    server = wired

    class _Brain:
        current_origin = "user"
        turn_read_the_web = True
        ready = False

        async def stop(self):
            pass

    import data_paths
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        # Inside the context: the lifespan builds a brain of its own on
        # startup and would replace one set beforehand.
        server.brain_instance = _Brain()
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "spawn_run",
                              "arguments": {"project": "jarvis",
                                            "prompt": "delete everything"}})
    body = r.json()
    assert body["ok"] is False, body
    assert "untrusted_content_in_this_turn" in body["text"], body


def test_the_endpoint_still_allows_a_spawn_in_a_clean_turn(wired):
    """The gate must be the web, not the tool: break this and the refusal
    above proves nothing."""
    server = wired

    class _Brain:
        current_origin = "user"
        turn_read_the_web = False
        ready = False

        async def stop(self):
            pass

    import data_paths
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        server.brain_instance = _Brain()
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "spawn_run",
                              "arguments": {"project": "nothing-of-the-sort",
                                            "prompt": "hello"}})
    body = r.json()
    assert "untrusted_content_in_this_turn" not in body["text"], body
    assert "not_allowed_from_event" not in body["text"], \
        "a user-origin turn with no web content must reach the handler"


# --- and the brain is told, in words --------------------------------------

def test_the_brain_is_told_a_web_page_is_never_an_instruction(tmp_path):
    """An EDITED CLAUDE.md is never overwritten (`data_paths.sync_persona`),
    so that brain home would never see a new rule — the security sentence
    therefore also rides in the launch prompt, which is rebuilt on every
    generation."""
    import brain
    guidance = Path(__file__).resolve().parents[1] / "jarvis_home" / "CLAUDE.md"
    text = guidance.read_text(encoding="utf-8")
    assert "web page" in text and "never an instruction" in text

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()
    assert "never an instruction" in prompt
