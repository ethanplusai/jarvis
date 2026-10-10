"""The WhatsApp line over HTTP: its status, and a test message.

Two routes, both for the Settings panel and the diagnostics page. The
status is a GET and shows no secret (the owner's number is masked, the key
is never in it). The test is a POST — it spends a message and, with `voice`,
a Fish synthesis — so the web boundary gates it like every other mutation:
a browser JARVIS serves from, or the tool token.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import whatsapp

router = APIRouter()

TEST_LINE = "Testing the line, sir — JARVIS here."


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(default=TEST_LINE, min_length=1, max_length=whatsapp.TEXT_BODY_MAX)
    voice: bool = False


@router.get("/api/whatsapp/status")
async def api_whatsapp_status():
    return whatsapp.status()


@router.get("/api/whatsapp/recent")
async def api_whatsapp_recent(limit: int = 20):
    """The last few messages either way: id, direction, kind, time and a
    short summary. What the owner wrote is not here (the Conversation
    panel has it); what JARVIS sent is, clipped."""
    return {"messages": whatsapp.recent(limit)}


@router.post("/api/whatsapp/test")
async def api_whatsapp_test(body: TestMessage):
    if not whatsapp.configured():
        names = whatsapp.missing()
        detail = ("Set " + ", ".join(names) + " first" if names
                  else str(whatsapp.issue() or "WhatsApp is not configured"))
        raise HTTPException(400, detail)
    try:
        receipt = await whatsapp.say(body.text, voice=body.voice)
    except whatsapp.WindowClosed as e:
        raise HTTPException(
            409, "The 24-hour window is shut: send JARVIS's number a message from your "
                 "phone first, or set WHATSAPP_TEMPLATE (see docs/whatsapp.md). " + str(e)) from None
    except whatsapp.ChannelError as e:
        raise HTTPException(502, str(e)) from None
    return {"sent": True, **receipt}
