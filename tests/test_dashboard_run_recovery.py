"""Run actions and async recovery exercised against the shipped browser bundle."""
import asyncio
import json
from urllib.parse import parse_qs

import pytest

from tests.dashboard_page import dashboard, quiet_machine, run_row, why_unavailable

UNAVAILABLE = why_unavailable()
pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")]


def machine():
    api = quiet_machine()
    api.json("/api/runs", {"runs": [run_row("r1"),
             {**run_row("r2"), "created_at": 1788403999.0}]})
    for name in ("r1", "r2"):
        api.json(f"/api/runs/{name}", {"run": run_row(name)})
        api.json(f"/api/runs/{name}/events", {"events": [], "total": 0})
    return api


async def open_first(page):
    await page.locator("#history-list .row").first.click()
    await page.locator("#transcript .empty").wait_for()


async def test_retry_failure_is_visible_and_button_recovers():
    api = machine()
    api.fails("/api/runs/r1/retry", 403)
    async with dashboard(api) as page:
        await open_first(page)
        await page.get_by_role("button", name="Retry", exact=True).click()
        await page.wait_for_function("document.querySelector('#run-action-message').textContent.includes('Could not retry')")
        assert await page.get_by_role("button", name="Retry", exact=True).is_enabled()


async def test_retry_is_single_flight_and_opens_the_new_run():
    api = machine()
    release = asyncio.Event()
    requests = []

    async def setup(page):
        async def retry(route):
            requests.append(route.request.url)
            await release.wait()
            await route.fulfill(json={"run_id": "r2"})
        await page.route("**/api/runs/r1/retry", retry)

    async with dashboard(api, setup) as page:
        await open_first(page)
        await page.get_by_role("button", name="Retry", exact=True).click()
        await page.wait_for_function("document.querySelector('.pane-actions button').disabled")
        await page.locator(".pane-actions button").evaluate("b => b.click()")
        assert len(requests) == 1
        release.set()
        await page.wait_for_function("document.querySelector('#detail .kv')?.textContent.includes('r2')")


async def test_failed_old_request_does_not_pollute_new_detail():
    api = machine()
    release = asyncio.Event()
    started = asyncio.Event()

    async def setup(page):
        async def old(route):
            started.set()
            await release.wait()
            await route.fulfill(status=500, json={"error": "failed"})
        await page.route("**/api/runs/r1", old)

    async with dashboard(api, setup) as page:
        await page.locator("#history-list .row").first.click()
        await started.wait()
        await page.locator("#history-list .row").nth(1).click()
        await page.locator("#transcript .empty").wait_for()
        release.set()
        await page.wait_for_function("document.querySelector('#detail .kv')?.textContent.includes('r2')")
        # Synchronize with completion of the failed request, not a fixed sleep.
        await page.wait_for_load_state("networkidle")
        assert "Could not load this run." not in await page.locator("#detail").inner_text()


def event(seq):
    return {"id": seq, "run_id": "r1", "seq": seq, "ts": 0, "kind": "assistant",
            "payload": json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": f"event-{seq}"}]}})}


async def test_gap_recovers_all_pages_and_deduplicates_live_events():
    api = machine()
    sockets = []

    async def setup(page):
        await page.route_web_socket("**/ws/runs", lambda ws: sockets.append(ws))

    async with dashboard(api, setup) as page:
        await open_first(page)

        def events(query):
            q = parse_qs(query)
            after = int(q.get("after_seq", [0])[0])
            limit = int(q.get("limit", [200])[0])
            return 200, {"events": [event(i) for i in range(after + 1, min(451, after + limit + 1))], "total": 450}

        api.routes["/api/runs/r1/events"] = events
        sockets[0].send(json.dumps({"type": "run_event", "run_id": "r1", "seq": 450,
                                    "kind": "assistant", "payload": json.loads(event(450)["payload"])}))
        await page.get_by_text("event-450", exact=True).wait_for()
        assert await page.locator("#transcript .ev").count() == 200
        assert await page.locator("#transcript .empty").count() == 0
        for _ in range(2):
            sockets[0].send(json.dumps({"type": "run_event", "run_id": "r1", "seq": 450,
                                        "kind": "assistant", "payload": json.loads(event(450)["payload"])}))
        # A subsequent event is the barrier proving the duplicate frames arrived.
        sockets[0].send(json.dumps({"type": "run_event", "run_id": "r1", "seq": 451,
                                    "kind": "assistant", "payload": json.loads(event(451)["payload"])}))
        await page.get_by_text("event-451", exact=True).wait_for()
        assert await page.get_by_text("event-450", exact=True).count() == 1
