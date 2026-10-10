"""Deleting one message from the Conversation panel.

Soft, like the desk's approvals, for a different reason: the `conversation`
table is also how a typed message is accepted exactly once — `accept()` is
INSERT OR IGNORE on the client's id — so a hard delete would let a retried
draft ("Receipt unknown — check before sending again") run a second time.
A deleted row keeps its id, gains a `deleted` stamp, leaves history and
never comes back; a receipt for it can still be answered.

On the page: every saved message gets a two-click Delete (arm, then act),
an unsent draft is removed locally with no request, and his live reply has
none — it is replaced by its record within seconds.
"""

import importlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.dashboard_page import dashboard, quiet_machine, why_unavailable
from tests.test_settings_page import no_gpu

ROOT = Path(__file__).parent.parent
UNAVAILABLE = why_unavailable()
_browser = pytest.mark.skipif(UNAVAILABLE is not None, reason=UNAVAILABLE or "")
WAIT_MS = 5_000


# --- the store ------------------------------------------------------------------

@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import conversation_store as cs
    cs.init_db()
    return cs


def test_a_deleted_message_leaves_history_and_keeps_its_id(store):
    store.accept("u-1", "hello")
    store.record_assistant("Good evening, sir.", "a-1")
    before = time.time()

    assert store.delete_message("a-1") is True

    assert [m["id"] for m in store.list_messages()] == ["u-1"]
    kept = store.get("a-1")
    assert kept is not None and isinstance(kept["deleted"], float) and kept["deleted"] >= before


def test_deleting_a_user_message_does_not_let_a_retry_run_again(store):
    """The whole reason this is soft."""
    first, _row = store.accept("u-1", "send the invoice")
    assert first is True
    store.delete_message("u-1")

    again, row = store.accept("u-1", "send the invoice")

    assert again is False, "the id is still claimed"
    assert row["deleted"] is not None
    assert store.list_messages() == []


def test_deleting_twice_or_something_unknown_is_reported_not_raised(store):
    store.accept("u-1", "hello")
    assert store.delete_message("u-1") is True
    assert store.delete_message("u-1") is False
    assert store.delete_message("nope") is False


def test_an_older_database_gains_the_column_on_init(store):
    from contextlib import closing
    with closing(store.connect()) as conn, conn:
        conn.execute("DROP TABLE conversation")
        conn.execute("""CREATE TABLE conversation (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE, role TEXT NOT NULL, text TEXT NOT NULL,
            status TEXT NOT NULL, created_at REAL NOT NULL)""")
    store.init_db()
    store.accept("u-1", "hello")
    assert store.delete_message("u-1") is True
    assert store.list_messages() == []


# --- the route --------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import conversation_store
    import data_paths
    import server as server_module
    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        yield client, conversation_store, data_paths


def test_the_route_deletes_and_answers_404_for_the_unknown(api):
    client, cs, data_paths = api
    cs.accept("u-1", "hello")
    token = data_paths.ensure_tool_token()

    r = client.delete("/api/conversation/u-1", headers={"Authorization": f"Bearer {token}"})

    assert r.status_code == 200 and r.json()["id"] == "u-1" and r.json()["deleted"]
    assert client.get("/api/conversation").json()["messages"] == []
    assert client.delete("/api/conversation/nope",
                         headers={"Authorization": f"Bearer {token}"}).status_code == 404
    assert client.get("/api/conversation/u-1").status_code == 200, "a receipt is still answerable"


def test_the_route_sits_behind_the_web_boundary(api):
    client, cs, _dp = api
    cs.accept("u-1", "hello")
    r = client.delete("/api/conversation/u-1")
    assert r.status_code in (401, 403)
    assert [m["id"] for m in client.get("/api/conversation").json()["messages"]] == ["u-1"]


# --- the page ---------------------------------------------------------------------

def test_the_docs_say_so():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "delete any message" in readme.lower()


def _page_api(messages):
    api = quiet_machine()
    api.json("/api/settings/status", {"claude_code_installed": True,
             "server_port": 8340, "uptime_seconds": 1,
             "env_keys_set": {"fish_audio": True, "fish_voice_id": True, "user_name": "Ada"}})
    api.json("/api/settings/preferences", {"user_name": "Ada", "honorific": "none"})
    api.json("/api/conversation", {"messages": messages})
    return api


def _row(seq, role, text):
    return {"id": f"m{seq}", "seq": seq, "role": role, "text": text,
            "status": "delivered" if role == "assistant" else "accepted",
            "created_at": time.time() - 60 + seq}


@_browser
@pytest.mark.asyncio
async def test_a_saved_message_is_deleted_in_two_clicks():
    messages = [_row(1, "user", "hello"), _row(2, "assistant", "Good evening, sir.")]
    api = _page_api(messages)
    deleted = []

    async def setup(page):
        await no_gpu(page)
        await page.route_web_socket("**/ws/voice", lambda ws: None)
        async def on_delete(route):
            if route.request.method != "DELETE":
                await route.continue_()
                return
            deleted.append(route.request.url.rsplit("/", 1)[-1])
            messages[:] = [m for m in messages if m["id"] != "m2"]
            await route.fulfill(json={"id": "m2", "deleted": time.time()})
        await page.route("**/api/conversation/m2", on_delete)

    async with dashboard(api, setup, entry="index.html") as page:
        await page.get_by_text("Good evening, sir.", exact=True).wait_for(timeout=WAIT_MS)
        buttons = page.locator("#conversation-log button", has_text="Delete")
        assert await buttons.count() == 2, "every saved message has one"

        await buttons.nth(1).click()
        assert deleted == [], "the first click arms; nothing is sent"
        await page.locator("#conversation-log button", has_text="Confirm").click()
        await page.wait_for_function(
            "!document.querySelector('#conversation-log').textContent.includes('Good evening, sir.')",
            timeout=WAIT_MS)

        assert deleted == ["m2"]
        assert await page.get_by_text("hello", exact=True).count() == 1, "the other message is untouched"


@_browser
@pytest.mark.asyncio
async def test_an_unsent_draft_is_removed_locally_with_no_request():
    api = _page_api([])
    requests = []

    async def setup(page):
        await no_gpu(page)
        await page.route_web_socket("**/ws/voice", lambda ws: None)
        async def watch(route):
            if route.request.method == "DELETE":
                requests.append(route.request.url)
            await route.continue_()
        await page.route("**/api/conversation/**", watch)

    async with dashboard(api, setup, entry="index.html") as page:
        await page.locator("#message-input").fill("a draft")
        await page.locator("#composer button").click()
        await page.get_by_text("a draft", exact=True).wait_for(timeout=WAIT_MS)
        assert await page.evaluate("JSON.parse(localStorage.getItem('jarvis-unsent-messages-v1')).length") == 1

        await page.locator("#conversation-log button", has_text="Delete").click()
        await page.locator("#conversation-log button", has_text="Confirm").click()
        await page.wait_for_function(
            "!document.querySelector('#conversation-log').textContent.includes('a draft')",
            timeout=WAIT_MS)

        assert requests == [], "an unsent draft never reached the server; deleting it does not either"
        assert await page.evaluate("JSON.parse(localStorage.getItem('jarvis-unsent-messages-v1')).length") == 0
