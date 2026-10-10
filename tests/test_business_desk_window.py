"""The Business desk: a clock on every approval and on the briefing, and a
window instead of a modal.

Same treatment the Conversation panel got. Every approval card says when it
was staged, last changed and (while pending) when it expires, to the
second; the briefing says when it was assembled — a server stamp, since a
computed snapshot has no row of its own. The desk itself opens as a window:
no backdrop, the page behind it still usable, minimises to its header,
drags by that header, moves by arrow keys, and remembers where it was left
and whether it was open. The window behaviour is `panelwindow.ts`, ONE
module shared with the Conversation panel.
"""

import time
from pathlib import Path

import pytest

from tests.dashboard_page import dashboard, noon_today, quiet_machine, why_unavailable

ROOT = Path(__file__).parent.parent
UNAVAILABLE = why_unavailable()
_browser = pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")


# --- the server stamps the briefing ------------------------------------------

def test_the_briefing_says_when_it_was_made(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import business_store as store
    store.init_db()
    before = time.time()
    result = store.briefing()
    after = time.time()
    assert isinstance(result["generated_at"], float)
    assert before <= result["generated_at"] <= after


# --- the wiring, pinned in the source ------------------------------------------

def _src() -> str:
    return (ROOT / "frontend/src/business.ts").read_text(encoding="utf-8")


def test_the_desk_shares_the_window_machinery_and_the_clock_format():
    src = _src()
    assert 'from "./panelwindow"' in src and "makeWindow(" in src
    assert "timeElement(" in src and 'from "./uibits"' in src
    assert "showModal()" not in src, "a window that minimises cannot also be a modal with a backdrop"
    assert "dialog.show()" in src


def test_the_desk_stylesheet_has_the_window_states():
    css = (ROOT / "frontend/src/business.css").read_text(encoding="utf-8")
    assert "#business.minimized" in css
    assert "#business-head" in css
    assert "touch-action: none" in css
    assert "position: fixed" in css


def test_the_docs_say_so():
    text = (ROOT / "docs/business.md").read_text(encoding="utf-8")
    assert "to the second" in text
    assert "minimise" in text.lower()


# --- the page, in a real browser -----------------------------------------------

def _desk(items, briefing=None):
    api = quiet_machine()
    api.json("/api/business/connections", {"providers": {}})
    api.json("/api/business/actions", {"items": items})
    api.json("/api/business/records/task", {"items": []})
    api.json("/api/business/briefing", briefing or {
        "open_tasks": 0, "overdue_tasks": 0, "leads": 0, "receivables_minor": {},
        "unpaid_expenses_minor": {}, "actions": {}, "generated_at": time.time()})
    return api


def _pending(created, updated, expires):
    return {"id": "draft", "seq": 1, "provider": "twilio", "operation": "call", "state": "pending",
            "digest": "a" * 64, "created": created, "updated": updated, "expires": expires,
            "payload": {"message": "hello"}, "result": {}}


def _clock(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


@_browser
@pytest.mark.asyncio
async def test_every_approval_and_the_briefing_carry_a_clock():
    now = noon_today()
    created, updated, expires, made = now - 3600, now - 60, now + 86400, now - 5
    api = _desk([_pending(created, updated, expires)], {
        "open_tasks": 1, "overdue_tasks": 0, "leads": 2, "receivables_minor": {},
        "unpaid_expenses_minor": {}, "actions": {"pending": 1}, "generated_at": made})

    async def at_noon(page):
        await page.clock.set_system_time(now)

    async with dashboard(api, at_noon) as page:
        await page.get_by_role("button", name="Business desk").click()
        await page.get_by_role("button", name="Approve & send").wait_for()
        meta = page.locator("#business article .business-meta")
        text = await meta.inner_text()
        stamps = meta.locator("time")
        assert await stamps.count() == 3, text
        assert await stamps.nth(0).inner_text() == _clock(created)
        assert await stamps.nth(1).inner_text() == _clock(updated)
        assert "expires" in text.lower()
        assert await stamps.nth(0).get_attribute("title")
        assert await stamps.nth(0).get_attribute("datetime")

        await page.get_by_role("button", name="Briefing", exact=True).click()
        asof = page.locator("#business .business-meta time")
        await asof.first.wait_for()
        assert await asof.first.inner_text() == _clock(made)
        assert "as of" in (await page.locator("#business .business-meta").first.inner_text()).lower()


@_browser
@pytest.mark.asyncio
async def test_the_desk_is_a_window_that_minimises_drags_nudges_and_remembers():
    api = _desk([])
    async with dashboard(api) as page:
        await page.get_by_role("button", name="Business desk").click()
        desk = page.locator("#business")
        await page.locator("#business-toggle").wait_for()
        assert await page.evaluate("document.querySelector('#business').matches(':modal')") is False, \
            "a modal's backdrop would make a minimised desk pointless"

        toggle = page.locator("#business-toggle")
        assert await toggle.get_attribute("aria-expanded") == "true"
        await toggle.click()
        assert await page.locator("#business-body").is_hidden()
        assert await toggle.get_attribute("aria-expanded") == "false"
        await toggle.click()
        assert await page.locator("#business-body").is_visible()

        before = await desk.bounding_box()
        grip = await page.locator("#business-head").bounding_box()
        x, y = grip["x"] + 30, grip["y"] + grip["height"] / 2
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 90, y + 120, steps=8)
        await page.mouse.up()
        after = await desk.bounding_box()
        assert abs((after["x"] - before["x"]) - 90) <= 2
        assert abs((after["y"] - before["y"]) - 120) <= 2

        await page.locator("#business-head").focus()
        await page.keyboard.press("ArrowLeft")
        nudged = await desk.bounding_box()
        assert abs((after["x"] - nudged["x"]) - 20) <= 1
        await page.keyboard.press("Enter")
        assert await page.locator("#business-body").is_hidden(), "Enter on the header toggles"

        await page.reload()
        await page.get_by_role("button", name="Business desk").click()
        await page.locator("#business-toggle").wait_for()
        assert await page.locator("#business-body").is_hidden(), "minimised is remembered"
        again = await desk.bounding_box()
        assert abs(again["x"] - nudged["x"]) <= 2 and abs(again["y"] - nudged["y"]) <= 2

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_function(
            "(() => { const r = document.querySelector('#business').getBoundingClientRect();"
            " return r.left >= 0 && r.right <= 391; })()")
