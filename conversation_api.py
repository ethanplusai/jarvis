"""The Conversation panel's history over HTTP. Carved out of server.py:
read, receipt, and the one soft delete (see conversation_store.init_db for
why it is soft). Reads are ungated like every GET; the delete sits behind
the web boundary like every mutation."""
import asyncio

from fastapi import APIRouter, HTTPException

import conversation_store

router = APIRouter()


@router.get("/api/conversation")
async def api_conversation(limit: int = 100, before: int | None = None):
    return {"messages": await asyncio.to_thread(conversation_store.list_messages, limit, before)}


@router.get("/api/conversation/{message_id}")
async def api_message_receipt(message_id: str):
    row = await asyncio.to_thread(conversation_store.get, message_id)
    if row is None:
        raise HTTPException(404, "Message not received")
    return row


@router.delete("/api/conversation/{message_id}")
async def api_message_delete(message_id: str):
    """Take one message out of the panel's history. Soft: the row keeps its
    id (see `conversation_store.init_db`), so a receipt for it still
    answers and a retried draft with that id is still a duplicate. A
    mutation, so the web boundary gates it like every other."""
    row = await asyncio.to_thread(conversation_store.get, message_id)
    if row is None:
        raise HTTPException(404, "No such message")
    await asyncio.to_thread(conversation_store.delete_message, message_id)
    return await asyncio.to_thread(conversation_store.get, message_id)
