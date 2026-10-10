"""The dashboard's honesty rules, checked on the page instead of in a comment.

Each test here corresponds to a rule the source states in prose and used to
break. The harness — the built bundle, a stub API on one loopback origin, and
Playwright's already-installed Chromium — is `tests/dashboard_page.py`, which
explains why it exists at all and why it uses the ASYNC Playwright API.

Nothing here touches the network or the user's data, and every test skips
cleanly on a machine without `npm` or Playwright.
"""

from __future__ import annotations

import time

import pytest

from tests.dashboard_page import (
    dashboard, quiet_machine, run_row, tokens, usage_limits, why_unavailable,
)

UNAVAILABLE = why_unavailable()
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or ""),
]

# Longer than the 60s usage poll and the 10s projects poll, so one
# fast-forward covers every timer the page sets.
POLL_MS = 61_000
# Everything here is local and rendered as soon as its fetch lands.
# Playwright's 30-second default would turn "the code under test is broken"
# into half a minute of waiting per assertion.
WAIT_MS = 5_000


# ── the usage zone owns its whole host ─────────────────────────────────────

async def test_a_recovered_usage_fetch_clears_the_failure_message():
    """The failure path replaced the host's children; the success path only
    ever appended. One dropped 60-second poll therefore left "Cannot read the
    usage limits." sitting permanently above two correct gauges."""
    api = quiet_machine()
    api.fails("/api/usage/limits", 503)
    async with dashboard(api) as page:
        await page.wait_for_selector(".usage-unavailable", timeout=WAIT_MS)

        api.json("/api/usage/limits", usage_limits())    # the poll recovers
        await page.clock.fast_forward(POLL_MS)
        await page.wait_for_selector(".usage-gauge", timeout=WAIT_MS)

        assert await page.locator(".usage-unavailable").count() == 0, (
            "'Cannot read the usage limits.' is still on screen beside live "
            "gauges")
        assert await page.locator(".usage-gauge").count() == 2


async def test_a_window_that_stops_being_reported_stops_being_drawn():
    """Gauges were created once and never removed, so a window that
    disappeared from the payload left its last reading on screen for ever —
    a number nothing is still reporting."""
    api = quiet_machine()
    async with dashboard(api) as page:
        await page.wait_for_function(
            "document.querySelectorAll('.usage-gauge').length === 2",
            timeout=WAIT_MS)

        api.json("/api/usage/limits", usage_limits("five_hour"))
        await page.clock.fast_forward(POLL_MS)

        await page.wait_for_function(
            "document.querySelectorAll('.usage-gauge').length === 1",
            timeout=WAIT_MS)


async def test_a_measured_reading_draws_its_gauges():
    """The control: this is what the two tests above are contrasted with."""
    async with dashboard(quiet_machine()) as page:
        await page.wait_for_selector(".usage-gauge", timeout=WAIT_MS)

        assert await page.locator(".usage-gauge").count() == 2
        assert await page.locator(".usage-unavailable").count() == 0


# ── a transcript we could not read is not an empty transcript ──────────────

async def test_a_failed_transcript_fetch_says_so():
    """The two fetches happen AFTER the pane is built and were outside the
    only try/catch, so a 500 left an empty `.transcript` div — visually
    identical to a run that recorded nothing — plus an unhandled rejection,
    and the "No events recorded" branch was never reached."""
    api = quiet_machine()
    api.json("/api/runs", {"runs": [run_row()]})
    api.json("/api/runs/r1", {"run": run_row()})
    api.fails("/api/runs/r1/events")
    async with dashboard(api) as page:
        await page.wait_for_selector("#history-list .row", timeout=WAIT_MS)
        await page.locator("#history-list .row").first.click()

        await page.wait_for_function(
            "document.getElementById('transcript')?.childElementCount > 0",
            timeout=WAIT_MS)

        assert "Could not load" in await page.locator("#transcript").inner_text()


async def test_a_run_with_no_events_says_that_instead():
    """The other branch, so the message above cannot be mistaken for the
    empty case being renamed."""
    api = quiet_machine()
    api.json("/api/runs", {"runs": [run_row()]})
    api.json("/api/runs/r1", {"run": run_row()})
    api.json("/api/runs/r1/events", {"events": [], "total": 0})
    async with dashboard(api) as page:
        await page.wait_for_selector("#history-list .row", timeout=WAIT_MS)
        await page.locator("#history-list .row").first.click()
        await page.wait_for_function(
            "document.getElementById('transcript')?.childElementCount > 0",
            timeout=WAIT_MS)

        assert "No events recorded" in \
            await page.locator("#transcript").inner_text()


# ── Specs keeps the reader's place ─────────────────────────────────────────

def _long_document(path: str = "docs/superpowers/specs/design.md") -> dict:
    return {
        "project": "claude-browser", "root": "/tmp/cb", "path": path,
        "kind": "spec", "title": "A long design", "modified": 1788404000.0,
        "approval": {"state": "awaiting", "approved_at": None,
                     "approved_sections": []},
        "progress": None, "preamble": "The opening paragraph.\n",
        "sections": [{"number": n, "title": f"Section {n}", "level": 2,
                      "body": ("Body text for this section. " * 40)}
                     for n in range(1, 25)],
    }


def _spec_project(path: str = "/tmp/cb", where: str = "") -> dict:
    return {
        "name": "claude-browser", "path": path, "where": where,
        "state": "awaiting",
        "documents": [{
            "path": "docs/superpowers/specs/design.md", "kind": "spec",
            "title": "A long design", "modified": 1788404000.0,
            "sections": 24,
            "approval": {"state": "awaiting", "approved_at": None,
                         "approved_sections": []},
            "progress": None}],
        "progress": None, "plan_path": "",
        "awaiting": ["docs/superpowers/specs/design.md"],
        "modified": 1788404000.0,
    }


async def _open_specs(page):
    await page.locator("#tab-specs").click()
    await page.wait_for_selector("#specs-doc-body .specs-sections",
                                 timeout=WAIT_MS)


async def test_specs_keeps_your_place_when_the_document_is_re_read():
    """`reconcile` re-fetches the open document on every socket hint, and
    during a build a ticked checkbox fires one every few seconds.
    `paintDetail` ended in an unconditional `body.scrollTop = 0`, directly
    under a comment saying that would make the page unusable."""
    api = quiet_machine()
    api.json("/api/specs", {"projects": [_spec_project()]})
    api.json("/api/specs/doc", _long_document())
    async with dashboard(api) as page:
        await _open_specs(page)

        body = page.locator("#specs-doc-body")
        await page.eval_on_selector("#specs-doc-body",
                                    "el => { el.scrollTop = 400; }")
        was = await body.evaluate("el => el.scrollTop")
        assert was > 0, "the fixture document is not tall enough to scroll"

        # What a socket hint does: leave the tab and come back, which is the
        # same `refreshSpecs() -> reconcile() -> loadDocument()` path.
        await page.locator("#tab-runs").click()
        await page.locator("#tab-specs").click()
        await page.wait_for_selector("#specs-doc-body .specs-sections",
                                     timeout=WAIT_MS)

        assert await body.evaluate("el => el.scrollTop") == was, (
            "the page scrolled back to the top while the user was reading")


async def test_opening_a_different_document_starts_at_the_top():
    """The other half of the rule: keeping your place is about the SAME
    document, not about never scrolling."""
    api = quiet_machine()
    api.json("/api/specs", {"projects": [
        _spec_project(where="in tmp"),
        _spec_project(path="/tmp/cb2", where="worktree runs-dashboard")]})

    def doc_for(query: str):
        """A different title per copy, so the test can wait for the switch
        instead of racing the repaint."""
        which = "second" if "cb2" in query else "first"
        body = _long_document()
        body["title"] = f"The {which} copy"
        return 200, body

    api.routes["/api/specs/doc"] = doc_for
    async with dashboard(api) as page:
        await _open_specs(page)
        await page.wait_for_function(
            "document.getElementById('specs-doc-title').textContent"
            " === 'The first copy'", timeout=WAIT_MS)

        await page.eval_on_selector("#specs-doc-body",
                                    "el => { el.scrollTop = 400; }")
        await page.locator("#specs-projects .row").nth(1).click()
        await page.wait_for_function(
            "document.getElementById('specs-doc-title').textContent"
            " === 'The second copy'", timeout=WAIT_MS)

        assert await page.locator("#specs-doc-body").evaluate(
            "el => el.scrollTop") == 0


async def test_two_copies_of_one_project_are_told_apart():
    """A project with a Claude Code worktree is listed twice under one name.
    Two identical rows would be worse than the bug that hid both."""
    api = quiet_machine()
    api.json("/api/specs", {"projects": [
        _spec_project(where="in tmp"),
        _spec_project(path="/tmp/cb2", where="worktree runs-dashboard")]})
    api.json("/api/specs/doc", _long_document())
    async with dashboard(api) as page:
        await page.locator("#tab-specs").click()
        await page.wait_for_selector("#specs-projects .row", timeout=WAIT_MS)

        text = await page.locator("#specs-projects").inner_text()

        assert await page.locator("#specs-projects .row").count() == 2
        assert "worktree runs-dashboard" in text
        assert "in tmp" in text


# ── the Projects detail pane distinguishes its two nulls ───────────────────

def _project_item(name: str = "chitauri", live: int = 1) -> dict:
    return {
        "name": name, "primary_path": f"/tmp/{name}", "paths": [f"/tmp/{name}"],
        "directory_exists": True, "session_count": 1,
        "live_session_count": live, "needs_you_count": 0, "active": False,
        "last_activity": 1788404000.0, "latest_run": None,
    }


async def test_a_failed_project_detail_fetch_does_not_read_as_no_selection():
    """It rendered "Select a project to see more." — an instruction to do the
    thing the user had just done — with their row still highlighted."""
    api = quiet_machine()
    api.json("/api/projects/view",
             {"projects": [_project_item()], "taken_at": 0.0})
    api.fails("/api/projects/view/chitauri")
    async with dashboard(api) as page:
        await page.locator("#tab-projects").click()
        await page.wait_for_selector("#proj-list .row", timeout=WAIT_MS)
        await page.locator("#proj-list .row").first.click()
        await page.wait_for_function(
            "document.getElementById('proj-detail')"
            "?.innerText.includes('chitauri')", timeout=WAIT_MS)

        text = await page.locator("#proj-detail").inner_text()
        assert "Could not load chitauri" in text
        assert "Select a project" not in text


async def test_a_project_whose_conversations_are_all_dead_is_not_drawn_as_idle():
    """`aggregateState` returned "idle" — the green done-dot — for any
    non-zero `session_count`, and that count includes `gone` and `fresh`."""
    api = quiet_machine()
    api.json("/api/projects/view",
             {"projects": [_project_item(live=0)], "taken_at": 0.0})
    async with dashboard(api) as page:
        await page.locator("#tab-projects").click()
        await page.wait_for_selector("#proj-list .row", timeout=WAIT_MS)

        # `idle` is the green done-dot in `stateStyle`; `unknown` is the
        # empty one. The shape carries the meaning with colour switched off,
        # so the shape is what this asserts.
        dot = page.locator("#proj-list .row .dot").first
        classes = await dot.get_attribute("class") or ""
        assert "dot--done" not in classes, (
            "a project with no live conversation wore the green done-dot")
        assert "dot--void" in classes


# ── a capped agent count renders as a floor in both pills ──────────────────

def _session_row(**over) -> dict:
    row = {
        "session_id": "s1", "voice_name": "chitauri",
        "roster_name": "chitauri-4b", "project": "chitauri",
        "cwd": "/tmp/chitauri", "state": "working", "needs": None,
        "needs_a_human_hand": False, "title": "Doing the thing",
        "summary": "Doing the thing", "last_prompt": "do it",
        "last_text": "working", "recent_tools": [], "started": 1788404000.0,
        "since": 1788404000.0, "origin": "terminal", "steerable": True,
        "pids": [1], "primary_pid": 1, "primary": True,
        "primary_reason": "the only one here",
        "agents_seen": 300, "agents_active": 12, "agents_capped": True,
    }
    row.update(over)
    return row


def _roster(rows: list[dict]) -> dict:
    """A roster reading taken NOW — otherwise the staleness banner (which is
    its own test) covers whatever this one is about."""
    return {"sessions": rows,
            "projects": {r["project"]: [r["session_id"]] for r in rows},
            "taken_at": time.time()}


async def test_a_capped_active_agent_count_renders_as_a_floor():
    """`agents_seen` has rendered as "300+" all along. `agents_active` is a
    floor for exactly the same reason — the cap is taken in file-name order,
    which is uncorrelated with recency — and rendered bare."""
    api = quiet_machine()
    api.json("/api/sessions", _roster([_session_row()]))
    async with dashboard(api) as page:
        await page.locator("#tab-sessions").click()
        await page.wait_for_selector("#sessions-view .row", timeout=WAIT_MS)

        # The pill is upper-cased by the stylesheet, so the assertion is too.
        text = (await page.locator("#sessions-view").inner_text()).lower()
        assert "12+ agents" in text


async def test_an_uncapped_active_agent_count_renders_exactly():
    api = quiet_machine()
    api.json("/api/sessions", _roster(
        [_session_row(agents_seen=12, agents_capped=False)]))
    async with dashboard(api) as page:
        await page.locator("#tab-sessions").click()
        await page.wait_for_selector("#sessions-view .row", timeout=WAIT_MS)

        text = (await page.locator("#sessions-view").inner_text()).lower()
        assert "12 agents" in text and "12+ agents" not in text


# ── whose prompt it is, and where ──────────────────────────────────────────
#
# Measured live, 2026-09-28: Paperclip drove a session through the Agent SDK,
# and every blocked session wore one badge — "your keystroke / it wants a key
# pressed in that terminal". That session had no terminal, and Paperclip
# answered its prompts itself. tests/test_prompt_owner.py holds the rule;
# these hold what the page draws from it.

async def _open_session(page) -> str:
    await page.locator("#tab-sessions").click()
    await page.wait_for_selector("#sessions-view .row", timeout=WAIT_MS)
    await page.locator("#sessions-view .sess-row").first.click()
    await page.wait_for_selector("#sessions-view .sd-name", timeout=WAIT_MS)
    return await page.locator("#sessions-view").inner_text()


async def test_a_program_s_held_prompt_is_drawn_as_the_program_s():
    api = quiet_machine()
    api.json("/api/sessions", _roster([_session_row(
        state="needs_you", needs="permission prompt", needs_a_human_hand=True,
        origin="background", entrypoint="sdk-ts")]))
    async with dashboard(api) as page:
        text = await _open_session(page)
        assert "The program that started this session shows that prompt" in text
        assert "its program's" in text.lower()
        assert "your keystroke" not in text.lower()
        assert "key pressed" not in text
        assert "background · sdk-ts" in text, "started as: origin beside the roster's word"


async def test_a_desktop_prompt_says_it_is_in_the_desktop_app():
    api = quiet_machine()
    api.json("/api/sessions", _roster([_session_row(
        state="needs_you", needs="permission prompt", needs_a_human_hand=True,
        origin="desktop", entrypoint="claude-desktop")]))
    async with dashboard(api) as page:
        text = await _open_session(page)
        assert "waiting in the Claude desktop app" in text
        assert "your keystroke" not in text.lower()


async def test_a_terminal_prompt_still_wants_a_keystroke():
    api = quiet_machine()
    api.json("/api/sessions", _roster([_session_row(
        state="needs_you", needs="permission prompt", needs_a_human_hand=True,
        origin="terminal", entrypoint="cli")]))
    async with dashboard(api) as page:
        text = await _open_session(page)
        assert "your keystroke" in text.lower()
        assert "key pressed in its terminal" in text


async def test_a_session_its_host_is_answering_says_so_and_asks_nothing():
    api = quiet_machine()
    api.json("/api/sessions", _roster([_session_row(
        state="working", origin="background", entrypoint="sdk-ts",
        waiting_on_host="permission prompt")]))
    async with dashboard(api) as page:
        text = await _open_session(page)
        assert "paused on “permission prompt”" in text
        assert "the program that started it answers itself" in text
        assert "Nothing is asked of you" in text


async def test_a_row_from_an_older_server_still_renders():
    """No `entrypoint`, no `waiting_on_host`: a server from before these
    fields must not break the page it is still serving."""
    api = quiet_machine()
    row = _session_row(state="needs_you", needs="permission prompt",
                       needs_a_human_hand=True)
    api.json("/api/sessions", _roster([row]))
    async with dashboard(api) as page:
        text = await _open_session(page)
        assert "your keystroke" in text.lower()
        assert "started as" in text.lower()


async def test_the_projects_tab_draws_a_program_s_prompt_as_the_program_s():
    api = quiet_machine()
    item = _project_item()
    item["needs_you_count"] = 1
    api.json("/api/projects/view", {"projects": [item], "taken_at": 0.0})
    api.json("/api/projects/view/chitauri", {"project": {
        **item,
        "sessions": [_session_row(
            state="needs_you", needs="permission prompt",
            needs_a_human_hand=True, origin="background", entrypoint="sdk-ts")],
        "runs": [], "repo": {"exists": False, "headline": "", "body": ""},
        "build": {"has_spec": False, "has_plan": False, "progress": None},
    }})
    async with dashboard(api) as page:
        await page.locator("#tab-projects").click()
        await page.wait_for_selector("#proj-list .row", timeout=WAIT_MS)
        await page.locator("#proj-list .row").first.click()
        await page.wait_for_function(
            "document.getElementById('proj-detail')"
            "?.innerText.includes('permission prompt')", timeout=WAIT_MS)
        text = await page.locator("#proj-detail").inner_text()
        assert "its program's" in text.lower()
        assert "your keystroke" not in text.lower()


# ── a frozen roster reading says so ────────────────────────────────────────

async def test_a_stale_roster_reading_is_visible():
    """`taken_at` was in the payload and in the type and rendered NOWHERE, so
    a watcher frozen by one bad transcript line went on serving 200s that
    looked live."""
    api = quiet_machine()
    stale = _roster([_session_row()])
    stale["taken_at"] = time.time() - 3600
    api.json("/api/sessions", stale)
    async with dashboard(api) as page:
        await page.locator("#tab-sessions").click()
        await page.wait_for_selector("#sessions-banner:not([hidden])",
                                     timeout=WAIT_MS)

        assert "reading from" in \
            await page.locator("#sessions-banner").inner_text()


async def test_a_fresh_roster_reading_says_nothing():
    api = quiet_machine()
    api.json("/api/sessions", _roster([_session_row()]))
    async with dashboard(api) as page:
        await page.locator("#tab-sessions").click()
        await page.wait_for_selector("#sessions-view .row", timeout=WAIT_MS)

        assert await page.locator("#sessions-banner").is_hidden()


# ── the Usage caption only claims what it can prove ────────────────────────

def _usage_session(session_id: str, total: int) -> dict:
    return {
        "session_id": session_id, "cwd": f"/tmp/{session_id}",
        "project": session_id, "tokens": tokens(total), "turns": 1,
        "agents": [], "agent_tokens": tokens(), "agent_count": 0,
        "active_agents": 0, "models": [], "first_at": 1788404000.0,
        "last_at": 1788404000.0, "context_tokens": total,
        "total_tokens": tokens(total), "own": False,
    }


def _usage_body(count: int, largest_listed: int) -> dict:
    body = dict(quiet_machine().routes["/api/usage/sessions"][1])
    body.update({
        "measured": True, "scanned_at": 1788404500.0,
        "session_count": count, "project_count": count,
        "largest_listed": largest_listed,
        "totals": tokens(10_000),
        "sessions": [_usage_session(f"s{i:03d}", 1000 - i)
                     for i in range(30)],
    })
    return body


async def test_the_caption_says_smaller_only_when_it_ranked_them():
    api = quiet_machine()
    api.json("/api/usage/sessions", _usage_body(count=900, largest_listed=40))
    async with dashboard(api) as page:
        await page.locator("#tab-usage").click()
        await page.wait_for_selector(".usage-more", timeout=WAIT_MS)

        assert "smaller conversations not listed" in \
            await page.locator(".usage-more").inner_text()


async def test_the_caption_says_other_when_it_cannot_prove_smaller():
    """`largest_listed` below the number of rows shown means the payload
    carries no ranking that reaches the bottom row, so "smaller" would be a
    claim about conversations nothing compared."""
    api = quiet_machine()
    api.json("/api/usage/sessions", _usage_body(count=900, largest_listed=5))
    async with dashboard(api) as page:
        await page.locator("#tab-usage").click()
        await page.wait_for_selector(".usage-more", timeout=WAIT_MS)

        text = await page.locator(".usage-more").inner_text()
        assert "other conversations not listed" in text
        assert "smaller" not in text


# ── the Memory tab says when the index and the folder disagree ─────────────

def _memory_snapshot(**over) -> dict:
    snap = {
        "path": "/tmp/jarvis-home", "index": [], "memories": [], "projects": [],
        "journal": [], "latest_journal_slug": None, "unindexed": [],
    }
    snap.update(over)
    return snap


async def test_unindexed_memories_are_named_and_can_be_indexed_from_the_page():
    """Four notes sat in memory/ for five days with an empty MEMORY.md and
    the page showed "Nothing indexed yet." beside four files — a fact stated
    twice and a problem stated nowhere."""
    api = quiet_machine()
    orphan = {"slug": "starnet-station", "title": "StarNet station", "modified": 1788404000.0}
    api.json("/api/memory", _memory_snapshot(memories=[orphan], unindexed=[orphan]))
    api.json("/api/memory/reindex", {"indexed": ["starnet-station"], "left_out": [], "full": False})
    async with dashboard(api) as page:
        await page.locator("#tab-memory").click()
        await page.wait_for_selector("#memory-index-list .callout", timeout=WAIT_MS)

        # `inner_text` reflects the stylesheet's uppercase callout label.
        text = (await page.locator("#memory-index-list").inner_text()).lower()
        assert "starnet station" in text
        assert "not in the index" in text

        api.json("/api/memory", _memory_snapshot(
            memories=[orphan],
            index=[{"title": "StarNet station", "slug": "starnet-station", "hook": "the crew"}]))
        await page.locator("#memory-index-list button").click()
        await page.wait_for_selector("#memory-index-list .row", timeout=WAIT_MS)

        assert "/api/memory/reindex" in api.calls
        text = (await page.locator("#memory-index-list").inner_text()).lower()
        assert "not in the index" not in text
        assert "the crew" in text


async def test_an_index_that_matches_the_folder_shows_no_notice():
    api = quiet_machine()
    entry = {"slug": "starnet-station", "title": "StarNet station", "modified": 1788404000.0}
    api.json("/api/memory", _memory_snapshot(
        memories=[entry],
        index=[{"title": "StarNet station", "slug": "starnet-station", "hook": "the crew"}]))
    async with dashboard(api) as page:
        await page.locator("#tab-memory").click()
        await page.wait_for_selector("#memory-index-list .row", timeout=WAIT_MS)
        assert await page.locator("#memory-index-list .callout").count() == 0
