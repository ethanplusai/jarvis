"""Onboarding must persist entered values before claiming it is complete."""
import pytest
from tests.dashboard_page import dashboard, quiet_machine, why_unavailable

UNAVAILABLE = why_unavailable()
pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")]


def setup_api():
    api = quiet_machine()
    api.json("/api/settings/status", {"claude_code_installed": True,
             "server_port": 8340, "uptime_seconds": 1,
             "env_keys_set": {"fish_audio": False, "fish_voice_id": False, "user_name": ""}})
    api.json("/api/settings/preferences", {"user_name": "", "honorific": "sir"})
    return api


async def no_gpu(page):
    # Exercise the actual voice-page bundle on machines with WebGL disabled;
    # these assertions concern setup, not the hardware renderer.
    await page.add_init_script("""// Headless Chromium exposes speech APIs without their native service.
        window.SpeechRecognition = undefined;
        window.webkitSpeechRecognition = undefined;
        const getContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function(type, ...args) {
            return type.startsWith('webgl') ? null : getContext.call(this, type, ...args);
        };""")


async def test_setup_saves_each_step_and_only_closes_after_success():
    api = setup_api()
    saved = []

    async def setup(page):
        await no_gpu(page)
        async def save(route):
            if route.request.method == "POST":
                saved.append(route.request.post_data_json)
                await route.fulfill(json={"success": True})
            else:
                await route.continue_()
        await page.route("**/api/settings/keys", save)
        await page.route("**/api/settings/preferences", save)

    async with dashboard(api, setup, entry="index.html") as page:
        await page.locator("#input-fish-key").fill("test-key")
        await page.locator("#btn-setup-next").click()
        await page.locator("#input-user-name").fill("Ada")
        await page.locator("#input-honorific").select_option("none")
        await page.locator("#btn-setup-next").click()
        await page.wait_for_function("!document.querySelector('#settings-container').classList.contains('open')")
        assert saved == [{"key_name": "FISH_API_KEY", "key_value": "test-key"},
                         {"user_name": "Ada", "honorific": "none"}]


async def test_rejected_key_keeps_setup_on_the_same_step():
    api = setup_api()
    api.fails("/api/settings/keys", 403)
    async with dashboard(api, no_gpu, entry="index.html") as page:
        await page.locator("#input-fish-key").fill("test-key")
        await page.locator("#btn-setup-next").click()
        await page.wait_for_function("document.querySelector('#settings-feedback').textContent.includes('403')")
        assert await page.locator("#input-fish-key").is_visible()
        assert await page.locator("#input-fish-key").input_value() == "test-key"
        assert await page.locator("#btn-setup-next").is_enabled()


async def test_fish_test_explains_a_billing_failure():
    api = setup_api()
    api.json("/api/settings/test-fish", {"valid": False, "error": "Key accepted, but the account has no credit."})
    async with dashboard(api, no_gpu, entry="index.html") as page:
        await page.locator("#btn-test-fish").click()
        await page.wait_for_function("document.querySelector('#settings-feedback').textContent.includes('no credit')")
        assert "status-red" in await page.locator("#status-fish").get_attribute("class")
