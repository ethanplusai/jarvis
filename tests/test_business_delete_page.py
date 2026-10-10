"""Deleting finished approvals from the Business desk, on the page.

A finished card offers **Delete**; a live one does not. Delete is two
clicks — the first arms it ("Confirm delete"), the second sends — because
this is a record of money-moving requests and a stray click must not clear
one. **Clear completed** does the same for every finished card at once and
only appears when there is something finished to clear.
"""

import time
from pathlib import Path

import pytest

from tests.dashboard_page import dashboard, quiet_machine, why_unavailable

ROOT = Path(__file__).parent.parent
UNAVAILABLE = why_unavailable()
_browser = pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")


def test_the_page_deletes_over_http_and_arms_first():
    src = (ROOT / "frontend/src/business.ts").read_text(encoding="utf-8")
    assert '"DELETE"' in src
    assert "clear-completed" in src
    assert "Confirm delete" in src, "a stray click must not clear a money-moving record"


def test_the_docs_say_what_delete_does_and_does_not_do():
    text = (ROOT / "docs/business.md").read_text(encoding="utf-8")
    assert "Delete" in text and "ledger" in text.lower()
    assert "Clear completed" in text


def _action(action_id, state, seq):
    now = time.time()
    return {"id": action_id, "seq": seq, "provider": "twilio", "operation": "call", "state": state,
            "digest": "a" * 64, "created": now - 600, "updated": now - 300, "expires": now + 86400,
            "payload": {"message": "hello"}, "result": {"message": "done"}}


def _desk(items):
    api = quiet_machine()
    api.json("/api/business/connections", {"providers": {}})
    api.json("/api/business/actions", {"items": items})
    api.json("/api/business/briefing", {"open_tasks": 0, "overdue_tasks": 0, "leads": 0,
             "receivables_minor": {}, "unpaid_expenses_minor": {}, "actions": {}, "generated_at": time.time()})
    return api


@_browser
@pytest.mark.asyncio
async def test_a_finished_approval_is_deleted_in_two_clicks_and_a_live_one_cannot_be():
    items = [_action("done", "submitted", 2), _action("live", "pending", 1)]
    api = _desk(items)
    deleted = []

    async def setup(page):
        async def on_delete(route):
            if route.request.method != "DELETE":
                await route.continue_()
                return
            deleted.append(route.request.url)
            items[:] = [i for i in items if i["id"] != "done"]
            await route.fulfill(json={"id": "done", "deleted": time.time()})
        await page.route("**/api/business/actions/done", on_delete)

    async with dashboard(api, setup) as page:
        await page.get_by_role("button", name="Business desk").click()
        await page.get_by_role("button", name="Approve & send").wait_for()
        cards = page.locator("#business article")
        assert await cards.count() == 2
        assert await page.get_by_role("button", name="Delete", exact=True).count() == 1, \
            "only the finished card offers it"

        await page.get_by_role("button", name="Delete", exact=True).click()
        assert deleted == [], "the first click arms; nothing is sent"
        confirm = page.get_by_role("button", name="Confirm delete")
        await confirm.wait_for()
        await confirm.click()
        await page.get_by_text("Deleted.", exact=False).wait_for()

        assert len(deleted) == 1 and deleted[0].endswith("/api/business/actions/done")
        assert await cards.count() == 1
        assert await page.get_by_role("button", name="Approve & send").count() == 1, "the live one is untouched"


@_browser
@pytest.mark.asyncio
async def test_clear_completed_takes_every_finished_card_and_appears_only_when_there_is_one():
    items = [_action("a", "submitted", 3), _action("b", "failed", 2), _action("live", "pending", 1)]
    api = _desk(items)
    cleared = []

    async def setup(page):
        async def on_clear(route):
            cleared.append(route.request.method)
            items[:] = [i for i in items if i["state"] == "pending"]
            await route.fulfill(json={"deleted": 2})
        await page.route("**/api/business/actions/clear-completed", on_clear)

    async with dashboard(api, setup) as page:
        await page.get_by_role("button", name="Business desk").click()
        await page.get_by_role("button", name="Approve & send").wait_for()
        clear = page.get_by_role("button", name="Clear completed", exact=True)
        assert await clear.count() == 1
        await clear.click()
        assert cleared == []
        await page.get_by_role("button", name="Confirm clear").click()
        await page.get_by_text("Cleared 2", exact=False).wait_for()

        assert cleared == ["POST"]
        assert await page.locator("#business article").count() == 1
        assert await page.get_by_role("button", name="Clear completed", exact=True).count() == 0, \
            "nothing finished is left, so the button goes away"
