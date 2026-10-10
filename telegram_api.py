"""The Telegram line over HTTP: its status, pairing, unpairing, a test.

The status is a GET and carries no secret (the token is never in it). The
rest are POSTs — one mints a code that makes whoever sends it to the bot the
owner (in place of any owner there is), one withdraws that code, one
forgets the owner, one spends a message and, with `voice`, a Fish synthesis
— so the web boundary gates them like every other mutation: a browser
JARVIS serves from, or the tool token.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import telegram

router = APIRouter()

TEST_LINE = "Testing the line, sir — JARVIS here."


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default=TEST_LINE, min_length=1, max_length=telegram.TEXT_BODY_MAX)
    voice: bool = False


@router.get("/api/telegram/status")
async def api_telegram_status():
    return telegram.status()


@router.get("/api/telegram/recent")
async def api_telegram_recent(limit: int = 20):
    return {"messages": telegram.recent(limit)}


@router.post("/api/telegram/pair")
async def api_telegram_pair():
    """A fresh pairing code. Needs the token, not an owner — that is what
    the code is for. An existing owner is replaced by whoever sends it.

    The token is put to Telegram first: a code for a bot Telegram does not
    know would be waited on for ten minutes and could never arrive. The
    answer carries the code's serial; the page waits for THAT serial's
    outcome, because `configured` is already true while an owner exists."""
    if not telegram.token():
        raise HTTPException(400, telegram.issue() or "Set TELEGRAM_BOT_TOKEN first")
    try:
        await telegram.check_token()
    except telegram.ChannelError as e:
        raise HTTPException(400, f"Telegram refused this bot token ({e}). Copy it again "
                                 f"from @BotFather and save it.") from None
    pairing = telegram.new_pairing_code()
    return {**pairing, "bot_username": telegram.status()["bot_username"],
            "how": ("Open the bot in Telegram on your phone and send it this code. "
                    "It is good for ten minutes, once.")}


class CancelPairing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Which code: a page abandoning ITS code must not withdraw one minted
    # since by another tab or by scripts/telegram_setup.py. None: any.
    serial: Optional[int] = None


@router.post("/api/telegram/pair/cancel")
async def api_telegram_pair_cancel(body: Optional[CancelPairing] = None):
    """Withdraw a live code without waiting for it to lapse — only the code
    with `serial`, when one is given. The owner, if there is one, stays."""
    telegram.cancel_pairing(serial=body.serial if body else None)
    return {k: v for k, v in telegram.pairing_status().items() if k != "code"}


@router.post("/api/telegram/unpair")
async def api_telegram_unpair():
    """Forget the owner: nobody's messages are read, and nothing is sent,
    until a phone pairs again. How a lost phone loses the line at once."""
    note = telegram.forget_owner()
    return {**telegram.status(), "note": note}


@router.post("/api/telegram/test")
async def api_telegram_test(body: TestMessage):
    if not telegram.configured():
        names = telegram.missing()
        if names == ["TELEGRAM_OWNER_ID"]:
            raise HTTPException(400, "Not paired yet: press Pair and send the code to the bot")
        detail = ("Set " + ", ".join(names) + " first" if names
                  else str(telegram.issue() or "Telegram is not configured"))
        raise HTTPException(400, detail)
    try:
        receipt = await telegram.say(body.text, voice=body.voice)
    except telegram.ChannelError as e:
        raise HTTPException(502, str(e)) from None
    return {"sent": True, **receipt}
