"""Hold-to-talk by default; an open microphone by choice.

Measured on 2026-09-24: with the microphone always open, JARVIS transcribed
another assistant's spoken report from the same room, took it as a barge-in
mid-sentence, and ran a full turn on it. Echo suppression knows his own
voice and nothing else's. So listening is now a mode: `hold` (the default —
hold Space outside a text box and speak) or `open` (as before), chosen in
Settings and remembered per browser. M mutes the microphone, V mutes his
voice, Esc still stops him; none of them fire while you are typing.

The decisions are pure in `frontend/src/listenmode.ts` (node:test); this is
the wiring, pinned in the source, and the assembled page in a real browser.
"""

from pathlib import Path

import pytest

from tests.dashboard_page import dashboard, quiet_machine, why_unavailable
from tests.test_settings_page import no_gpu

ROOT = Path(__file__).parent.parent
UNAVAILABLE = why_unavailable()
_browser = pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")
WAIT_MS = 5_000


# --- the wiring ---------------------------------------------------------------

def _main() -> str:
    return (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8")


def test_the_page_uses_the_pure_module_for_every_decision():
    main = _main()
    assert 'from "./listenmode"' in main
    for name in ("loadListenMode(", "shortcutFor(", "isInteractiveTarget(", "hintFor(", "TALK_TAIL_MS"):
        assert name in main, name


def test_the_rest_state_is_one_function_not_four_ternaries():
    main = _main()
    assert "function restState(" in main
    assert 'isMuted ? "idle" : "listening"' not in main, "every site asks restState()"


def test_keyup_is_wired_or_the_key_never_comes_back_up():
    main = _main()
    assert 'addEventListener("keyup"' in main


def test_the_setting_lives_in_the_settings_dialog_and_reaches_the_page_live():
    settings = (ROOT / "frontend/src/settings.ts").read_text(encoding="utf-8")
    assert 'id="input-listen-mode"' in settings
    assert 'value="hold"' in settings and 'value="open"' in settings
    assert "saveListenMode(" in settings
    assert "jarvis-listen-mode" in settings and "jarvis-listen-mode" in _main(), \
        "the dialog announces the change; the page applies it without a reload"


def test_it_is_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "hold Space" in readme
    skill = (ROOT / "skills/jarvis-setup/SKILL.md").read_text(encoding="utf-8")
    assert "hold-to-talk" in skill.lower() or "hold Space" in skill


# --- the page -------------------------------------------------------------------

def _api():
    api = quiet_machine()
    api.json("/api/settings/status", {"claude_code_installed": True, "server_port": 8340,
             "uptime_seconds": 1, "env_keys_set": {"fish_audio": True, "fish_voice_id": True, "user_name": "Ada"}})
    api.json("/api/settings/preferences", {"user_name": "Ada", "honorific": "none"})
    api.json("/api/conversation", {"messages": []})
    return api


async def _setup(page):
    await no_gpu(page)
    await page.route_web_socket("**/ws/voice", lambda ws: None)


async def _status(page) -> str:
    return (await page.locator("#status-text").inner_text()).strip()


@_browser
@pytest.mark.asyncio
async def test_by_default_he_listens_only_while_space_is_held():
    async with dashboard(_api(), _setup, entry="index.html") as page:
        await page.clock.run_for(1500)                       # the mic claim settles
        await page.wait_for_function(
            "document.querySelector('#status-text').textContent.includes('hold Space')", timeout=WAIT_MS)

        await page.keyboard.down(" ")
        await page.wait_for_function(
            "document.querySelector('#status-text').textContent.includes('listening')", timeout=WAIT_MS)

        await page.keyboard.up(" ")
        await page.clock.run_for(2500)                       # the tail after the key
        await page.wait_for_function(
            "document.querySelector('#status-text').textContent.includes('hold Space')", timeout=WAIT_MS)


@_browser
@pytest.mark.asyncio
async def test_m_and_v_toggle_the_two_buttons_but_not_while_typing():
    async with dashboard(_api(), _setup, entry="index.html") as page:
        await page.clock.run_for(1500)
        mic = page.locator("#btn-mute")
        voice = page.locator("#btn-voice")

        await page.keyboard.press("m")
        assert "muted" in (await mic.get_attribute("class") or "")
        assert "muted" in (await _status(page)).lower()
        await page.keyboard.press("m")
        assert "muted" not in (await mic.get_attribute("class") or "")

        await page.keyboard.press("v")
        assert "muted" in (await voice.get_attribute("class") or "")
        await page.keyboard.press("v")
        assert "muted" not in (await voice.get_attribute("class") or "")

        await page.locator("#message-input").focus()
        await page.keyboard.type("m v ")
        assert "muted" not in (await mic.get_attribute("class") or ""), "typing is typing"
        assert "muted" not in (await voice.get_attribute("class") or "")
        assert await page.locator("#message-input").input_value() == "m v "


@_browser
@pytest.mark.asyncio
async def test_the_open_microphone_is_a_setting_that_takes_effect_at_once_and_is_remembered():
    async with dashboard(_api(), _setup, entry="index.html") as page:
        await page.clock.run_for(1500)
        await page.locator("#btn-menu").click()
        await page.locator("#btn-settings").click()
        select = page.locator("#input-listen-mode")
        await select.wait_for(timeout=WAIT_MS)
        assert await select.input_value() == "hold"

        await select.select_option("open")
        await page.wait_for_function(
            "document.querySelector('#status-text').textContent.includes('listening')", timeout=WAIT_MS)

        await page.reload()
        await page.clock.run_for(1500)
        await page.wait_for_function(
            "document.querySelector('#status-text').textContent.includes('listening')", timeout=WAIT_MS)
        assert await page.evaluate("localStorage.getItem('jarvis-listen-mode-v1')") == "open"


@_browser
@pytest.mark.asyncio
async def test_a_choice_made_before_the_settings_finish_loading_still_takes_effect():
    """The dialog's select is on the page before its status fetch returns.

    Seen on the macOS runner, never locally: the choice was made in that
    window, the handler was not wired until after the fetch, and the change
    was lost. The fetch is held open here so the window is there every time.
    """
    async with dashboard(_api(), _setup, entry="index.html") as page:
        await page.clock.run_for(1500)
        held = []
        await page.route("**/api/settings/status", lambda route: held.append(route))
        await page.locator("#btn-menu").click()
        await page.locator("#btn-settings").click()
        select = page.locator("#input-listen-mode")
        await select.wait_for(timeout=WAIT_MS)
        assert await select.input_value() == "hold"

        await select.select_option("open")
        await page.wait_for_function(
            "document.querySelector('#status-text').textContent.includes('listening')", timeout=WAIT_MS)
        assert await page.evaluate("localStorage.getItem('jarvis-listen-mode-v1')") == "open"
        for route in held:
            await route.continue_()
