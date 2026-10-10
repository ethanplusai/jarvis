"""The hand-post card: the company page's post, sent to the owner to post himself.

Owner request, 2026-10-08: until LinkedIn grants the Community Management
API, a company-page post cannot go out through the API, and browser
automation is off the table. So the approved card does not publish anything:
it sends the owner, on Telegram, exactly what to post — the text as its own
message, ready to copy, and the media file — and he posts it on the page by
hand. He replies to that message with the post's link, and the link is
written on the card. That reply is his say-so for the write; nothing else
writes it.

Telegram is a fake here; nothing reaches LinkedIn or Telegram.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json

import pytest

TEXT = "Your site ranks #1 on Google and ChatGPT has never heard of you.\n\nBoth are normal."
LINK = "https://www.linkedin.com/feed/update/urn:li:activity:7513999999999999999/"


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple] = []
        self.next_id = 500

    async def send_text(self, text, **_):
        self.next_id += 1
        self.sent.append(("text", text, self.next_id))
        return {"message_id": str(self.next_id), "kind": "text"}

    async def send_document(self, data, filename, mime, caption=""):
        self.next_id += 1
        self.sent.append(("document", (filename, mime, len(data), caption), self.next_id))
        return {"message_id": str(self.next_id), "kind": "document"}

    async def deliver(self, text):
        self.next_id += 1
        self.sent.append(("deliver", text, self.next_id))
        return {"message_id": str(self.next_id)}


@pytest.fixture
def hand(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAHfakeTokenTokenTokenTokenTokenToken00")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "424242001")
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("LINKEDIN_MEDIA_ROOTS", str(media))
    import data_paths
    importlib.reload(data_paths)
    import business_store
    importlib.reload(business_store)
    business_store.init_db()
    import linkedin_handpost
    importlib.reload(linkedin_handpost)
    fake = FakeTelegram()
    import telegram
    for name in ("send_text", "send_document", "deliver"):
        monkeypatch.setattr(telegram, name, getattr(fake, name))
    linkedin_handpost._fake = fake
    linkedin_handpost._media = media
    return linkedin_handpost


def _still(hand, data=b"\x89PNG" + b"5" * 300):
    path = hand._media / "post-3.png"
    path.write_bytes(data)
    return {"kind": "image", "path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "alt_text": "Three failure modes"}


def _desk():
    import business_api
    importlib.reload(business_api)
    business_api.start()
    return business_api


def test_a_hand_post_card_holds_the_page_text_and_its_media(hand):
    body = {"account": "organization", "text": TEXT, "media": _still(hand)}
    assert hand.validate("post", body)
    with pytest.raises(ValueError):
        hand.validate("post", {**body, "text": ""})
    with pytest.raises(ValueError):
        hand.validate("post", {**body, "media": {**body["media"], "sha256": "0" * 64}})
    with pytest.raises(ValueError):
        hand.validate("post", {**body, "schedule": "now"})


def test_approving_sends_the_text_ready_to_copy_and_the_file(hand):
    media = _still(hand)
    receipt = asyncio.run(hand.perform("post", {"account": "organization", "text": TEXT, "media": media}))
    kinds = [kind for kind, *_ in hand._fake.sent]
    assert kinds == ["text", "text", "document"]
    header, post, document = hand._fake.sent
    assert "by hand" in header[1] and "reply" in header[1].lower() and "link" in header[1].lower()
    assert post[1] == TEXT, "the post as its own message, exactly, to copy whole"
    assert document[1][0].endswith(".png") and document[1][2] == len(b"\x89PNG" + b"5" * 300)
    assert receipt["anchor_message_id"] == str(header[2])
    assert set(receipt["message_ids"]) == {str(header[2]), str(post[2]), str(document[2])}


def test_a_media_file_changed_since_the_card_is_not_sent(hand):
    media = _still(hand)
    (hand._media / "post-3.png").write_bytes(b"\x89PNG" + b"6" * 300)
    with pytest.raises(Exception):
        asyncio.run(hand.perform("post", {"account": "organization", "text": TEXT, "media": media}))
    assert hand._fake.sent == []


def test_one_card_one_send_through_the_desk(hand):
    api = _desk()
    card = api.propose(api.Proposal(provider="linkedin_hand", operation="post",
                                    payload={"account": "organization", "text": TEXT, "media": _still(hand)}))
    assert hand._fake.sent == [], "staging a card sends nothing"
    done = asyncio.run(api.decide(card["id"], card["digest"], True))
    assert done["state"] == "submitted"
    assert "by hand" in done["result"]["message"]
    with pytest.raises(ValueError):
        asyncio.run(api.decide(card["id"], card["digest"], True))
    assert len([s for s in hand._fake.sent if s[0] == "document"]) == 1


def test_the_owners_reply_with_the_link_is_written_on_the_card(hand):
    api = _desk()
    card = api.propose(api.Proposal(provider="linkedin_hand", operation="post",
                                    payload={"account": "organization", "text": TEXT}))
    done = asyncio.run(api.decide(card["id"], card["digest"], True))
    anchor = done["result"]["receipt"]["anchor_message_id"]
    recorded = hand.record_reply(anchor, f"Posted: {LINK} thanks")
    assert recorded is not None and recorded["id"] == card["id"]
    import business_store
    after = business_store.get_action(card["id"])
    assert after["result"]["post_url"] == LINK
    assert LINK in after["result"]["message"]
    assert after["result"]["receipt"]["anchor_message_id"] == anchor, "the delivery receipt is kept"
    assert "posted_by_hand" in [a["event"] for a in business_store.audit(card["id"])]


def test_a_reply_that_is_not_to_a_hand_post_or_has_no_link_records_nothing(hand):
    api = _desk()
    card = api.propose(api.Proposal(provider="linkedin_hand", operation="post",
                                    payload={"account": "organization", "text": TEXT}))
    done = asyncio.run(api.decide(card["id"], card["digest"], True))
    anchor = done["result"]["receipt"]["anchor_message_id"]
    assert hand.record_reply(999999, LINK) is None
    assert hand.record_reply(anchor, "done, will send the link later") is None
    assert hand.record_reply(anchor, "https://example.com/not-linkedin") is None


def test_telegram_reads_which_message_a_reply_answers(hand):
    import telegram
    update = telegram.parse_update({"update_id": 7, "message": {
        "message_id": 41, "date": 1, "text": LINK, "from": {"id": 424242001},
        "chat": {"id": 424242001, "type": "private"}, "reply_to_message": {"message_id": 501}}})
    assert update.reply_to == 501


def test_a_reply_with_the_link_is_recorded_without_a_brain_turn(hand, monkeypatch):
    import telegram
    import messaging
    api = _desk()
    card = api.propose(api.Proposal(provider="linkedin_hand", operation="post",
                                    payload={"account": "organization", "text": TEXT}))
    done = asyncio.run(api.decide(card["id"], card["digest"], True))
    anchor = int(done["result"]["receipt"]["anchor_message_id"])
    turns = []

    async def on_text(*args, **kwargs):
        turns.append(args)

    monkeypatch.setattr(messaging, "on_text", on_text)
    update = telegram.parse_update({"update_id": 8, "message": {
        "message_id": 42, "date": 1, "text": LINK, "from": {"id": 424242001},
        "chat": {"id": 424242001, "type": "private"}, "reply_to_message": {"message_id": anchor}}})
    asyncio.run(telegram.handle(update, owner=("telegram", 424242001)))
    assert turns == [], "the link is written by the reply itself, not by the brain"
    import business_store
    assert business_store.get_action(card["id"])["result"]["post_url"] == LINK
    assert any(LINK in str(s[1]) for s in hand._fake.sent if s[0] == "deliver"), "he is told it was recorded"
