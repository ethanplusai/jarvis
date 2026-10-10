from urllib.parse import parse_qs
import pytest
from tests.dashboard_page import dashboard, quiet_machine, run_row, why_unavailable

UNAVAILABLE = why_unavailable()
pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")]


async def test_old_active_run_stays_visible_while_history_pages_and_filters():
    api = quiet_machine()
    active = {**run_row("active", "running"), "created_at": 1, "project_name": "old active"}
    history = [{**run_row(f"r{i:03}"), "created_at": 100,
                "project_name": "selected" if i % 2 else "other"} for i in reversed(range(105))]
    def runs(query):
        params = parse_qs(query)
        if params.get("status") == ["queued,running"]:
            return 200, {"runs": [active]}
        rows = history
        if params.get("project"):
            rows = [r for r in rows if params["project"][0] in r["project_name"]]
        if params.get("before_id"):
            rows = [r for r in rows if r["id"] < params["before_id"][0]]
        return 200, {"runs": rows[:100]}
    api.routes["/api/runs"] = runs
    async with dashboard(api) as page:
        await page.wait_for_function("document.querySelectorAll('#history-list .row').length === 100")
        assert "old active" in await page.locator("#active-list").inner_text()
        await page.locator("#history-more").click()
        await page.wait_for_function("document.querySelectorAll('#history-list .row').length === 105")
        assert await page.locator("#history-more").is_hidden()
        await page.locator("#history-project").fill("selected")
        await page.locator("#history-filter").evaluate("form => form.requestSubmit()")
        await page.wait_for_function("document.querySelectorAll('#history-list .row').length === 52")
        assert "other" not in await page.locator("#history-list").inner_text()
        assert "old active" in await page.locator("#active-list").inner_text()
