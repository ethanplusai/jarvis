"""JARVIS on WhatsApp: the owner's phone as a second door (whatsapp.py).

The properties held here are the ones the design rests on:

  * every send goes to the configured owner — there is no `to` anywhere;
  * a message from any other number is dropped before the brain sees it;
  * a message is acted on once, however often the poll sees it;
  * a button tap decides only the card it was made for (id AND digest), by
    the same function the desk's route uses, and the ledger says so;
  * a word like "yes" is never an approval, and "approve" with two cards
    waiting asks which rather than guessing;
  * a secret in a request is redacted on the phone as it is on the desk;
  * a shut 24-hour window falls back to the template, or says why not;
  * an announcement reaches the phone only when nobody is in the tab.

Kapso is a MockTransport (`Kapso`), so no test can message anybody; the
conftest guard is lifted for this module alone and this fake replaces the
one place a client is made.
"""
import asyncio
import importlib
import inspect
import json
import time

import httpx
import pytest

import business_api as api
import business_providers as providers
import business_store as store
import messaging
import whatsapp

OWNER = "+15005550007"
OWNER_WA = "15005550007"
STRANGER_WA = "15550001111"
PHONE_ID = "123456789012345"
KEY = "kapso-test-key-1234567890"


@pytest.fixture(autouse=True)
def line(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KAPSO_API_KEY", KEY)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", OWNER)
    for keys in providers.REQUIRED.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    for name, value in (("_client", None), ("_client_key", ()), ("_hot_until", 0.0),
                        ("_last_error", None), ("_ignored_strangers", 0), ("_poller", None),
                        ("_last_poll", None), ("_errors_in_a_row", 0)):
        monkeypatch.setattr(whatsapp, name, value)
    for name in ("_chat", "_synth", "_confirm", "_shown", "_loop", "_turn_gate"):
        monkeypatch.setattr(messaging, name, None)
    monkeypatch.setattr(messaging, "_turns", set())
    monkeypatch.setattr(messaging, "_accepting", True)
    monkeypatch.setattr(messaging, "_told_restarting", set())
    store.init_db()
    whatsapp.init_db()
    monkeypatch.setattr(api, "_closing", False)


class Kapso:
    """A stand-in for api.kapso.ai: records every request, answers as the
    proxy does, and can shut the 24-hour window or refuse an upload."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.inbox: list[dict] = []
        self.window_closed = False
        self.fail_media = False
        self.next_id = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers.get("X-API-Key") == KEY, "every call carries the key"
        path = request.url.path
        assert path.startswith(f"/meta/whatsapp/v24.0/{PHONE_ID}/"), path
        if request.method == "GET" and path.endswith("/messages"):
            return httpx.Response(200, json={"data": list(self.inbox),
                                             "paging": {"cursors": {"after": ""}}})
        if request.method == "POST" and path.endswith("/media"):
            if self.fail_media:
                return httpx.Response(400, json={"error": {"message": "bad media", "code": 100}})
            return httpx.Response(200, json={"id": "media-1"})
        if request.method == "POST" and path.endswith("/messages"):
            body = json.loads(request.content)
            if body.get("status") == "read":
                return httpx.Response(200, json={"success": True})
            if self.window_closed and body.get("type") != "template":
                return httpx.Response(400, json={"error": {
                    "message": "(#131047) Re-engagement message", "code": 131047}})
            self.next_id += 1
            return httpx.Response(200, json={
                "messaging_product": "whatsapp", "contacts": [{"wa_id": body.get("to")}],
                "messages": [{"id": f"wamid.out{self.next_id}"}]})
        return httpx.Response(404, json={"error": {"message": "no such route", "code": 803}})

    def sent(self) -> list[dict]:
        """Every message JARVIS sent, in order — read receipts left out."""
        out = []
        for request in self.requests:
            if request.method == "POST" and request.url.path.endswith("/messages"):
                body = json.loads(request.content)
                if body.get("status") != "read":
                    out.append(body)
        return out

    def reads(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests
                if r.method == "POST" and r.url.path.endswith("/messages")
                and json.loads(r.content).get("status") == "read"]

    def uploads(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/media")]


@pytest.fixture
def kapso(monkeypatch):
    fake = Kapso()

    def new_client(base_url, api_key):
        return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(fake.handle),
                                 headers={"X-API-Key": api_key})
    monkeypatch.setattr(whatsapp, "_new_client", new_client)
    return fake


def _text_from(sender: str, text: str, wamid: str, at: float | None = None) -> dict:
    return {"id": wamid, "timestamp": str(int(at or time.time())), "type": "text",
            "from": sender, "text": {"body": text}, "kapso": {"direction": "inbound"}}


def _button_from(sender: str, button_id: str, wamid: str, title: str = "Approve") -> dict:
    return {"id": wamid, "timestamp": str(int(time.time())), "type": "interactive",
            "from": sender, "interactive": {"type": "button_reply",
                                            "button_reply": {"id": button_id, "title": title}},
            "kapso": {"direction": "inbound"}}


class Chat:
    def __init__(self, reply="Still going, sir."):
        self.calls: list[tuple[str, str]] = []
        self.forwarded: list[bool] = []
        self.reply = reply

    async def __call__(self, text, wamid, line="whatsapp", forwarded=False, owner=None):
        assert line == "whatsapp", "the policy names the line the message came over"
        self.calls.append((text, wamid))
        self.forwarded.append(forwarded)
        return self.reply


# --- configuration ---------------------------------------------------------

def test_unset_is_not_configured_and_names_what_is_missing(monkeypatch):
    for key in ("KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_OWNER_NUMBER"):
        monkeypatch.delenv(key)
    monkeypatch.delenv("JARVIS_OWNER_PHONE", raising=False)
    assert not whatsapp.configured()
    assert not whatsapp.touched()
    assert whatsapp.missing() == ["KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_OWNER_NUMBER"]
    with pytest.raises(whatsapp.NotConfigured):
        whatsapp.require()


def test_the_owner_number_falls_back_to_the_one_twilio_calls(monkeypatch):
    monkeypatch.delenv("WHATSAPP_OWNER_NUMBER")
    monkeypatch.setenv("JARVIS_OWNER_PHONE", "+447700900123")
    cfg = whatsapp.config()
    assert cfg is not None
    assert cfg.owner == "+447700900123"
    assert cfg.owner_wa_id == "447700900123"


def test_a_malformed_owner_number_is_an_issue_not_a_send(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "4155550132")
    assert not whatsapp.missing()
    assert "E.164" in (whatsapp.issue() or "")
    assert not whatsapp.configured()


def test_the_wa_id_override_wins_for_countries_whose_ids_differ(monkeypatch):
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "+5215512345678")
    monkeypatch.setenv("WHATSAPP_OWNER_WA_ID", "525512345678")
    assert whatsapp.config().owner_wa_id == "525512345678"


def test_status_shows_neither_the_number_nor_the_key():
    text = json.dumps(whatsapp.status())
    assert KEY not in text
    assert OWNER_WA not in text
    assert whatsapp.status()["owner"] == "•••0007"
    assert whatsapp.status()["configured"] is True
    assert "Not available" in whatsapp.status()["calls"]


# --- sending: the owner and nobody else -------------------------------------

def test_no_sender_takes_a_recipient():
    """The invariant as a property of the API, not of one call site."""
    for fn in (whatsapp.send_text, whatsapp.deliver, whatsapp.send_buttons,
               whatsapp.send_voice_note, whatsapp.send_template, whatsapp.say,
               whatsapp.reach, whatsapp.notify_card):
        params = inspect.signature(fn).parameters
        assert "to" not in params and "recipient" not in params, fn.__name__


@pytest.mark.asyncio
async def test_a_text_goes_to_the_owner_by_the_proxy_with_the_key(kapso):
    receipt = await whatsapp.send_text("Hello, sir.")
    assert receipt["wamid"] == "wamid.out1"
    [body] = kapso.sent()
    assert body["to"] == OWNER_WA
    assert body["messaging_product"] == "whatsapp"
    assert body["type"] == "text" and body["text"]["body"] == "Hello, sir."
    assert kapso.requests[0].url.path == f"/meta/whatsapp/v24.0/{PHONE_ID}/messages"


@pytest.mark.asyncio
async def test_control_characters_never_leave(kapso):
    await whatsapp.send_text("hi\x07 there\x1b[2J")
    assert kapso.sent()[0]["text"]["body"] == "hi there[2J"


def test_chunks_cut_at_paragraphs_then_lines_then_sentences():
    text = ("A" * 3000) + "\n\n" + ("B" * 3000) + "\n\n" + ("C" * 100)
    pieces = whatsapp.chunks(text)
    assert [p[0] for p in pieces] == ["A", "B"], "the cut lands on the paragraph break"
    assert pieces[1].endswith("C" * 100), "and what still fits stays together"
    assert all(len(p) <= whatsapp.TEXT_CHUNK for p in pieces)
    lines = whatsapp.chunks(("L" * 3500) + "\n" + ("M" * 3500))
    assert [p[0] for p in lines] == ["L", "M"]
    sentences = whatsapp.chunks(("S" * 3500) + ". " + ("T" * 3500))
    assert [p[0] for p in sentences] == ["S", "T"] and sentences[0].endswith(".")
    assert whatsapp.chunks("   ") == []
    solid = whatsapp.chunks("x" * 9000)
    assert len(solid) == 3 and "".join(solid) == "x" * 9000


@pytest.mark.asyncio
async def test_a_long_message_is_split_and_numbered(kapso):
    text = ("First paragraph. " * 200) + "\n\n" + ("Second paragraph. " * 200)
    receipt = await whatsapp.deliver(text)
    sent = kapso.sent()
    assert len(sent) == 2
    assert sent[0]["text"]["body"].startswith("(1/2) First")
    assert sent[1]["text"]["body"].startswith("(2/2) Second")
    assert all(len(b["text"]["body"]) <= whatsapp.TEXT_BODY_MAX for b in sent)
    assert receipt["via"] == "text"


@pytest.mark.asyncio
async def test_a_shut_window_falls_back_to_the_template(kapso, monkeypatch):
    kapso.window_closed = True
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "jarvis_attention")
    receipt = await whatsapp.deliver("Line one.\nLine   two.")
    assert receipt["via"] == "template"
    text_try, template = kapso.sent()
    assert text_try["type"] == "text"
    assert template["type"] == "template"
    assert template["template"]["name"] == "jarvis_attention"
    assert template["template"]["language"] == {"code": "en_US"}
    [param] = template["template"]["components"][0]["parameters"]
    assert param["parameter_name"] == "message"
    assert param["text"] == "Line one. Line two.", "one line, single spaces, as Meta demands"


@pytest.mark.asyncio
async def test_a_shut_window_without_a_template_is_refused_in_words(kapso):
    kapso.window_closed = True
    with pytest.raises(whatsapp.WindowClosed) as raised:
        await whatsapp.deliver("Hello")
    assert raised.value.code == 131047
    assert await whatsapp.reach("Hello") is False
    assert "131047" in (whatsapp.status()["last_error"] or "")


@pytest.mark.asyncio
async def test_a_provider_error_never_carries_its_whole_body(kapso, monkeypatch):
    def handle(request):
        return httpx.Response(500, json={"error": {"message": "x" * 5000, "code": 1}})
    monkeypatch.setattr(whatsapp, "_new_client", lambda base_url, api_key: httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handle), headers={"X-API-Key": api_key}))
    with pytest.raises(whatsapp.ChannelError) as raised:
        await whatsapp.send_text("Hello")
    assert len(str(raised.value)) < 300 and raised.value.status == 500


@pytest.mark.asyncio
async def test_reach_is_silent_without_a_number(kapso, monkeypatch):
    monkeypatch.delenv("KAPSO_API_KEY")
    assert await whatsapp.reach("Hello") is False
    assert kapso.requests == []


# --- approval cards ------------------------------------------------------

def _connector_card(text="hello"):
    return store.propose("connector:linkedin", "mcp__linkedin__create_post",
                         {"text": text, "confirm_post": True})


@pytest.mark.asyncio
async def test_a_staged_card_reaches_the_phone_with_buttons_bound_to_its_digest(kapso):
    action = _connector_card()
    assert await whatsapp.notify_card(action) is True
    [body] = kapso.sent()
    assert body["type"] == "interactive"
    buttons = body["interactive"]["action"]["buttons"]
    assert [b["reply"]["title"] for b in buttons] == ["Approve", "Reject"]
    assert buttons[0]["reply"]["id"] == f"ok:{action['id']}:{action['digest']}"
    assert buttons[1]["reply"]["id"] == f"no:{action['id']}:{action['digest']}"
    text = body["interactive"]["body"]["text"]
    assert len(text) <= whatsapp.INTERACTIVE_BODY_MAX
    assert "connector:linkedin" in text and "mcp__linkedin__create_post" in text
    assert action["id"][:8] in text
    assert "hello" in text, "the request is shown, redacted like the desk shows it"
    assert "holding it" in text, "a connector card says what a yes does"


@pytest.mark.asyncio
async def test_a_huge_request_still_fits_the_phone(kapso):
    action = _connector_card("z" * 20000)
    await whatsapp.notify_card(action)
    text = kapso.sent()[0]["interactive"]["body"]["text"]
    assert len(text) <= whatsapp.INTERACTIVE_BODY_MAX
    assert text.endswith("Business desk.")


@pytest.mark.asyncio
async def test_a_secret_in_the_request_is_redacted_on_the_phone(kapso, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "private-test-credential")
    action = _connector_card("the token is private-test-credential, keep it")
    await whatsapp.notify_card(action)
    text = kapso.sent()[0]["interactive"]["body"]["text"]
    assert "private-test-credential" not in text
    assert "[redacted]" in text
    assert "A secret in the request" in text


@pytest.mark.asyncio
async def test_a_decided_card_is_not_announced(kapso):
    action = _connector_card()
    store.transition(action["id"], action["digest"], "pending", "rejected")
    assert await whatsapp.notify_card(store.get_action(action["id"])) is False
    assert kapso.sent() == []


@pytest.mark.asyncio
async def test_a_shut_window_still_announces_the_card_in_words(kapso, monkeypatch):
    kapso.window_closed = True
    monkeypatch.setenv("WHATSAPP_TEMPLATE", "jarvis_attention")
    action = _connector_card()
    assert await whatsapp.notify_card(action) is True
    template = [b for b in kapso.sent() if b["type"] == "template"][-1]
    words = template["template"]["components"][0]["parameters"][0]["text"]
    assert "Approval needed" in words and action["id"][:8] in words and "approve" in words


def test_the_store_hook_fires_after_commit_and_a_broken_hook_costs_nothing(monkeypatch):
    seen = []

    def boom(action):
        raise RuntimeError("phone on fire")
    monkeypatch.setattr(store, "ON_PROPOSED", [boom, seen.append])
    action = _connector_card()
    assert seen and seen[0]["id"] == action["id"] and seen[0]["state"] == "pending"
    assert store.get_action(action["id"])["state"] == "pending"


# --- parsing what the poll returns ----------------------------------------

@pytest.mark.parametrize("item, expected", [
    (_text_from(OWNER_WA, "hi", "w1"), ("text", "hi", "")),
    (_button_from(OWNER_WA, "ok:x:y", "w2"), ("button", "Approve", "ok:x:y")),
    ({"id": "w3", "from": OWNER_WA, "timestamp": "1", "type": "button",
      "button": {"payload": "ok:x:y", "text": "Approve"}}, ("button", "Approve", "ok:x:y")),
    ({"id": "w4", "from": OWNER_WA, "timestamp": "1", "type": "interactive",
      "interactive": {"type": "list_reply", "list_reply": {"id": "row1", "title": "Row"}}},
     ("button", "Row", "row1")),
    ({"id": "w5", "from": OWNER_WA, "timestamp": "1", "type": "image", "image": {"id": "m"}},
     ("other", "", "")),
])
def test_parse_inbound_reads_each_kind(item, expected):
    parsed = whatsapp.parse_inbound(item)
    assert parsed is not None
    assert (parsed.kind, parsed.text, parsed.button_id) == expected
    assert parsed.sender == OWNER_WA


def test_parse_inbound_drops_what_is_not_an_inbound_message():
    echo = {**_text_from(OWNER_WA, "hi", "w9"), "kapso": {"direction": "outbound"}}
    assert whatsapp.parse_inbound(echo) is None
    assert whatsapp.parse_inbound({"type": "text", "text": {"body": "no id"}}) is None
    assert whatsapp.parse_inbound("not a dict") is None
    iso = {**_text_from(OWNER_WA, "hi", "w10"), "timestamp": "2026-09-26T10:00:00Z"}
    assert whatsapp.parse_inbound(iso).at > 1_700_000_000


# --- the poll: who is read, and how often -----------------------------------

@pytest.mark.asyncio
async def test_a_strangers_message_is_dropped_before_the_brain_sees_it(kapso, monkeypatch):
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    kapso.inbox = [_text_from(STRANGER_WA, "approve", "w-stranger")]
    assert await whatsapp.poll_once() == 0
    assert chat.calls == []
    assert kapso.sent() == [], "not even a reply"
    assert whatsapp.status()["ignored_strangers"] == 1
    assert await whatsapp.poll_once() == 0
    assert whatsapp.status()["ignored_strangers"] == 1, "claimed once, never re-read"


@pytest.mark.asyncio
async def test_the_owners_text_runs_a_turn_and_the_reply_goes_back(kapso, monkeypatch):
    chat = Chat("Still going, sir.")
    monkeypatch.setattr(messaging, "_chat", chat)
    kapso.inbox = [_text_from(OWNER_WA, "how's the build?", "w-owner-1")]
    assert await whatsapp.poll_once() == 1
    await messaging.wait_for_turns()
    assert chat.calls == [("how's the build?", "w-owner-1")]
    assert kapso.sent()[-1]["text"]["body"] == "Still going, sir."
    [read] = kapso.reads()
    assert read["message_id"] == "w-owner-1" and read["typing_indicator"] == {"type": "text"}
    assert whatsapp.status()["last_received"] is not None


@pytest.mark.asyncio
async def test_a_message_is_acted_on_once_however_often_it_is_seen(kapso, monkeypatch):
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    kapso.inbox = [_text_from(OWNER_WA, "hello", "w-once")]
    for _ in range(3):
        await whatsapp.poll_once()
    await messaging.wait_for_turns()
    assert len(chat.calls) == 1


@pytest.mark.asyncio
async def test_a_message_from_before_the_catch_up_window_is_left_alone(kapso, monkeypatch):
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    kapso.inbox = [_text_from(OWNER_WA, "approve", "w-old", at=time.time() - 3600)]
    assert await whatsapp.poll_once() == 0
    assert chat.calls == []
    since = kapso.requests[0].url.params["since"]
    assert since.startswith("20"), "an ISO 8601 instant, as Kapso wants"


@pytest.mark.asyncio
async def test_the_poll_asks_only_for_inbound_and_pages_on_a_cursor(kapso, monkeypatch):
    pages = [
        {"data": [_text_from(OWNER_WA, f"m{i}", f"w-page-{i}") for i in range(100)],
         "paging": {"cursors": {"after": "next"}}},
        {"data": [_text_from(OWNER_WA, "last", "w-page-last")], "paging": {}},
    ]
    gets = []

    def handle(request):
        if request.method == "GET":
            gets.append(request)
            return httpx.Response(200, json=pages[len(gets) - 1])
        return kapso.handle(request)
    monkeypatch.setattr(whatsapp, "_new_client", lambda base_url, api_key: httpx.AsyncClient(
        base_url=base_url, transport=httpx.MockTransport(handle), headers={"X-API-Key": api_key}))
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    assert await whatsapp.poll_once() == 101
    await messaging.wait_for_turns()
    assert [g.url.params.get("after") for g in gets] == [None, "next"]
    assert gets[0].url.params["direction"] == "inbound"
    assert gets[0].url.params["limit"] == "100"


@pytest.mark.asyncio
async def test_the_interval_is_quick_after_traffic_and_slow_otherwise(kapso, monkeypatch):
    cfg = whatsapp.config()
    assert whatsapp._interval(cfg) == cfg.idle_sec
    await whatsapp.send_text("hi")
    assert whatsapp._interval(cfg) == cfg.hot_sec


@pytest.mark.asyncio
async def test_something_that_is_not_text_gets_a_polite_no(kapso):
    kapso.inbox = [{"id": "w-img", "from": OWNER_WA, "timestamp": str(int(time.time())),
                    "type": "image", "image": {"id": "m"}, "kapso": {"direction": "inbound"}}]
    await whatsapp.poll_once()
    assert "only read text" in kapso.sent()[-1]["text"]["body"]


# --- decisions from the phone ---------------------------------------------

@pytest.mark.asyncio
async def test_a_button_tap_approves_a_connector_card_once_and_records_the_door(kapso):
    action = _connector_card()
    ok, _no = whatsapp.button_ids(action)
    kapso.inbox = [_button_from(OWNER_WA, ok, "w-tap-1")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "approved"
    events = [row["event"] for row in store.audit(action["id"])]
    assert events == ["proposed", "approved", "via:whatsapp"]
    assert "Allowed" in kapso.sent()[-1]["text"]["body"]
    # A second tap on the same button is a new message with the same id.
    kapso.inbox = [_button_from(OWNER_WA, ok, "w-tap-2")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "approved"
    assert events == [row["event"] for row in store.audit(action["id"])], "nothing more happened"
    assert "already been decided" in kapso.sent()[-1]["text"]["body"]


@pytest.mark.asyncio
async def test_a_button_with_another_cards_digest_decides_nothing(kapso):
    action = _connector_card()
    forged = f"ok:{action['id']}:{'0' * 64}"
    kapso.inbox = [_button_from(OWNER_WA, forged, "w-forged")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "pending"
    assert "already been decided" in kapso.sent()[-1]["text"]["body"]


@pytest.mark.asyncio
async def test_a_button_id_that_is_not_ours_is_refused(kapso):
    action = _connector_card()
    kapso.inbox = [_button_from(OWNER_WA, "ok:not-a-uuid:nope", "w-bad")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "pending"
    assert "not one of mine" in kapso.sent()[-1]["text"]["body"]


@pytest.mark.asyncio
async def test_a_provider_card_approved_from_the_phone_is_sent_by_the_server(kapso, monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "private-test-credential")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550006")
    monkeypatch.setenv("JARVIS_OWNER_PHONE", OWNER)
    performed = []

    async def perform(*args, **kwargs):
        performed.append(args)
        return {"sid": "CA" + "2" * 32, "status": "queued"}
    monkeypatch.setattr(providers, "perform", perform)
    action = api.propose(api.Proposal(provider="twilio", operation="call",
                                      payload={"message": "Hello, sir."}))
    ok, _no = whatsapp.button_ids(action)
    kapso.inbox = [_button_from(OWNER_WA, ok, "w-call")]
    await whatsapp.poll_once()
    assert len(performed) == 1
    row = store.get_action(action["id"])
    assert row["state"] == "submitted"
    assert "via:whatsapp" in [e["event"] for e in store.audit(action["id"])]
    assert "Approved and sent" in kapso.sent()[-1]["text"]["body"]


@pytest.mark.asyncio
async def test_reject_by_word_with_one_card_waiting(kapso):
    action = _connector_card()
    kapso.inbox = [_text_from(OWNER_WA, "Reject.", "w-reject")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "rejected"
    assert "Rejected" in kapso.sent()[-1]["text"]["body"]


@pytest.mark.asyncio
async def test_approve_with_two_cards_waiting_asks_which_then_takes_the_prefix(kapso):
    first, second = _connector_card("one"), _connector_card("two")
    kapso.inbox = [_text_from(OWNER_WA, "approve", "w-which")]
    await whatsapp.poll_once()
    assert store.get_action(first["id"])["state"] == "pending"
    assert store.get_action(second["id"])["state"] == "pending"
    asked = kapso.sent()[-1]["text"]["body"]
    assert first["id"][:8] in asked and second["id"][:8] in asked and "which" in asked
    kapso.inbox = [_text_from(OWNER_WA, f"approve {second['id'][:8]}", "w-second")]
    await whatsapp.poll_once()
    assert store.get_action(second["id"])["state"] == "approved"
    assert store.get_action(first["id"])["state"] == "pending"


@pytest.mark.asyncio
async def test_approve_with_nothing_waiting_says_so(kapso, monkeypatch):
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    kapso.inbox = [_text_from(OWNER_WA, "approve", "w-nothing")]
    await whatsapp.poll_once()
    assert "Nothing is waiting" in kapso.sent()[-1]["text"]["body"]
    assert chat.calls == [], "a decision word is never conversation"


@pytest.mark.asyncio
async def test_a_bare_yes_is_conversation_not_approval(kapso, monkeypatch):
    chat = Chat("Very good, sir.")
    monkeypatch.setattr(messaging, "_chat", chat)
    action = _connector_card()
    kapso.inbox = [_text_from(OWNER_WA, "yes", "w-yes")]
    await whatsapp.poll_once()
    await messaging.wait_for_turns()
    assert store.get_action(action["id"])["state"] == "pending"
    assert chat.calls == [("yes", "w-yes")]


@pytest.mark.asyncio
async def test_approvals_from_the_phone_can_be_switched_off(kapso, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APPROVALS", "0")
    action = _connector_card()
    ok, _no = whatsapp.button_ids(action)
    kapso.inbox = [_button_from(OWNER_WA, ok, "w-off"),
                   _text_from(OWNER_WA, "approve", "w-off-2")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "pending"
    assert all("switched off" in b["text"]["body"] for b in kapso.sent())


@pytest.mark.asyncio
async def test_a_lapsed_card_cannot_be_approved_by_word(kapso):
    from contextlib import closing
    action = _connector_card()
    with closing(store.connect()) as conn, conn:
        conn.execute("UPDATE business_actions SET expires=? WHERE id=?",
                     (time.time() - 1, action["id"]))
    kapso.inbox = [_text_from(OWNER_WA, f"approve {action['id'][:8]}", "w-lapsed")]
    await whatsapp.poll_once()
    assert store.get_action(action["id"])["state"] == "pending"
    assert "No card waiting" in kapso.sent()[-1]["text"]["body"]
    kapso.inbox = [_text_from(OWNER_WA, "approve", "w-lapsed-2")]
    await whatsapp.poll_once()
    assert "Nothing is waiting" in kapso.sent()[-1]["text"]["body"]


# --- "go" / "cancel" for what a turn staged ---------------------------------

@pytest.mark.asyncio
async def test_go_is_a_confirmation_when_something_waits_and_talk_otherwise(kapso, monkeypatch):
    answers = [None, "Passed to chitauri, sir."]
    asked = []

    async def confirm(go, number, line):
        assert line == "whatsapp", "a read-back is answered on the line it went to"
        asked.append((go, number))
        return answers.pop(0)
    chat = Chat("Go where, sir?")
    monkeypatch.setattr(messaging, "_confirm", confirm)
    monkeypatch.setattr(messaging, "_chat", chat)
    kapso.inbox = [_text_from(OWNER_WA, "go", "w-go-1")]
    await whatsapp.poll_once()
    await messaging.wait_for_turns()
    assert asked == [(True, None)] and chat.calls == [("go", "w-go-1")]
    kapso.inbox = [_text_from(OWNER_WA, "Go 2!", "w-go-2")]
    await whatsapp.poll_once()
    assert asked[-1] == (True, 2)
    assert kapso.sent()[-1]["text"]["body"] == "Passed to chitauri, sir."
    assert len(chat.calls) == 1


# --- voice notes -----------------------------------------------------------

def test_sniff_audio_tells_a_voice_note_from_a_file():
    assert whatsapp.sniff_audio(b"OggS\x00\x02" + b"\x00" * 20) == ("audio/ogg", True)
    assert whatsapp.sniff_audio(b"ID3\x04" + b"\x00" * 20) == ("audio/mpeg", False)
    assert whatsapp.sniff_audio(b"\xff\xfb\x90\x00" + b"\x00" * 20) == ("audio/mpeg", False)
    assert whatsapp.sniff_audio(b"RIFF\x00\x00\x00\x00WAVE") == ("audio/wav", False)


@pytest.mark.asyncio
async def test_an_opus_synthesis_becomes_a_voice_note(kapso, monkeypatch):
    async def synth(text):
        return b"OggS" + text.encode()
    monkeypatch.setattr(messaging, "_synth", synth)
    receipt = await whatsapp.say("Hello, sir.", voice=True)
    assert receipt["voice_note"] is True
    [upload] = kapso.uploads()
    assert b'name="type"' in upload.content and b"audio/ogg" in upload.content
    text, audio = kapso.sent()
    assert audio["type"] == "audio" and audio["audio"] == {"id": "media-1", "voice": True}


@pytest.mark.asyncio
async def test_an_mp3_synthesis_is_sent_as_plain_audio(kapso, monkeypatch):
    async def synth(text):
        return b"ID3" + b"\x00" * 10
    monkeypatch.setattr(messaging, "_synth", synth)
    await whatsapp.say("Hello, sir.", voice=True)
    assert b"audio/mpeg" in kapso.uploads()[0].content
    assert kapso.sent()[-1]["audio"] == {"id": "media-1"}


@pytest.mark.asyncio
async def test_a_voice_note_that_fails_never_loses_the_text(kapso, monkeypatch):
    async def synth(text):
        return b"OggS" + b"\x00" * 10
    monkeypatch.setattr(messaging, "_synth", synth)
    kapso.fail_media = True
    receipt = await whatsapp.say("Hello, sir.", voice=True)
    assert receipt["voice_note"] is False
    assert kapso.sent()[0]["text"]["body"] == "Hello, sir."


@pytest.mark.asyncio
async def test_reach_sends_a_voice_note_for_urgent_lines_unless_told_not_to(kapso, monkeypatch):
    calls = []

    async def synth(text):
        calls.append(text)
        return b"OggS" + b"\x00" * 10
    monkeypatch.setattr(messaging, "_synth", synth)
    assert await whatsapp.reach("A session needs you, sir.", voice=True) is True
    assert calls == ["A session needs you, sir."]
    monkeypatch.setenv("WHATSAPP_VOICE_NOTES", "0")
    assert await whatsapp.reach("Again.", voice=True) is True
    assert calls == ["A session needs you, sir."], "the setting is honoured"


# --- the server side -------------------------------------------------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    import conversation_store
    run_store.init_db()
    store.init_db()
    conversation_store.init_db()
    whatsapp.init_db()
    server_module.voice_clients.clear()
    server_module._pending_completions.clear()
    server_module._pending_run_completions.clear()
    server_module._phone_pending.clear()
    server_module._staged_steers.clear()
    server_module._staged_dialogs.clear()
    return server_module


@pytest.fixture
def accepting(wired, monkeypatch):
    """`wired`, with the sessions set to accept a steer outright — so a sent
    steer is "Passed to X, sir." and nothing more. Read from settings.json,
    which the suite never takes from the machine running it (conftest.py);
    the approve-it-first caveat has its own tests in test_session_inbox.py."""
    monkeypatch.setattr(wired, "_inbound_accepted", lambda: True)
    return wired


class FakeSpeech:
    def __init__(self):
        self.calls = []

    async def say(self, text, priority=None, **kwargs):
        self.calls.append(text)


class Reach:
    def __init__(self):
        self.calls = []

    async def __call__(self, line, *, voice=False):
        self.calls.append((line, voice))
        return True


def test_the_tool_is_registered_acting_and_exempt_in_every_registry(wired):
    import brain
    import jarvis_mcp
    server = wired
    assert "message_user" in server.TOOL_HANDLERS
    assert "message_user" in server.ACTING_TOOLS, "user-driven turns only"
    assert "message_user" in server.TAINT_EXEMPT_ACTING
    assert "message_user" not in server.FOREIGN_TEXT_REFUSED
    assert "message_user" in server.CHANGES_SOMETHING
    assert "message_user" in server.TAINT_EXEMPT_TOOLS
    assert server._untrusted_content_refusal("message_user", True, source="a web page") is None
    assert "message_user" in {t["name"] for t in jarvis_mcp.TOOL_SPECS}
    assert "mcp__jarvis__message_user" in brain.ALLOWED_TOOLS
    assert "whatsapp_message" not in server.TOOL_HANDLERS, "one tool for every line"


@pytest.mark.asyncio
async def test_the_tool_reports_a_receipt_or_why_nothing_went(wired, monkeypatch):
    server = wired
    sent = []

    async def say(text, *, voice=False):
        sent.append((text, voice))
        return {"sent": ["whatsapp"], "failed": {}, "via": "text", "voice_note": voice}
    monkeypatch.setattr(messaging, "say", say)
    reply = json.loads(await server.tool_message_user({"text": "Build done.", "voice": True}))
    assert reply == {"sent": True, "to": "the user's own phone", "lines": ["whatsapp"],
                     "via": "text", "voice_note": True}
    assert sent == [("Build done.", True)]
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID")
    reply = json.loads(await server.tool_message_user({"text": "Build done."}))
    assert reply["sent"] is False and "WHATSAPP_PHONE_NUMBER_ID" in reply["reason"]
    assert "TELEGRAM_BOT_TOKEN" in reply["reason"], "every line says what it still needs"
    with pytest.raises(ValueError):
        await server.tool_message_user({"text": "  "})


@pytest.mark.asyncio
async def test_a_shut_window_is_reported_to_the_brain_not_raised(wired, monkeypatch):
    server = wired

    async def say(text, *, voice=False):
        raise whatsapp.WindowClosed("shut", code=131047)
    monkeypatch.setattr(messaging, "say", say)
    reply = json.loads(await server.tool_message_user({"text": "hi"}))
    assert reply["sent"] is False and "24-hour window" in reply["reason"]


def _event(kind="needs_you"):
    return {"kind": kind, "at": 1.0, "session": {
        "session_id": "s", "voice_name": "chitauri", "project": "chitauri",
        "state": "needs_you", "needs": "permission prompt", "needs_a_human_hand": True,
        "title": "Fix the redirect", "summary": "Fix the redirect",
        "last_text": "Shall I proceed?", "steerable": True}}


@pytest.mark.asyncio
async def test_a_needs_you_reaches_the_phone_only_when_nobody_is_in_the_tab(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    reach = Reach()
    monkeypatch.setattr(messaging, "reach", reach)
    await server._announce_needs_you(_event())
    assert len(reach.calls) == 1
    line, voice = reach.calls[0]
    assert "chitauri" in line and "permission" in line and voice is True
    server.voice_clients.add(object())
    try:
        await server._announce_needs_you(_event())
    finally:
        server.voice_clients.clear()
    assert len(reach.calls) == 1, "he heard it spoken; do not text it too"


@pytest.mark.asyncio
async def test_a_phone_failure_never_reaches_the_watcher(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())

    async def explode(line, *, voice=False):
        raise RuntimeError("the line is down")
    monkeypatch.setattr(messaging, "reach", explode)
    server._on_session_event(_event())
    await asyncio.sleep(0.05)
    for task in list(server._bg_tasks):
        if task.done():
            assert task.exception() is None


@pytest.mark.asyncio
async def test_finished_work_and_failures_are_texted_when_nobody_is_listening(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", FakeSpeech())
    reach = Reach()
    monkeypatch.setattr(messaging, "reach", reach)
    server._pending_completions.append("chitauri")
    await server._announce_batch()
    assert reach.calls == [("chitauri has finished, sir.", False)]
    run = {"status": "failed", "origin": "voice", "project_name": "chitauri",
           "project_path": "C:/dev/chitauri", "prompt": "x"}
    await server._announce_run_failure(run)
    assert reach.calls[-1][1] is True, "a failure is urgent: voice note too"
    assert "failed" in reach.calls[-1][0]


class FakeBrain:
    """Enough of `Brain` for one WhatsApp turn: ready, and says one line."""

    def __init__(self, line="Still going, sir.", stage=None):
        self.ready, self.failed = True, False
        self.rotation_pending = False
        self.current_origin = None
        self.turns = []
        self.line, self.stage = line, stage

    async def turn(self, text, origin="user", on_delta=None, on_tool=None, untrusted=None):
        import brain
        self.turns.append((text, origin))
        if self.stage:
            self.stage()
        on_delta(self.line)
        return brain.TurnResult(origin=origin, text=self.line, stop_reason="result")


@pytest.mark.asyncio
async def test_the_whatsapp_turn_runs_the_brain_once_per_message(wired, monkeypatch):
    import conversation_store
    server = wired
    fake = FakeBrain()
    monkeypatch.setattr(server, "brain_instance", fake)
    reply = await server._phone_chat("how's the build?", "wamid.in1", "whatsapp")
    assert reply == "Still going, sir."
    assert len(fake.turns) == 1
    text, origin = fake.turns[0]
    assert origin == "user"
    assert text.startswith("(Over WhatsApp, from the user's phone.") and text.endswith("how's the build?")
    assert text.startswith(server.PHONE_TURN_PREFIX.format(line="WhatsApp"))
    rows = conversation_store.list_messages()
    assert [(r["role"], r["text"]) for r in rows] == [
        ("user", "how's the build?"), ("assistant", "Still going, sir.")]
    assert await server._phone_chat("how's the build?", "wamid.in1", "whatsapp") == ""
    assert len(fake.turns) == 1, "the same message id is never a second turn"


@pytest.mark.asyncio
async def test_a_brain_that_is_not_ready_says_so_without_a_turn(wired, monkeypatch):
    server = wired
    fake = FakeBrain()
    fake.ready = False
    monkeypatch.setattr(server, "brain_instance", fake)
    assert "starting" in await server._phone_chat("hello", "wamid.in2")
    fake.failed, fake.failure_reason = True, "auth"
    assert "login has expired" in await server._phone_chat("hello", "wamid.in3")
    assert fake.turns == []


@pytest.mark.asyncio
async def test_a_staged_steer_from_the_phone_waits_for_go(accepting, monkeypatch):
    server = accepting
    import run_store
    import session_steer
    recorded, posted = [], []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: recorded.append(a))
    monkeypatch.setattr(session_steer, "post_to_session",
                        lambda path, prompt: posted.append((path, prompt)) or session_steer.SENT)

    def stage():
        server._staged_steers.append(server._StagedSteer(
            session_id="s1", voice_name="chitauri", project="chitauri",
            prompt="carry on with the redirect", socket_path="/tmp/s1.sock"))
    fake = FakeBrain("I'll tell chitauri, sir.", stage=stage)
    monkeypatch.setattr(server, "brain_instance", fake)
    reply = await server._phone_chat("tell chitauri to carry on", "wamid.steer")
    assert reply.startswith("I'll tell chitauri, sir.")
    assert "read it back" in reply and "carry on with the redirect" in reply and "'go'" in reply
    assert posted == [], "nothing moves on the turn itself"
    assert server._staged_steers == [], "and the voice path will not perform it either"
    assert await server._phone_confirm(True, None, "whatsapp") is None, "not delivered yet"
    server._phone_shown("whatsapp", "wamid.steer")
    assert await server._phone_confirm(True, None, "whatsapp") == "Passed to chitauri, sir."
    assert posted == [("/tmp/s1.sock", "carry on with the redirect")]
    assert recorded[-1][4] == session_steer.SENT
    assert await server._phone_confirm(True, None, "whatsapp") is None, "spent"


@pytest.mark.asyncio
async def test_cancel_records_and_does_nothing(wired, monkeypatch):
    server = wired
    import run_store
    import session_steer
    recorded, posted = [], []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: recorded.append(a))
    monkeypatch.setattr(session_steer, "post_to_session",
                        lambda *a: posted.append(a) or session_steer.SENT)
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri", "go on", None))
    message = server._phone_stage_confirmations("whatsapp", "w-1")
    server._phone_shown("whatsapp", "w-1")
    assert "1. Telling chitauri: go on" in message
    assert "Cancelled" in await server._phone_confirm(False, 1, "whatsapp")
    assert posted == [] and recorded[-1][4] == "cancelled_by_user"
    assert await server._phone_confirm(True, 7, "whatsapp") is None, "nothing waits: it is conversation"
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri", "again", None))
    server._phone_stage_confirmations("whatsapp", "w-2")
    server._phone_shown("whatsapp", "w-2")
    assert "Nothing numbered 7" in await server._phone_confirm(True, 7, "whatsapp")
    assert posted == []


@pytest.mark.asyncio
async def test_two_staged_things_need_a_number(wired, monkeypatch):
    server = wired
    import run_store
    monkeypatch.setattr(run_store, "record_steer", lambda *a: None)
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri", "one", None))
    server._staged_steers.append(server._StagedSteer("s2", "hammer", "hammer", "two", None))
    message = server._phone_stage_confirmations("whatsapp", "w-3")
    server._phone_shown("whatsapp", "w-3")
    assert "'go N'" in message
    assert "Which one" in await server._phone_confirm(True, None, "whatsapp")


@pytest.mark.asyncio
async def test_an_unconfirmed_read_back_lapses_with_an_audit_row(wired, monkeypatch):
    server = wired
    import run_store
    recorded = []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: recorded.append(a))
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri", "one", None))
    server._phone_stage_confirmations("whatsapp", "w-4")
    server._phone_shown("whatsapp", "w-4")
    server._phone_pending[0].staged_at -= server.PHONE_CONFIRM_TTL + 1
    assert await server._phone_confirm(True, None, "whatsapp") is None
    assert recorded[-1][4] == "not_confirmed"


@pytest.mark.asyncio
async def test_go_before_the_read_back_arrived_or_on_another_line_does_nothing(accepting, monkeypatch):
    """A read-back exists so the owner sees the exact words before anything
    moves. With turns running beside the poll, a "go" can arrive while the
    read-back is still being sent — or on the other line, which never showed
    it. Neither performs it: until then the word is conversation."""
    server = accepting
    import run_store
    import session_steer
    posted = []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: None)
    monkeypatch.setattr(session_steer, "post_to_session",
                        lambda path, prompt: posted.append(prompt) or session_steer.SENT)
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri",
                                                     "push to main", "/tmp/s1.sock"))
    server._phone_stage_confirmations("telegram", "tg:1:1")
    assert await server._phone_confirm(True, None, "telegram") is None, "not delivered yet"
    server._phone_shown("whatsapp", "tg:1:1")
    assert await server._phone_confirm(True, None, "telegram") is None, "shown elsewhere"
    server._phone_shown("telegram", "tg:1:1")
    assert await server._phone_confirm(True, None, "whatsapp") is None, "read on the other line"
    assert await server._phone_confirm(True, None, "telegram") == "Passed to chitauri, sir."
    assert posted == ["push to main"]


@pytest.mark.asyncio
async def test_a_later_reply_does_not_vouch_for_a_read_back_that_never_arrived(wired, monkeypatch):
    """Turn A reads a steer back and its reply fails to send; turn B's reply
    gets through. Only B's read-back was seen, so A's stays unanswerable."""
    server = wired
    import run_store
    import session_steer
    posted = []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: None)
    monkeypatch.setattr(session_steer, "post_to_session",
                        lambda path, prompt: posted.append(prompt) or session_steer.SENT)
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri",
                                                     "push to main", "/tmp/s1.sock"))
    server._phone_stage_confirmations("telegram", "tg:1:1")
    server._phone_shown("telegram", "tg:1:2")
    assert await server._phone_confirm(True, None, "telegram") is None
    assert posted == []


@pytest.mark.asyncio
async def test_an_unread_read_back_still_lapses_with_an_audit_row(wired, monkeypatch):
    server = wired
    import run_store
    recorded = []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: recorded.append(a))
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri", "one", None))
    server._phone_stage_confirmations("whatsapp", "w-5")
    server._phone_pending[0].staged_at -= server.PHONE_CONFIRM_TTL + 1
    assert await server._phone_confirm(True, None, "whatsapp") is None
    assert recorded[-1][4] == "not_confirmed"


def test_the_desk_route_and_the_phone_decide_through_one_function(wired):
    assert "decide(" in inspect.getsource(api.decision)
    assert "business_api.decide" in inspect.getsource(messaging._decide)


def test_the_store_hook_is_registered_at_startup(wired):
    from fastapi.testclient import TestClient
    server = wired
    with TestClient(server.app):
        assert server._phone_card_hook in store.ON_PROPOSED
        assert messaging._chat is server._phone_chat
        assert messaging._confirm is server._phone_confirm


def test_the_card_hook_puts_the_send_on_the_loop(wired, monkeypatch):
    scheduled = []
    monkeypatch.setattr(messaging, "schedule", lambda make: scheduled.append(make) or True)
    wired._phone_card_hook({"id": "x", "state": "pending"})
    assert len(scheduled) == 1


def test_the_settings_are_settable_bounded_and_scrubbed(wired):
    import claude_env
    server = wired
    for key in ("KAPSO_API_KEY", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_OWNER_NUMBER"):
        assert key in server.SETTABLE_ENV_KEYS
        assert server._env_value_problem(key, "x" * 100_000)
        assert claude_env.is_scrubbed(key) if hasattr(claude_env, "is_scrubbed") else True
    assert server._env_value_problem("WHATSAPP_OWNER_NUMBER", "+14155550132") is None
    child = claude_env.child_env({"KAPSO_API_KEY": KEY, "WHATSAPP_OWNER_NUMBER": OWNER,
                                  "WHATSAPP_PHONE_NUMBER_ID": PHONE_ID, "PATH": "x"})
    assert "KAPSO_API_KEY" not in child and "WHATSAPP_OWNER_NUMBER" not in child
    assert "WHATSAPP_PHONE_NUMBER_ID" not in child and child["PATH"] == "x"


def test_preflight_tells_a_half_finished_setup_from_none_and_from_done(monkeypatch):
    import preflight
    assert preflight._check_whatsapp_sync().ok
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID")
    check = preflight._check_whatsapp_sync()
    assert check.status == preflight.STATUS_WARN and "WHATSAPP_PHONE_NUMBER_ID" in check.message
    assert check.remedy
    assert "half set up" in preflight._phrase_for(check)
    for key in ("KAPSO_API_KEY", "WHATSAPP_OWNER_NUMBER"):
        monkeypatch.delenv(key)
    monkeypatch.delenv("JARVIS_OWNER_PHONE", raising=False)
    untouched = preflight._check_whatsapp_sync()
    assert untouched.ok and "not configured" in untouched.message
    monkeypatch.setenv("KAPSO_API_KEY", KEY)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)
    monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "not a number")
    bad = preflight._check_whatsapp_sync()
    assert bad.status == preflight.STATUS_WARN and "E.164" in bad.message


def test_the_status_route_is_open_and_the_test_route_is_gated(wired, monkeypatch):
    from fastapi.testclient import TestClient
    server = wired
    sent = []

    async def say(text, *, voice=False):
        sent.append((text, voice))
        return {"wamid": "wamid.out1", "via": "text", "voice_note": False}
    monkeypatch.setattr(whatsapp, "say", say)
    with TestClient(server.app) as client:
        status = client.get("/api/whatsapp/status")
        assert status.status_code == 200 and status.json()["configured"] is True
        assert KEY not in status.text
        refused = client.post("/api/whatsapp/test", json={})
        assert refused.status_code == 403, "no Origin, no token: not the browser"
        ok = client.post("/api/whatsapp/test", json={"voice": False},
                         headers={"Origin": "http://localhost:5173"})
        assert ok.status_code == 200 and ok.json()["sent"] is True
        assert sent == [("Testing the line, sir — JARVIS here.", False)]
        capabilities = client.get("/api/capabilities").json()
        assert capabilities["whatsapp"] is True


def test_the_test_route_says_what_is_missing(wired, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("KAPSO_API_KEY")
    with TestClient(wired.app) as client:
        r = client.post("/api/whatsapp/test", json={}, headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 400 and "KAPSO_API_KEY" in r.json()["detail"]



# --- review of b664a9d: a forward is somebody else's words, on this line too --------

def _forwarded_from(sender, text, wamid, **context):
    item = _text_from(sender, text, wamid)
    item["context"] = context or {"forwarded": True}
    return item


@pytest.mark.parametrize("context", [{"forwarded": True}, {"frequently_forwarded": True}])
def test_a_forward_is_marked(context):
    assert whatsapp.parse_inbound(_forwarded_from(OWNER_WA, "hi", "w-f", **context)).forwarded


def test_the_owners_own_text_and_a_reply_are_his():
    assert whatsapp.parse_inbound(_text_from(OWNER_WA, "hi", "w-own")).forwarded is False
    reply = _forwarded_from(OWNER_WA, "that one", "w-reply", **{"from": OWNER_WA, "id": "wamid.q"})
    assert whatsapp.parse_inbound(reply).forwarded is False


@pytest.mark.asyncio
async def test_a_forwarded_approve_decides_nothing_and_goes_to_the_brain_as_a_forward(
        kapso, monkeypatch):
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    action = _connector_card()
    kapso.inbox = [_forwarded_from(OWNER_WA, "approve", "w-fwd-1")]
    await whatsapp.poll_once()
    await messaging.wait_for_turns()
    assert store.get_action(action["id"])["state"] == "pending"
    assert chat.calls == [("approve", "w-fwd-1")] and chat.forwarded == [True]



@pytest.mark.asyncio
async def test_a_bare_go_names_nothing_while_a_read_back_is_unconfirmed(accepting, monkeypatch):
    """Item A was read back and delivered; item B's read-back failed to send,
    as far as JARVIS knows — a timed-out send can still arrive. A bare "go"
    could mean either, so it asks; "go N" still works for what he has seen."""
    server = accepting
    import run_store
    import session_steer
    posted = []
    monkeypatch.setattr(run_store, "record_steer", lambda *a: None)
    monkeypatch.setattr(session_steer, "post_to_session",
                        lambda path, prompt: posted.append(prompt) or session_steer.SENT)
    server._staged_steers.append(server._StagedSteer("s1", "chitauri", "chitauri",
                                                     "carry on", "/tmp/s1.sock"))
    server._phone_stage_confirmations("whatsapp", "w-a")
    server._phone_shown("whatsapp", "w-a")
    seen = server._phone_pending[0].token
    server._staged_steers.append(server._StagedSteer("s2", "hammer", "hammer",
                                                     "push to main", "/tmp/s2.sock"))
    server._phone_stage_confirmations("whatsapp", "w-b")
    assert "Which one" in await server._phone_confirm(True, None, "whatsapp")
    assert posted == []
    assert await server._phone_confirm(True, seen, "whatsapp") == "Passed to chitauri, sir."
    assert posted == ["carry on"]



@pytest.mark.asyncio
async def test_a_number_change_while_marking_read_starts_no_turn(kapso, monkeypatch):
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    real_mark_read = whatsapp.mark_read

    async def mark_then_change(wamid, *, typing=False):
        await real_mark_read(wamid, typing=typing)
        monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "+15005550099")
    monkeypatch.setattr(whatsapp, "mark_read", mark_then_change)
    kapso.inbox = [_text_from(OWNER_WA, "run the deploy", "w-change")]
    await whatsapp.poll_once()
    await messaging.wait_for_turns()
    assert chat.calls == []


@pytest.mark.asyncio
async def test_old_number_message_later_in_the_batch_gets_no_turn(kapso, monkeypatch):
    """The poll matches a whole batch against the configuration it read
    first. A message later in that batch is still the OLD number's, even if
    the number changed while an earlier one was being handled: it must not
    run as the new owner, nor be answered on the new number."""
    chat = Chat()
    monkeypatch.setattr(messaging, "_chat", chat)
    real_mark_read = whatsapp.mark_read
    changed = []

    async def mark_then_change(wamid, *, typing=False):
        await real_mark_read(wamid, typing=typing)
        if not changed:
            changed.append(wamid)
            monkeypatch.setenv("WHATSAPP_OWNER_NUMBER", "+15005550099")
    monkeypatch.setattr(whatsapp, "mark_read", mark_then_change)
    kapso.inbox = [_text_from(OWNER_WA, "first", "w-1", at=time.time() - 2),
                   _text_from(OWNER_WA, "second, from the old number", "w-2", at=time.time() - 1)]
    await whatsapp.poll_once()
    await messaging.wait_for_turns()
    assert chat.calls == []
