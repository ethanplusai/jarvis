import time
import pytest
from tests.dashboard_page import dashboard, quiet_machine, why_unavailable

UNAVAILABLE = why_unavailable()
pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")]


async def test_business_approval_requires_review_and_renders_hostile_text_safely(tmp_path):
    api = quiet_machine()
    api.json("/api/business/connections", {"providers": {"twilio": {"configured": False, "missing": ["TWILIO_AUTH_TOKEN"], "issue": None}}})
    item = {"id": "draft", "seq": 1, "provider": "twilio", "operation": "call", "state": "pending", "digest": "a" * 64,
            "expires": time.time() + 86400, "payload": {"message": "<img src=x onerror=alert(1)>"}, "result": {}}
    api.json("/api/business/actions", {"items": [item]})
    decisions = []
    async def setup(page):
        async def decide(route):
            decisions.append(route.request.post_data_json)
            item["state"] = "submitted"
            await route.fulfill(json=item)
        await page.route("**/api/business/actions/draft/decision", decide)
    async with dashboard(api, setup) as page:
        await page.get_by_role("button", name="Business desk").click()
        approve = page.get_by_role("button", name="Approve & send")
        await approve.wait_for()
        assert await approve.is_disabled()
        assert await page.locator("#business img").count() == 0
        assert await page.get_by_role("button", name="Check connection & report").is_disabled()
        await page.get_by_role("checkbox", name="I reviewed this request and authorize it").check()
        await approve.click()
        await page.get_by_text("Receipt recorded.", exact=False).wait_for()
        assert decisions == [{"digest": "a" * 64, "approve": True}]
        await page.set_viewport_size({"width": 390, "height": 844})
        bounds = await page.locator("#business").bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        await page.screenshot(path=str(tmp_path / "business-mobile.png"))


async def test_business_local_record_edit_preserves_revision():
    api = quiet_machine()
    api.json("/api/business/connections", {"providers": {}})
    api.json("/api/business/actions", {"items": []})
    api.json("/api/business/records/task", {"items": [{"id": "task1", "seq": 1, "version": 4, "kind": "task",
             "body": {"title": "Follow up", "notes": "Call customer", "contact": "Ada", "status": "open", "due": "", "amount_minor": 0, "currency": "USD"}}]})
    writes = []
    async def setup(page):
        async def save(route):
            writes.append(route.request.post_data_json)
            await route.fulfill(json={"ok": True})
        await page.route("**/api/business/records", save)
    async with dashboard(api, setup) as page:
        await page.get_by_role("button", name="Business desk").click()
        await page.get_by_role("button", name="Tasks", exact=True).click()
        await page.get_by_role("button", name="Edit", exact=True).click()
        await page.get_by_label("Status", exact=True).select_option("done")
        await page.get_by_role("button", name="Save changes").click()
        await page.get_by_text("Saved locally.", exact=False).wait_for()
        assert writes[0]["id"] == "task1" and writes[0]["version"] == 4
        assert writes[0]["status"] == "done"


async def test_delayed_connection_response_does_not_replace_current_view():
    import asyncio
    api = quiet_machine()
    api.json("/api/business/actions", {"items": []})
    api.json("/api/business/records/task", {"items": []})
    release = asyncio.Event()
    async def setup(page):
        async def connection(route):
            await release.wait()
            await route.fulfill(json={"providers": {}})
        await page.route("**/api/business/connections", connection)
    async with dashboard(api, setup) as page:
        await page.get_by_role("button", name="Business desk").click()
        await page.get_by_role("button", name="Tasks", exact=True).click()
        await page.get_by_label("Title / name", exact=True).fill("Unsaved work")
        release.set()
        await page.get_by_role("heading", name="Connections", exact=True).wait_for()
        assert await page.get_by_label("Title / name", exact=True).input_value() == "Unsaved work"
