"""Post a message into a running Claude Code session.

The wire format is one JSON line on the session's inbox Unix socket. It carries
PEER authority, not the user's: the session receives it as a new turn (or
queues it between tool calls if busy), but it cannot dismiss a permission
prompt or a modal dialog. On this machine those are the two `waitingFor`
reasons that actually occur, so the caller must check before promising a fix.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

SENT = "sent"          # the bytes left over the socket — NOT that the target
                        # session accepted or even received them; no reply is
                        # ever read back to confirm that
NOT_LIVE = "not_live"
REFUSED = "refused"
FAILED = "failed"


def post_to_session(socket_path: str | None, prompt: str,
                    timeout: float = 5.0) -> str:
    """Deliver one prompt. Returns `sent`, `not_live`, `refused`, or `failed`.

    A missing socket file and a stale one left by a dead process are both
    `not_live`: from the user's point of view there is nothing to talk to.
    """
    if not prompt or not prompt.strip():
        return REFUSED
    if not socket_path or not Path(socket_path).exists():
        return NOT_LIVE

    lines = []
    # What is actually known about this token, recorded so nobody "fixes" it
    # into something it cannot be: it is optional on macOS (the target may
    # require none at all); we can only ever send OUR OWN — there is no way
    # to look up another process's; tokens observably differ between
    # sessions (3 distinct values seen across 7 live sessions on one
    # machine, and a server started from a plain terminal has none). If the
    # target validates a token and ours does not match — or it has none and
    # we send one — the send below can fail silently from our side: a
    # successful `sendall` proves the bytes left this process, not that the
    # target accepted them. See the SENT docstring above and the wording at
    # the call site in server.py's `_perform_staged_steers`.
    token = os.getenv("CLAUDE_CODE_MESSAGING_TOKEN", "")
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(json.dumps({"type": "user",
                             "message": {"role": "user", "content": prompt.strip()}}))
    payload = ("\n".join(lines) + "\n").encode()

    if not hasattr(socket, "AF_UNIX"):
        if os.name == "nt":
            import windows_inbox
            try:
                windows_inbox.send(socket_path, payload, timeout)
                return SENT
            except OSError as error:
                return NOT_LIVE if error.errno == 10061 else FAILED
        return FAILED
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except (ConnectionRefusedError, FileNotFoundError):
        sock.close()
        return NOT_LIVE           # a stale .sock from a process that has gone
    except OSError:
        sock.close()
        return FAILED
    try:
        sock.sendall(payload)
    except OSError:
        return FAILED
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return SENT
