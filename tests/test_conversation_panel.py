"""The Conversation panel: a clock on every message, and a window you can
put where you want it.

Every message shows when it happened, to the second — the server's
`created_at` for anything it has recorded, the browser's clock for an
unsent draft and for his live text reply. The panel itself minimises to its
header, drags by that header anywhere on the page (clamped to the viewport,
re-clamped when the window shrinks), moves by arrow keys for anyone without
a mouse, and remembers both where it is and whether it is minimised.

The pure decisions (clamping, formatting, remembering) live in
`frontend/src/panelstate.ts` under node:test. What is asserted here is the
assembled page in a real browser, the way `tests/test_conversation_page.py`
does — plus a few source-level pins for the wiring no browser test can
cheaply reach.
"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests.dashboard_page import dashboard, noon_today, quiet_machine, why_unavailable
from tests.test_settings_page import no_gpu

UNAVAILABLE = why_unavailable()
_browser = pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")
WAIT_MS = 5_000


# --- the wiring, pinned in the source ------------------------------------------

def _conv() -> str:
    return (ROOT / "frontend/src/conversation.ts").read_text(encoding="utf-8")


def test_every_message_gets_a_time_element_from_the_shared_builder():
    """`timeElement` (uibits.ts) is the one place a <time> is built, on top
    of the pure formatter in panelstate.ts."""
    src = _conv()
    assert 'from "./uibits"' in src and "timeElement(" in src
    shared = (ROOT / "frontend/src/uibits.ts").read_text(encoding="utf-8")
    assert "formatStamp(" in shared and "stampTitle(" in shared


def test_a_draft_and_a_live_reply_are_stamped_when_they_are_made():
    """The server never sees an unsent draft, and his live reply exists
    before its record does — so those two carry the browser's own clock."""
    src = _conv()
    submit = src[src.index("function submit("):src.index("async function refresh(")]
    assert "created_at" in submit, "an unsent draft has no server row to take a time from"
    live = src[src.index("function showReply("):src.index("function settleReply(")]
    assert "created_at" in live


def _window() -> str:
    """The window behaviour is ONE module, shared with the Business desk."""
    return (ROOT / "frontend/src/panelwindow.ts").read_text(encoding="utf-8")


def test_the_panel_is_a_window_through_the_shared_module():
    assert 'from "./panelwindow"' in _conv() and "makeWindow(" in _conv()


def test_dragging_uses_pointer_capture_and_never_starts_on_a_button():
    src = _window()
    assert "setPointerCapture(" in src
    assert 'closest("button' in src, "a click on Minimise must not begin a drag"


def test_the_panel_remembers_where_it_is_and_whether_it_is_minimised():
    src = _window()
    assert "loadPanelState(" in src and "savePanelState(" in src
    assert "clampPosition(" in src
    assert 'addEventListener("resize"' in src, "a saved position must survive a smaller window"


def test_the_header_is_reachable_by_keyboard():
    src = _window()
    assert "nudge(" in src
    assert 'aria-expanded' in src


def test_the_stylesheet_has_the_two_states():
    css = (ROOT / "frontend/src/style.css").read_text(encoding="utf-8")
    assert "#conversation.minimized" in css
    assert "#conversation-head" in css
    assert "touch-action: none" in css, "or the drag fights the page scroll on a phone"


def test_the_readme_says_so():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "to the second" in readme
    assert "minimise" in readme.lower()


# --- the page, in a real browser -----------------------------------------------

def _api(messages):
    api = quiet_machine()
    api.json("/api/settings/status", {"claude_code_installed": True,
             "server_port": 8340, "uptime_seconds": 1,
             "env_keys_set": {"fish_audio": True, "fish_voice_id": True, "user_name": "Ada"}})
    api.json("/api/settings/preferences", {"user_name": "Ada", "honorific": "none"})
    api.json("/api/conversation", {"messages": messages})
    return api


def _row(seq, role, text, created_at):
    return {"id": f"m{seq}", "seq": seq, "role": role, "text": text,
            "status": "delivered" if role == "assistant" else "accepted",
            "created_at": created_at}


async def _setup(page):
    await no_gpu(page)
    await page.route_web_socket("**/ws/voice", lambda ws: None)


@_browser
@pytest.mark.asyncio
async def test_every_message_shows_its_time_to_the_second():
    now = noon_today()
    today = now - 90
    yesterday = now - 86_400 - 60
    api = _api([_row(1, "user", "earlier", yesterday), _row(2, "assistant", "Six, sir.", today)])

    async def at_noon(page):
        await _setup(page)
        await page.clock.set_system_time(now)

    async with dashboard(api, at_noon, entry="index.html") as page:
        await page.get_by_text("Six, sir.", exact=True).wait_for(timeout=WAIT_MS)
        stamps = page.locator("#conversation-log time")
        assert await stamps.count() == 2
        texts = await stamps.all_inner_texts()
        assert texts[1] == time.strftime("%H:%M:%S", time.localtime(today))
        assert texts[0].endswith(time.strftime("%H:%M:%S", time.localtime(yesterday)))
        assert len(texts[0]) > 8, "yesterday's message names the day as well"
        title = await stamps.nth(0).get_attribute("title")
        assert time.strftime("%Y", time.localtime(yesterday)) in title
        assert await stamps.nth(1).get_attribute("datetime")


@_browser
@pytest.mark.asyncio
async def test_an_unsent_draft_is_stamped_by_the_browser():
    api = _api([])
    async with dashboard(api, _setup, entry="index.html") as page:
        await page.locator("#message-input").fill("a draft")
        await page.locator("#composer button").click()
        await page.get_by_text("a draft", exact=True).wait_for(timeout=WAIT_MS)
        assert await page.locator("#conversation-log time").count() == 1
        assert len(await page.locator("#conversation-log time").inner_text()) == 8


@_browser
@pytest.mark.asyncio
async def test_minimise_hides_the_body_and_survives_a_reload():
    api = _api([_row(1, "assistant", "Six, sir.", time.time())])
    async with dashboard(api, _setup, entry="index.html") as page:
        await page.get_by_text("Six, sir.", exact=True).wait_for(timeout=WAIT_MS)
        toggle = page.locator("#conversation-toggle")
        assert await toggle.get_attribute("aria-expanded") == "true"
        await toggle.click()
        assert await page.locator("#conversation-body").is_hidden()
        assert await page.locator("#composer").is_hidden()
        assert await toggle.get_attribute("aria-expanded") == "false"
        await page.reload()
        await page.locator("#conversation-toggle").wait_for(timeout=WAIT_MS)
        assert await page.locator("#conversation-body").is_hidden(), "minimised is remembered"
        await page.locator("#conversation-toggle").click()
        assert await page.locator("#composer").is_visible()


@_browser
@pytest.mark.asyncio
async def test_the_panel_drags_by_its_header_remembers_and_stays_on_screen():
    api = _api([_row(1, "assistant", "Six, sir.", time.time())])
    async with dashboard(api, _setup, entry="index.html") as page:
        await page.get_by_text("Six, sir.", exact=True).wait_for(timeout=WAIT_MS)
        panel = page.locator("#conversation")
        head = page.locator("#conversation-head")
        before = await panel.bounding_box()
        grip = await head.bounding_box()
        x, y = grip["x"] + 20, grip["y"] + grip["height"] / 2
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x - 150, y - 200, steps=8)
        await page.mouse.up()
        after = await panel.bounding_box()
        assert abs((before["x"] - after["x"]) - 150) <= 2
        assert abs((before["y"] - after["y"]) - 200) <= 2

        await page.reload()
        await page.get_by_text("Six, sir.", exact=True).wait_for(timeout=WAIT_MS)
        again = await panel.bounding_box()
        assert abs(again["x"] - after["x"]) <= 2 and abs(again["y"] - after["y"]) <= 2, \
            "where it was put is where it comes back"

        await page.set_viewport_size({"width": 390, "height": 844})
        # The re-clamp rides the window's `resize` event, which arrives a tick
        # after the viewport changes.
        await page.wait_for_function(
            "(() => { const r = document.querySelector('#conversation').getBoundingClientRect();"
            " return r.left >= 0 && r.right <= 391; })()", timeout=WAIT_MS)
        small = await panel.bounding_box()
        assert small["x"] >= 0 and small["x"] + small["width"] <= 390 + 1
        assert small["y"] >= 0 and small["y"] + small["height"] <= 844 + 1

        # A click on the button inside the header is a click, not a drag.
        await page.locator("#conversation-toggle").click()
        assert await page.locator("#conversation-body").is_hidden()
        assert await page.locator("#conversation-toggle").get_attribute("aria-expanded") == "false"


@_browser
@pytest.mark.asyncio
async def test_arrow_keys_move_the_panel_for_keyboard_users():
    api = _api([_row(1, "assistant", "Six, sir.", time.time())])
    async with dashboard(api, _setup, entry="index.html") as page:
        await page.get_by_text("Six, sir.", exact=True).wait_for(timeout=WAIT_MS)
        panel = page.locator("#conversation")
        await page.locator("#conversation-head").focus()
        before = await panel.bounding_box()
        await page.keyboard.press("ArrowLeft")
        await page.keyboard.press("ArrowUp")
        after = await panel.bounding_box()
        assert abs((before["x"] - after["x"]) - 20) <= 1
        assert abs((before["y"] - after["y"]) - 20) <= 1
        await page.keyboard.press("Enter")
        assert await page.locator("#conversation-body").is_hidden(), "Enter on the header toggles"
