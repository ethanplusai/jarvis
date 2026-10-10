import json
import pytest
from tests.dashboard_page import dashboard, quiet_machine, why_unavailable
from tests.test_settings_page import no_gpu

UNAVAILABLE = why_unavailable()
pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")]


async def test_typed_receipt_and_offline_outbox_survive_reload(tmp_path):
    api = quiet_machine()
    api.json("/api/settings/status", {"claude_code_installed": True,
             "server_port": 8340, "uptime_seconds": 1,
             "env_keys_set": {"fish_audio": True, "fish_voice_id": True, "user_name": "Ada"}})
    api.json("/api/settings/preferences", {"user_name": "Ada", "honorific": "none"})
    api.json("/api/conversation", {"messages": []})
    wires, received = [], []
    async def setup(page):
        await no_gpu(page)
        async def connected(ws):
            wires.append(ws)
            ws.on_message(lambda message: received.append(json.loads(message)))
        await page.route_web_socket("**/ws/voice", connected)
    async with dashboard(api, setup, entry="index.html") as page:
        await page.get_by_text("Connected", exact=True).wait_for()
        await page.locator("#message-input").fill("<b>typed safely</b>")
        await page.locator("#composer button").click()
        await page.wait_for_function("document.querySelector('#conversation-log').textContent.includes('Awaiting receipt')")
        sent = next(m for m in received if m.get("type") == "transcript")
        assert sent["source"] == "typed"
        assert await page.locator("#conversation-log b").count() == 0
        wires[0].send(json.dumps({"type": "receipt", "id": sent["id"], "status": "accepted"}))
        await page.get_by_text("Received", exact=True).wait_for()
        assert await page.evaluate("JSON.parse(localStorage.getItem('jarvis-unsent-messages-v1')).length") == 0
        await wires[0].close()
        await page.wait_for_function("document.querySelector('#voice-connection').textContent.includes('Reconnecting')")
        await page.locator("#message-input").fill("offline draft")
        await page.locator("#composer button").click()
        await page.get_by_text("Not sent — disconnected", exact=True).wait_for()
        before = len([m for m in received if m.get("type") == "transcript"])
        await page.reload()
        await page.get_by_text("offline draft", exact=True).wait_for()
        assert len([m for m in received if m.get("type") == "transcript"]) == before
        assert await page.get_by_role("button", name="Load earlier messages").is_hidden()
        await page.screenshot(path=str(tmp_path / "conversation-desktop.png"))
        await page.set_viewport_size({"width": 390, "height": 844})
        bounds = await page.locator("#composer button").bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 844
        await page.screenshot(path=str(tmp_path / "conversation-mobile.png"))
