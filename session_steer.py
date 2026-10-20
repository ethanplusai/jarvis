r"""Post a message into a running Claude Code session.

The wire format is one JSON line on the session's inbox: a Unix socket on
macOS, a named pipe (`\\.\pipe\LOCAL\cc-msg-<hex>`) on Windows. It carries
PEER authority, not the user's: the session receives it as a new turn (or
queues it between tool calls if busy), but it cannot dismiss a permission
prompt or a modal dialog. On this machine those are the two `waitingFor`
reasons that actually occur, so the caller must check before promising a fix.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

SENT = "sent"          # the bytes left over the socket — NOT that the target
                        # session accepted or even received them; no reply is
                        # ever read back to confirm that
NOT_LIVE = "not_live"
REFUSED = "refused"
FAILED = "failed"

PIPE_PREFIX = "\\\\.\\pipe\\"
_ERROR_PIPE_BUSY = 231


def is_pipe(socket_path: str | None) -> bool:
    return bool(socket_path) and socket_path.lower().startswith(PIPE_PREFIX)


def endpoint_exists(socket_path: str | None) -> bool:
    """True if a session's inbox is there to be written to.

    A named pipe is looked up by listing the pipe namespace, never with
    `Path.exists()`: stat-ing a pipe opens it, which takes one of the target
    server's connection instances and drops it again.
    """
    if not socket_path:
        return False
    if is_pipe(socket_path):
        if sys.platform != "win32":
            return False
        name = socket_path[len(PIPE_PREFIX):].lower()
        try:
            return name in {n.lower() for n in os.listdir(PIPE_PREFIX)}
        except OSError:
            return False
    return Path(socket_path).exists()


def post_to_session(socket_path: str | None, prompt: str,
                    timeout: float = 5.0) -> str:
    """Deliver one prompt. Returns `sent`, `not_live`, `refused`, or `failed`.

    A missing socket file and a stale one left by a dead process are both
    `not_live`: from the user's point of view there is nothing to talk to.
    """
    if not prompt or not prompt.strip():
        return REFUSED
    if not endpoint_exists(socket_path):
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

    if is_pipe(socket_path):
        return _post_to_pipe(socket_path, payload, timeout)
    if not hasattr(socket, "AF_UNIX"):
        return FAILED             # a socket path on a Python without AF_UNIX

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
    except (ConnectionRefusedError, FileNotFoundError):
        return NOT_LIVE           # a stale .sock from a process that has gone
    except OSError:
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


def _post_to_pipe(pipe_path: str, payload: bytes, timeout: float) -> str:
    """The Windows half of `post_to_session`: one write to the named pipe.

    Opening succeeds at once, or fails at once: gone (`not_live`), or every
    instance busy with another client, which is waited out up to `timeout`.
    One short write to a pipe whose server is reading does not block.
    """
    import _winapi

    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(pipe_path, "wb", buffering=0) as pipe:
                pipe.write(payload)
            return SENT
        except FileNotFoundError:
            return NOT_LIVE       # the session exited after it was listed
        except OSError as e:
            left_ms = int((deadline - time.monotonic()) * 1000)
            if getattr(e, "winerror", None) != _ERROR_PIPE_BUSY or left_ms <= 0:
                return FAILED     # refused, broken mid-write, or busy too long
            try:
                _winapi.WaitNamedPipe(pipe_path, left_ms)
            except OSError:
                pass              # timed out or gone: the next open says which
