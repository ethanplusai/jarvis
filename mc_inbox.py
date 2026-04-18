"""
Mission Control inbox watcher — polls MC every 15s for new agent reports
and pushes them to connected WebSocket clients.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mc_client import mc_client

log = logging.getLogger("jarvis.mc_inbox")


async def watch_inbox(notify: Callable[[dict], Awaitable[None]]) -> None:
    """Poll MC inbox, forward unread reports/questions via `notify`.

    `notify` pushes a dict to all registered WebSocket clients — typically
    task_manager._notify.
    """
    seen_ids: set[str] = set()
    while True:
        try:
            await asyncio.sleep(15)
            messages = await mc_client.list_inbox(agent="me", status="unread", limit=20)
            for msg in messages:
                msg_id = msg.get("id")
                if not msg_id or msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                msg_type = msg.get("type", "update")
                sender = msg.get("from", "system")
                subject = msg.get("subject", "(no subject)")
                if msg_type == "report":
                    log.info(f"[MC inbox] {sender} finished: {subject}")
                    await notify(
                        {
                            "type": "mc_inbox",
                            "from": sender,
                            "subject": subject,
                            "body": f"Sir, {sender} finished: {subject}",
                        }
                    )
                elif msg_type == "question":
                    log.info(f"[MC inbox] {sender} is asking: {subject}")
                    await notify(
                        {
                            "type": "mc_inbox",
                            "from": sender,
                            "subject": subject,
                            "body": msg.get("body", "")[:200],
                        }
                    )
                await mc_client.mark_inbox_read(msg_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Inbox watcher error: {e}")
