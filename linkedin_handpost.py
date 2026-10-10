"""The hand-post card: a LinkedIn post sent to the owner to publish himself.

Owner request, 2026-10-08. A company page can only be posted to
through LinkedIn's API once LinkedIn grants the Community Management API,
and browser automation is ruled out. Until then a company-page post is the
desk's `linkedin_hand` provider: the card holds the exact text and media,
the owner's Approve sends him — on Telegram — the text as its own message,
ready to copy whole, and the media file as sent, with nothing re-encoded.
He posts it on the page by hand and replies to that message with the
post's link. `record_reply` writes the link on the card. His reply is the
say-so for that write, and nothing else writes it: not the brain, not a
message that is not a reply to the card's own delivery.

Nothing here touches LinkedIn. It is the same gate as every other card:
one card, approval spent once (`business_api.decide`), receipt on the card.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
from contextlib import closing
from typing import Optional

import linkedin_api
from business_providers import ProviderError

PROVIDER = "linkedin_hand"
PAGE_NAMES = {"organization": "the company page", "member": "your own profile"}
# A link to a LinkedIn post, as the owner would paste it back.
_LINK = re.compile(r"https://(?:www\.)?linkedin\.com/[^\s<>\"']+|https://lnkd\.in/[^\s<>\"']+")
DOCUMENT_MAX = 50 * 1024 * 1024   # a Telegram bot's document limit


def validate(operation: str, body) -> dict:
    """The same post a `linkedin` card holds: account, text, optional media
    from the media folder bound by sha256. No sign-in needed."""
    if operation != "post":
        raise ValueError("A hand-post is operation post")
    if not isinstance(body, dict):
        raise ValueError("Invalid hand-post")
    allowed = {"account", "text", "media"}
    if set(body) - allowed:
        raise ValueError(f"A hand-post has unknown fields: {sorted(set(body) - allowed)}")
    if body.get("account", "organization") not in PAGE_NAMES:
        raise ValueError("account must be organization or member")
    linkedin_api._check_text(body.get("text"), linkedin_api.TEXT_MAX)
    if "media" in body:
        linkedin_api._check_media(body["media"])
    return body


def identity() -> dict:
    """Bound to the owner's Telegram line: a card staged for one owner is
    never delivered to another."""
    import telegram
    cfg = telegram.config()
    if cfg is None:
        raise ValueError("Telegram is not set up; a hand-post is delivered there")
    return {"delivery": "telegram",
            "owner": hashlib.sha256(str(cfg.owner_id).encode()).hexdigest()[:16]}


async def perform(operation: str, body: dict) -> dict:
    """Send the owner what to post. Nothing goes to LinkedIn."""
    import telegram
    validate(operation, body)
    account = body.get("account", "organization")
    data = None
    media = body.get("media")
    if media:
        data = linkedin_api._media_file(media).read_bytes()
        if hashlib.sha256(data).hexdigest() != media.get("sha256"):
            raise ProviderError("The media file changed since the card was approved; nothing was sent.")
        if len(data) > DOCUMENT_MAX:
            raise ProviderError("The media file is too big for Telegram; take it from the desk.")
    where = PAGE_NAMES[account]
    header = (f"Post this on {where} by hand, sir — the text exactly as in the next message"
              + (", with the file after it" if media else "") +
              ". When it is up, reply to THIS message with the post's link and I will "
              "write it on the card.")
    sent = []
    try:
        first = await telegram.send_text(header)
        sent.append(first["message_id"])
        for piece in _pieces(body["text"]):
            sent.append((await telegram.send_text(piece))["message_id"])
        if media:
            name = linkedin_api._media_file(media).name
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            caption = media.get("alt_text") or media.get("title") or ""
            sent.append((await telegram.send_document(data, name, mime, caption=caption[:900]))["message_id"])
    except Exception as error:
        # Telegram's own failure: the owner may have part of it. Unknown, so
        # the desk says to look before sending again.
        raise ProviderError(f"Telegram did not take all of the hand-post ({error.__class__.__name__}); "
                            f"check your phone before sending it again.", uncertain=bool(sent)) from None
    return {"delivered": "telegram", "account": account, "anchor_message_id": first["message_id"],
            "message_ids": sent, "media": (media or {}).get("kind")}


def _pieces(text: str) -> list[str]:
    import messaging
    return messaging.chunks(text) if len(text) > messaging.TEXT_CHUNK else [text]


def summary(receipt: dict) -> str:
    where = PAGE_NAMES.get(receipt.get("account", "organization"), "LinkedIn")
    return (f"Sent to your phone to post by hand on {where}. Reply to that message with "
            f"the post's link and it is written here.")


def record_reply(reply_to, text) -> Optional[dict]:
    """The owner replied to a hand-post's delivery with the post's link:
    write it on that card. None when the reply answers no hand-post, or
    holds no LinkedIn link — then it is an ordinary message."""
    import business_store
    found = _LINK.search(str(text or ""))
    if not found:
        return None
    link = found.group(0).rstrip(".,;:)!?")
    wanted = str(reply_to)
    with closing(business_store.connect()) as conn:
        rows = conn.execute(
            "SELECT id, result FROM business_actions WHERE provider=? AND state='submitted' "
            "AND updated>=? ORDER BY seq DESC LIMIT 50",
            (PROVIDER, time.time() - 30 * 86400)).fetchall()
    for action_id, raw in rows:
        try:
            result = json.loads(raw or "{}")
        except ValueError:
            continue
        receipt = result.get("receipt") or {}
        if wanted == str(receipt.get("anchor_message_id")) or wanted in {str(m) for m in receipt.get("message_ids") or []}:
            return business_store.add_link(action_id, PROVIDER, link,
                                           f"Posted by hand. {link}", "posted_by_hand")
    return None
