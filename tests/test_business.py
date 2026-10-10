"""Failure-oriented contracts; no requests to real provider accounts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

import business_api as api
import business_providers as providers
import business_store as store


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    for keys in providers.REQUIRED.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "private-test-credential")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550006")
    monkeypatch.setenv("JARVIS_OWNER_PHONE", "+15005550007")
    store.init_db()
    monkeypatch.setattr(api, "_closing", False)


def proposal():
    return api.propose(api.Proposal(provider="twilio", operation="call", payload={"message": "Hello <user> & team"}))


@pytest.mark.asyncio
async def test_concurrent_approval_sends_exactly_once(monkeypatch):
    action = proposal(); sent = []
    async def perform(*args, **kwargs):
        sent.append(args)
        await asyncio.sleep(.01)
        return {"sid": "CA" + "2" * 32, "status": "queued"}
    monkeypatch.setattr(providers, "perform", perform)
    results = await asyncio.gather(*(api.execute(action["id"], action["digest"]) for _ in range(8)), return_exceptions=True)
    assert len(sent) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 7
    assert store.get_action(action["id"])["state"] == "submitted"
    assert [row["event"] for row in store.audit(action["id"])] == ["proposed", "executing", "submitted"]


def test_cross_thread_claim_and_crash_recovery():
    action = proposal()
    def claim():
        try:
            store.transition(action["id"], action["digest"], "pending", "executing")
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _: claim(), range(8))) == 1
    store.recover_interrupted()
    assert store.get_action(action["id"])["state"] == "unknown"
    assert claim() is False


@pytest.mark.asyncio
async def test_ambiguous_timeout_is_never_retried(monkeypatch):
    action = proposal()
    async def fail(*args, **kwargs):
        raise providers.ProviderError("Connection failed", uncertain=True)
    monkeypatch.setattr(providers, "perform", fail)
    assert (await api.execute(action["id"], action["digest"]))["state"] == "unknown"
    with pytest.raises(ValueError):
        await api.execute(action["id"], action["digest"])


@pytest.mark.asyncio
async def test_cancel_marks_unknown(monkeypatch):
    action = proposal()
    async def fail(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(providers, "perform", fail)
    with pytest.raises(asyncio.CancelledError):
        await api.execute(action["id"], action["digest"])
    assert store.get_action(action["id"])["state"] == "unknown"


@pytest.mark.asyncio
async def test_changed_owner_invalidates_approval(monkeypatch):
    action = proposal()
    monkeypatch.setenv("JARVIS_OWNER_PHONE", "+15005550008")
    with pytest.raises(ValueError, match="destination changed"):
        await api.execute(action["id"], action["digest"])
    assert store.get_action(action["id"])["state"] == "pending"


def test_bearer_cannot_approve_and_stale_digest_refused(monkeypatch):
    app = FastAPI(); app.include_router(api.router)
    action = proposal()
    monkeypatch.setattr(api.web_auth, "origin_allowed", lambda origin: origin == "http://localhost:8340")
    with TestClient(app) as client:
        path = f"/api/business/actions/{action['id']}/decision"
        body = {"digest": action["digest"], "approve": False}
        assert client.post(path, json=body).status_code == 403
        assert client.post(path, json=body, headers={"Authorization": "Bearer tool", "Origin": "http://localhost:8340"}).status_code == 403
        body["digest"] = "0" * 64
        assert client.post(path, json=body, headers={"Origin": "http://localhost:8340"}).status_code == 409
        body["digest"] = action["digest"]
        assert client.post(path, json=body, headers={"Origin": "http://localhost:8340"}).json()["state"] == "rejected"


def test_expired_approval_and_record_conflict(monkeypatch):
    action = proposal()
    monkeypatch.setattr(store.time, "time", lambda: action["expires"] + 1)
    with pytest.raises(ValueError, match="expired"):
        store.transition(action["id"], action["digest"], "pending", "executing")
    model = api.Record(kind="task", title="Follow up")
    item = api.save_record(model)
    updated = api.save_record(model.model_copy(update={"id": item["id"], "version": 1, "status": "done"}))
    assert updated["version"] == 2
    with pytest.raises(ValueError, match="changed"):
        api.save_record(model.model_copy(update={"id": item["id"], "version": 1}))


@pytest.mark.parametrize("provider,operation,payload", [
    ("twilio", "call", {"message": "Hello", "to": "+15005550008"}),
    ("twilio", "call", {"message": "Hello", "time_limit": True}),
    ("twilio", "call", {"message": "Hello", "time_limit": 301}),
    ("twilio", "call", {"message": "\x00"}),
    ("google", "mutate", {"mutateOperations": [{"customerOperation": {"remove": "all"}}]}),
    ("google", "mutate", {"mutateOperations": [], "partialFailure": True}),
    ("meta", "campaigns/../../me", {"name": "bad"}),
    ("meta", "campaigns", {"access_token": "secret"}),
    ("chatgpt", "campaigns/id?url=evil", {"name": "bad"}),
])
def test_invalid_payloads(provider, operation, payload):
    with pytest.raises(ValueError):
        providers.validate(provider, operation, payload)


@pytest.mark.asyncio
async def test_twilio_escapes_speech_and_never_uses_payload_destination(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(201, json={"sid": "CA" + "2" * 32})
    factory = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs))
    await providers.perform("twilio", "call", {"message": "Hello </Say><Dial>attacker</Dial> & team"})
    from urllib.parse import parse_qs
    body = parse_qs(requests[0].content.decode())
    assert body["To"] == ["+15005550007"]
    assert "&lt;/Say&gt;" in body["Twiml"][0]
    assert "<Dial>" not in body["Twiml"][0]
    assert body["TimeLimit"] == ["120"]
    assert requests[0].url.host == "api.twilio.com"


@pytest.mark.asyncio
async def test_google_refresh_and_atomic_mutation(monkeypatch):
    for key in providers.REQUIRED["google"]:
        monkeypatch.setenv(key, "test-value")
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "1234567890")
    monkeypatch.delenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", raising=False)
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"access_token": "temporary"} if "oauth2" in request.url.host else {"mutateOperationResponses": []})
    factory = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs))
    await providers.perform("google", "mutate", {"mutateOperations": [{"campaignOperation": {"update": {"status": "PAUSED"}}}]})
    assert len(seen) == 2
    assert seen[1].headers["authorization"] == "Bearer temporary"
    assert json.loads(seen[1].content)["partialFailure"] is False


def test_reports_strip_secret_paging_urls_and_values():
    result = api.public_result({"paging": {"next": "https://example/?access_token=secret"}, "name": "private-test-credential", "access_token": "secret", "id": "123"})
    assert result == {"name": "[redacted]", "id": "123"}


def test_large_history_cursor_and_export():
    for index in range(151):
        store.save_record("task", {"title": str(index)})
    first = store.list_records("task")
    second = store.list_records("task", first[-1]["seq"])
    assert len(first) == 100 and len(second) == 51
    assert len({row["id"] for row in first + second}) == 151
    assert len(list(store.export_rows())) == 151


def test_capabilities_are_configuration_not_claimed_verification():
    result = providers.capabilities()
    assert result["twilio"]["configured"] is True
    assert result["twilio"]["verified"] is False
    assert result["google"]["configured"] is False
    assert "private-test-credential" not in str(result)


def test_restoring_backup_invalidates_old_approvals(tmp_path):
    import maintenance
    import run_store
    run_store.init_db()
    action = proposal()
    archive = tmp_path.parent / (tmp_path.name + "-business.zip")
    maintenance.backup(archive)
    store.transition(action["id"], action["digest"], "pending", "executing")
    store.transition(action["id"], action["digest"], "executing", "submitted")
    maintenance.restore(archive)
    assert store.get_action(action["id"])["state"] == "unknown"
    assert store.audit(action["id"])[-1]["event"] == "restore_invalidated"


def test_restoring_backup_disarms_an_approved_connector_call(monkeypatch, tmp_path):
    """One approval, one send — across a restore too.

    A connector action RESTS in 'approved': the desk arms it and the
    PreToolUse gate spends it (approved -> submitted) on the next identical
    call. A backup taken while it was armed brought it back armed, so a post
    that had already gone out went out a second time with nobody asked.
    """
    import importlib
    import data_paths
    import maintenance
    import run_store
    import server
    import tool_log
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    for module in (data_paths, run_store, store, tool_log, server):
        importlib.reload(module)
    monkeypatch.setattr(server, "GATE_APPROVAL_WAIT_SEC", 0.05)
    run_store.init_db(); store.init_db(); tool_log.init_db()

    class UserTurn:
        current_origin = "user"
        async def stop(self):
            pass

    def ask():
        token = data_paths.ensure_tool_token()
        with TestClient(server.app) as client:
            server.brain_instance = UserTurn()
            body = client.post("/internal/pretool", headers={"Authorization": f"Bearer {token}"},
                               json={"tool_name": "mcp__linkedin__create_post",
                                     "tool_input": {"text": "the launch post"}, "tool_use_id": "t"}).json()
        return body["hookSpecificOutput"]["permissionDecision"]

    assert ask() == "deny"
    [action] = store.list_actions()
    with TestClient(server.app) as client:
        approved = client.post(f"/api/business/actions/{action['id']}/decision",
                               headers={"Origin": "http://localhost:5173"},
                               json={"digest": action["digest"], "approve": True})
    assert approved.status_code == 200, approved.text
    assert store.get_action(action["id"])["state"] == "approved"
    archive = tmp_path.parent / (tmp_path.name + "-armed.zip")
    maintenance.backup(archive)
    assert ask() == "allow"
    assert store.get_action(action["id"])["state"] == "submitted"
    maintenance.restore(archive)
    assert ask() == "deny", "a restore re-armed an approval that was already spent"
    assert store.get_action(action["id"])["state"] == "unknown"
    assert store.audit(action["id"])[-1]["event"] == "restore_invalidated"
    [fresh] = [a for a in store.list_actions() if a["state"] == "pending"]
    assert fresh["id"] != action["id"], "the call must be put to the user afresh"


def test_provider_credentials_never_inherited_by_brain_or_runs():
    import claude_env
    source = {"PATH": "path", "GOOGLE_ADS_REFRESH_TOKEN": "private", "META_ACCESS_TOKEN": "private",
              "TWILIO_AUTH_TOKEN": "private", "OPENAI_ADS_API_KEY": "private", "JARVIS_OWNER_PHONE": "private"}
    assert claude_env.child_env(source) == {"PATH": "path"}


def test_briefing_keeps_currency_totals_separate():
    for currency, amount in (("USD", 100), ("AED", 200), ("USD", 300)):
        api.save_record(api.Record(kind="invoice", title="Invoice", status="sent", currency=currency, amount_minor=amount))
    api.save_record(api.Record(kind="task", title="Late", due="2020-01-01"))
    result = store.briefing()
    assert result["receivables_minor"] == {"USD": 400, "AED": 200}
    assert result["overdue_tasks"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,operation,expected_host,expected_path", [
    ("meta", "campaigns", "graph.facebook.com", "/v25.0/act_123/campaigns"),
    ("meta", "campaigns/456", "graph.facebook.com", "/v25.0/456"),
    ("chatgpt", "campaigns", "api.ads.openai.com", "/v1/campaigns"),
    ("chatgpt", "campaigns/cmpn_123", "api.ads.openai.com", "/v1/campaigns/cmpn_123"),
])
async def test_meta_and_chatgpt_use_documented_post_contract(monkeypatch, provider, operation, expected_host, expected_path):
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123")
    monkeypatch.setenv("META_API_VERSION", "v25.0")
    monkeypatch.setenv("META_ACCESS_TOKEN", "meta-test")
    monkeypatch.setenv("OPENAI_ADS_API_KEY", "openai-ads-test")
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"account_id": "123"} if request.method == "GET" else {"id": "456"})
    factory = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs))
    await providers.perform(provider, operation, {"name": "Campaign", "status": "PAUSED" if provider == "meta" else "paused"})
    assert len(seen) == (2 if provider == "meta" and "/" in operation else 1)
    assert seen[-1].method == "POST"
    assert seen[-1].url.host == expected_host and seen[-1].url.path == expected_path
    assert "access_token" not in str(seen[-1].url)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,uncertain", [(400, False), (401, False), (429, False), (500, True), (408, True), (302, False)])
async def test_provider_errors_are_redacted_and_never_follow_redirects(code, uncertain):
    count = 0
    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(code, text="secret token private-test-credential", headers={"Location": "https://attacker.example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        with pytest.raises(providers.ProviderError) as caught:
            await providers._request(client, "POST", "https://api.twilio.com/test")
    assert caught.value.uncertain is uncertain
    assert "secret" not in str(caught.value) and count == 1


@pytest.mark.asyncio
async def test_oversized_or_invalid_success_receipt_is_unknown():
    for content in (b"x" * 2_000_001, b"not JSON"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))) as client:
            with pytest.raises(providers.ProviderError) as caught:
                await providers._request(client, "POST", "https://api.twilio.com/test")
            assert caught.value.uncertain


@pytest.mark.asyncio
async def test_shutdown_drains_provider_work_before_releasing_runtime(monkeypatch):
    action = proposal()
    started = asyncio.Event()
    async def perform(*args):
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(providers, "perform", perform)
    task = asyncio.create_task(api.execute(action["id"], action["digest"]))
    await started.wait()
    await api.shutdown()
    assert task.done() and not api._inflight
    assert store.get_action(action["id"])["state"] == "unknown"
    with pytest.raises(ValueError, match="shutting down"):
        await api.execute(action["id"], action["digest"])


@pytest.mark.asyncio
async def test_cannot_cancel_another_recipient_call(monkeypatch):
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"to": "+15005550008"})
    factory = httpx.AsyncClient
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kwargs: factory(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(ValueError, match="owner"):
        await providers.perform("twilio", "cancel", {"call_sid": "CA" + "2" * 32})
    assert len(seen) == 1 and seen[0].method == "GET"


@pytest.mark.asyncio
async def test_the_routes_still_work_as_plain_functions():
    # The brain's business_status tool used to call the route functions
    # directly, outside FastAPI. A default written as `before: int =
    # Query(...)` is then the Query object itself, and SQLite refused it:
    # measured live 2026-09-23, "Error binding parameter 1: type 'Query' is
    # not supported" on every call, while the HTTP routes worked. The tool
    # reads the store now; the routes must still take no-argument calls.
    action = proposal()
    store.save_record("task", {"title": "Follow up", "status": "open", "due": None})
    assert [item["id"] for item in api.actions()["items"]] == [action["id"]]
    assert len(api.records("task")["items"]) == 1
    status = json.loads(await api.tool_business_status({}))
    assert [card["id"] for card in status["live_approvals"]["cards"]] == [action["id"]]
    assert len(status["records"]["task"]["items"]) == 1
    assert status["records"]["invoice"] == {"count": 0}
    page = json.loads(await api.tool_business_status({"kind": "task"}))
    assert [item["title"] for item in page["items"]] == ["Follow up"]


def test_the_http_routes_still_validate_and_default_their_cursor():
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    proposal()
    assert len(client.get("/api/business/actions").json()["items"]) == 1
    assert client.get("/api/business/actions?before=0").status_code == 422
    assert client.get("/api/business/records/task?before=0").status_code == 422
    assert client.get("/api/business/records/task").json() == {"items": []}
