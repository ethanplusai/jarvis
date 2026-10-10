"""JARVIS's children calling JARVIS: one POST to its own server, and nowhere else.

The PreToolUse hook (`pretool_hook.py`) and the stdio MCP child
(`jarvis_mcp.py`) both call the server that spawned their parent, carrying
the loopback bearer token. That call must reach the address it names and
nothing else, and urllib's default opener guaranteed neither half:

- **It asks for a proxy.** `ProxyHandler` reads HTTP_PROXY / HTTPS_PROXY /
  ALL_PROXY — and on Windows, with none set, the proxy in Internet Options
  (`getproxies_registry`) — and bypasses one for 127.0.0.1 only when
  NO_PROXY names it. `claude_env.child_env` passes proxy variables through
  on purpose, since a user's own tools may need them. Over HTTP the proxy is
  handed the token in the clear; over HTTPS it is asked for a CONNECT it
  could answer with a certificate of its own, and nothing here would notice,
  because the loopback certificate is self-signed and is not verified.
- **It follows redirects.** A 301/302/303 is re-sent as a GET, to whatever
  host the Location names, with the Authorization header still on — and the
  hook would then take its verdict from wherever it landed.

`http.client` does neither: it dials the host and port it is given and
reports what came back. A 3xx here is an answer like any other status that
is not 2xx, and raises `StatusError`.

Standard library only, and nothing at import time but imports: the hook
loads this per tool call, and a hook that dies before it prints a verdict
exits 1, which the Claude CLI treats as a non-blocking error — the tool
call RUNS.
"""
from __future__ import annotations

import http.client
import ssl
import urllib.parse


class StatusError(Exception):
    """The server answered with a status that is not 2xx — a redirect
    included, since one is never followed. Carries the status and nothing
    else: callers put it into words the user hears."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


def post_json(url: str, body: bytes, token: str, *, timeout: float,
              context: ssl.SSLContext | None = None) -> bytes:
    """POST `body` (JSON) to `url` with `Authorization: Bearer <token>`,
    straight to the host and port in `url`. Returns the body of a 2xx answer.

    `context` is used for an https URL exactly as given; None means the
    default, verified one. Raises ValueError for a URL that is not http(s)
    with a host — before anything is dialled; StatusError for any status
    that is not 2xx; OSError (TimeoutError, ConnectionError, an
    ssl.SSLError) or http.client.HTTPException for a transport that failed.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL with a host (scheme {parts.scheme!r})")
    port = parts.port   # ValueError for a port that is not a number
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if parts.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, port, timeout=timeout, context=context)
    else:
        connection = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    try:
        connection.request("POST", target, body=body, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        # Closed on the way out whatever happens: on `Connection: close` the
        # socket belongs to the response, and a StatusError raised past an
        # open one would keep it open for as long as the error is held.
        with connection.getresponse() as response:
            if not 200 <= response.status < 300:
                raise StatusError(response.status)
            return response.read()
    finally:
        connection.close()
