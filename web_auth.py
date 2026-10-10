"""Who is allowed to reach JARVIS's HTTP and WebSocket surface.

JARVIS spawns `claude --dangerously-skip-permissions` on request and reads
every conversation on the machine. Until this module existed, anything that
could open a TCP connection could do both: `POST /api/runs` was remote code
execution, and `/ws/voice` accepted a handshake from any web page the user
happened to be visiting and let it speak as them.

There are exactly two legitimate callers, and they prove themselves in
different ways:

**The browser.** It has no secret and must not need one — the user opens
Chrome, clicks once, and talks. But it is same-origin (Vite proxies `/api`
and `/ws` from :5173 to the API port, and the built frontend is served off
the API port itself), so every state-changing request and every WebSocket
handshake it makes carries an `Origin` header the browser sets and no page
can forge. That header is what admits it. A page on `http://evil.example`
— or on `http://localhost:3000`, which is just as much a stranger — sends
its own origin and is refused.

**A local non-browser client**: the brain's MCP child, a debugging script.
It sends no `Origin` at all, but it can read the tool token out of a 0600
file in the data directory, which only this user can open. That token is
what admits it.

So: a request is authorized if its `Origin` is one JARVIS actually serves
from, or if it carries the tool token. A WebSocket with no `Origin` is not a
browser; it is refused unless it has the token, because "no Origin" is
exactly what the LAN exploit looks like, and refusing it outright would
break the local clients that legitimately have no origin to send.

What this does NOT do, and cannot: an `Origin` header is only unforgeable
when a browser sets it. Anything speaking raw HTTP can send whatever origin
it likes. Against a non-browser attacker the defence is the token and the
loopback bind (`server.DEFAULT_BIND_HOST`), not this header.

Reads (`GET`, `HEAD`) are deliberately not gated on `Origin`. A same-origin
`GET` from fetch carries no `Origin` header at all, so gating them would
break the dashboard the moment it asked for `/api/runs`, and the browser has
no token to offer instead. What keeps a hostile page from *reading* those
responses is that JARVIS sends no CORS headers at all any more; what keeps a
LAN client from reading them is the loopback bind.

One thing does gate every request including the reads: `host_allowed`. DNS
rebinding is the way around "no CORS" — point evil.example at 127.0.0.1 and
the page and the response share an origin, so no CORS is needed. The `Host`
header is the only evidence of it, and a rebinding attack must name a domain
it controls, which has a dot in it.

Framing is the *other* way around the Origin check, and it forges nothing.
A hostile page iframes `http://localhost:8340/dashboard`, makes it
invisible, lays bait over the top, and harvests one click. The click lands
on JARVIS's own button, so the request it makes is made by JARVIS's own
page and carries the one `Origin` the guard exists to admit. Every response
therefore says it will not be framed — see `SECURITY_HEADERS`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import sys
from typing import NamedTuple, Optional, Sequence
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

import data_paths

log = logging.getLogger("jarvis.auth")

# The methods that can change something. A GET cannot be gated (see above),
# so anything with a side effect must not be one.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Every spelling of "this machine" a browser can put in an Origin.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")

# Vite's dev server. It takes the next free port when 5173 is busy, and the
# user must not have to discover that a second `npm run dev` broke JARVIS —
# so the window is a handful of ports wide, not one. Deliberately not "any
# loopback port": a page on http://localhost:3000 is somebody else's dev
# server and is exactly as much a stranger as evil.example.
#
# Measured, not assumed: with the shipped vite.config.ts the browser's own
# origin is what arrives here. Vite forwards `Origin` verbatim on the POST
# and on the WebSocket upgrade (and sends none on a same-origin GET, which
# is why GETs are not gated).
DEV_SERVER_PORTS = tuple(range(5173, 5181))

DEFAULT_API_PORT = 8340

# Sent on EVERY response JARVIS writes — HTML, JSON, static asset, 404, and
# the guard's own 403s. Not per-route: a header applied per route is a header
# somebody forgets on the route added next month.
#
#   frame-ancestors / X-Frame-Options
#       The clickjacking fix, and the reason this list exists. Both
#       spellings because they are not interchangeable: `X-Frame-Options` is
#       what a browser reads when it has no CSP support to speak of, and
#       `frame-ancestors` is what supersedes it everywhere else — and it is
#       the one that also covers `<object>`, `<embed>` and `<frame>`, so a
#       JSON response cannot be framed either.
#
#   nosniff
#       JARVIS answers with JSON that contains attacker-influenced text
#       (a prompt, a file path, an LLM's own words). Without this a browser
#       is free to decide a response "looks like" HTML and run it.
#
#   Referrer-Policy
#       The dashboard's URLs carry run ids and project paths. `no-referrer`
#       rather than `strict-origin-when-cross-origin`: nothing here has any
#       reason to tell an outside site it was visited from, so the strictest
#       value costs nothing.
#
# Deliberately NOT a full CSP. Every script on both pages is an external
# `<script type="module" src=...>` and every style an external stylesheet
# (verified in the source and the built output), so `script-src 'self'`
# would hold today — but it would only ever apply to the pages served off
# this port (in development they come from Vite, which sends none of this),
# and it would fail closed and silently the first time somebody inlined a
# script. `frame-ancestors` is the directive doing security work here.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)

_SECURITY_HEADER_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)


def apply_security_headers(raw_headers) -> list[tuple[bytes, bytes]]:
    """Our values, replacing rather than joining any that are already there.

    Appending would produce `X-Frame-Options: SAMEORIGIN, DENY`, which is
    two conflicting policies and which several browsers resolve by ignoring
    the header entirely. These are the security headers; ours win.
    """
    kept = [(name, value) for name, value in (raw_headers or ())
            if name.lower() not in _SECURITY_HEADER_NAMES]
    kept.extend(SECURITY_HEADERS)
    return kept


# ---------------------------------------------------------------------------
# Where this process is actually listening
# ---------------------------------------------------------------------------
#
# Three things need to know: the origin allowlist, the Host allowlist, and
# the URL the brain's MCP child dials. All three read JARVIS_PORT /
# JARVIS_BIND_HOST / JARVIS_SCHEME, and until now only `server.main()` ever
# wrote them. So `python server.py --port 9000` worked and
#
#     uvicorn server:app --port 9000
#
# did not: nothing set the variables, the allowlist was still built for
# 8340, and the operator's own browser at http://localhost:9000 was refused
# by JARVIS's own guard. `uvicorn server:app` on uvicorn's default port 8000
# had the same problem, and so did the `--host 0.0.0.0` warning, which was
# printed from `__main__` and therefore never seen by the one launch path
# that most needed it.
#
# The app cannot ask uvicorn where it bound: uvicorn runs the ASGI lifespan
# *before* it creates its sockets (`Server.startup` calls
# `lifespan.startup()` first), so at the moment this is needed there is no
# socket to interrogate. What is left is what the process was launched with:
# the environment, then the command line, then the launcher's own documented
# defaults — and, when even that is not enough, saying so out loud instead
# of guessing.

# Uvicorn's own CLI defaults, which are not JARVIS's.
UVICORN_DEFAULT_HOST = "127.0.0.1"
UVICORN_DEFAULT_PORT = 8000

# JARVIS's, per `server.main()`'s argparse defaults.
JARVIS_DEFAULT_HOST = "127.0.0.1"
JARVIS_DEFAULT_PORT = DEFAULT_API_PORT

# Every spelling of "only this machine" that can be *bound* to. Wider than
# LOOPBACK_HOSTS above, which is about what a browser puts in an Origin:
# 127.0.0.0/8 is all loopback, and an operator may well bind 127.0.0.2.
_LOOPBACK_NAMES = frozenset({"localhost"})

# Flags that name a listening socket this cannot reason about as an address.
_OPAQUE_BIND_FLAGS = ("--uds", "--fd")


class Bind(NamedTuple):
    """Where the server is listening, and how confident we are about it.

    `source` is part of the value on purpose. "unknown" is a real answer and
    it must be able to travel: adopting a guessed bind would build an origin
    allowlist for a port nothing is listening on and lock the operator out
    of their own dashboard without a word.
    """

    host: str
    port: int
    scheme: str
    source: str


def is_loopback(host: Optional[str]) -> bool:
    """Does binding here mean "only this machine can reach it"?"""
    if not host:
        return False
    name = host.strip().strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _flag(argv: Sequence[str], name: str) -> Optional[str]:
    """`--name value` or `--name=value`; the last occurrence wins, as click's
    own parsing does."""
    found = None
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            found = argv[i + 1]
        elif arg.startswith(f"{name}="):
            found = arg[len(name) + 1:]
    return found


def detect_bind(argv: Sequence[str] | None = None,
                environ=None) -> Bind:
    """The address this process is listening on, as well as it can be known.

    The environment first — `server.main()` records its parsed arguments
    there, and an operator behind a launcher this cannot read can set the
    same variables by hand. Then the command line, which covers both
    `python server.py --host ...` and `uvicorn server:app --host ...`
    because both spell it the same way. Then the defaults of whichever
    launcher this looks like.
    """
    original = list(getattr(sys, "orig_argv", [])) if argv is None else list(argv)
    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ

    launcher = (argv[0] if argv else "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    uvicorn_cli = launcher.startswith("uvicorn") or any(
        arg == "-m" and index + 1 < len(original) and original[index + 1] == "uvicorn"
        for index, arg in enumerate(original))
    default_host = UVICORN_DEFAULT_HOST if uvicorn_cli else JARVIS_DEFAULT_HOST
    default_port = UVICORN_DEFAULT_PORT if uvicorn_cli else JARVIS_DEFAULT_PORT

    env_host = (environ.get("JARVIS_BIND_HOST") or "").strip()
    env_port = (environ.get("JARVIS_PORT") or "").strip()
    env_scheme = (environ.get("JARVIS_SCHEME") or "").strip()

    argv_host = _flag(argv, "--host")
    argv_port = _flag(argv, "--port")
    ssl_flagged = any(a == f or a.startswith(f"{f}=") for a in argv
                      for f in ("--ssl-keyfile", "--ssl-certfile", "--ssl"))

    host = env_host or argv_host or default_host
    scheme = env_scheme or ("https" if ssl_flagged else "http")

    port = default_port
    # First that parses wins, in the same order as the host: environment,
    # then command line, then the launcher's default.
    for candidate in (env_port, argv_port):
        if not candidate:
            continue
        try:
            port = int(candidate)
            break
        except (TypeError, ValueError):
            log.warning("ignoring a port that is not a number: %r", candidate)

    if env_host or env_port:
        source = "environment"
    elif any(a == f or a.startswith(f"{f}=") for a in argv
             for f in _OPAQUE_BIND_FLAGS):
        # A unix socket or an inherited fd. Not an address, and pretending
        # otherwise would build the wrong allowlist in silence.
        source = "unknown"
    elif argv_host or argv_port:
        source = "command line"
    elif uvicorn_cli:
        source = "uvicorn default"
    else:
        source = "default"

    return Bind(host, port, scheme, source)


def adopt_bind(bind: Bind) -> None:
    """Publish a detected bind so the allowlists and the MCP URL see it.

    `setdefault`, never overwrite: `server.main()` writes these before
    uvicorn even starts and it is the one launcher that knows for certain.
    A bind whose source is "unknown" is not published at all — the existing
    defaults are a better guess than a wrong certainty.
    """
    if bind.source == "unknown":
        return
    os.environ.setdefault("JARVIS_BIND_HOST", bind.host)
    os.environ.setdefault("JARVIS_PORT", str(bind.port))
    os.environ.setdefault("JARVIS_SCHEME", bind.scheme)


def exposure_warning(bind: Bind) -> list[str]:
    """What to tell the operator, or [] when there is nothing to tell.

    Everything on this surface acts with the user's full authority:
    `/api/runs` spawns `claude --dangerously-skip-permissions`,
    `/api/sessions` reads every conversation on the machine. The Origin
    check makes those safe from a hostile *page*, but an Origin header is
    only unforgeable when a browser sets it — anything speaking raw HTTP can
    claim to be the dashboard. The tool token is the answer for a local
    client; there is no answer for a LAN client except not being on the LAN.
    """
    if bind.source == "unknown":
        return [
            "! JARVIS could not work out what address it is listening on",
            "  from how it was launched, so its own origin and Host checks",
            "  are using defaults that may be wrong — the browser may be",
            "  refused. Set JARVIS_BIND_HOST and JARVIS_PORT to the address",
            "  you are actually serving from.",
        ]
    if is_loopback(bind.host):
        return []
    return [
        f"! Bound to {bind.host}, not loopback. Anything that can",
        "  reach this port can read every conversation on this",
        "  machine; only the tool token stands in front of the",
        "  endpoints that act. Bind 127.0.0.1 unless you mean it —",
        "  and if you do, set JARVIS_ALLOWED_ORIGINS to the address",
        "  you will open the page at, or the browser will be",
        "  refused too.",
    ]


def api_port() -> int:
    """The port this server is actually bound to, per main()."""
    try:
        return int(os.getenv("JARVIS_PORT", "") or DEFAULT_API_PORT)
    except ValueError:
        return DEFAULT_API_PORT


def allowed_origins() -> frozenset[str]:
    """The origins JARVIS serves its own pages from — and nothing else.

    Recomputed per call rather than cached: the port is only known once
    main() has parsed its arguments, and the test suite moves it.
    """
    ports = {api_port(), *DEV_SERVER_PORTS}
    origins = {
        f"{scheme}://{host}:{port}"
        for scheme in ("http", "https")
        for host in LOOPBACK_HOSTS
        for port in ports
    }
    for extra in os.getenv("JARVIS_ALLOWED_ORIGINS", "").split(","):
        extra = extra.strip().rstrip("/")
        if extra:
            origins.add(extra)
    return frozenset(origins)


def origin_allowed(origin: Optional[str]) -> bool:
    """Exact match only.

    Never a prefix or suffix test: `http://localhost:5173.evil.example` and
    `http://localhost:51730` both start with an allowed origin, and `null`
    (what a sandboxed iframe or a file:// page sends) is not this machine.
    """
    if not origin:
        return False
    return origin in allowed_origins()


def allowed_hosts() -> frozenset[str]:
    """Host names an operator has said this server answers to.

    The bind host counts: `python server.py --host jarvis.example.com` is
    the operator saying that name IS this server, and the brain's own MCP
    child dials whatever main() was given. Every other dotted name has to be
    named in JARVIS_ALLOWED_ORIGINS.
    """
    hosts = set()
    bind = os.getenv("JARVIS_BIND_HOST", "").strip().strip("[]").lower()
    if bind:
        hosts.add(bind)
    for origin in os.getenv("JARVIS_ALLOWED_ORIGINS", "").split(","):
        origin = origin.strip()
        if not origin:
            continue
        try:
            name = urlsplit(origin).hostname
        except ValueError:
            continue
        if name:
            hosts.add(name.lower())
    return frozenset(hosts)


def host_allowed(host_header: Optional[str]) -> bool:
    """Refuse a `Host` that names a domain. This is the rebinding check.

    The reads — /api/sessions, /api/specs/doc — cannot be gated on Origin,
    because a same-origin GET from fetch does not send one. What stops a
    hostile page reading them is that JARVIS answers with no CORS headers.
    DNS rebinding goes around that: evil.example resolves to 127.0.0.1, the
    browser sends `Host: evil.example`, and the page and the response now
    share an origin, so no CORS is needed at all.

    The Host header is the only evidence that this happened, and it is
    enough. A rebinding attack has to name a domain it controls in public
    DNS, and a public domain has a dot in it. An address literal is not a
    name and has nothing to rebind; a single-label host (`localhost`, a
    machine name, `testserver`) cannot be a public domain. Anything dotted
    has to be declared in JARVIS_ALLOWED_ORIGINS — which an operator
    reaching JARVIS at a .local or tailscale name has already had to set for
    the Origin check anyway.
    """
    if not host_header:
        return True                    # HTTP/1.0, and some local clients
    try:
        hostname = urlsplit(f"//{host_header}").hostname or ""
    except ValueError:
        return False
    if not hostname:
        return False
    hostname = hostname.lower()
    if hostname in allowed_hosts():
        return True
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return True
    except ValueError:
        pass
    return "." not in hostname


def bearer(header_value: Optional[str]) -> Optional[str]:
    """The token out of an `Authorization: Bearer <token>` header."""
    if not header_value or len(header_value) < 8:
        return None
    if header_value[:7].lower() != "bearer ":
        return None
    return header_value[7:].strip() or None


def token_matches(supplied: Optional[str]) -> bool:
    """Constant-time compare against the per-install tool token.

    The token file is only touched when a caller actually offered one, so
    the common path — a browser with a good origin — does no disk I/O.
    """
    if not supplied:
        return False
    try:
        expected = data_paths.ensure_tool_token()
    except Exception as e:                                   # pragma: no cover
        log.error(f"could not read the tool token: {e}")
        return False
    try:
        return secrets.compare_digest(supplied.encode("utf-8", "ignore"),
                                      expected.encode("utf-8"))
    except (TypeError, ValueError):                          # pragma: no cover
        return False


def request_authorized(origin: Optional[str],
                       authorization: Optional[str]) -> bool:
    return origin_allowed(origin) or token_matches(bearer(authorization))


def _header(scope: dict, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def scope_authorized(scope: dict) -> bool:
    return request_authorized(_header(scope, b"origin"),
                              _header(scope, b"authorization"))


class OriginGuard:
    """One gate in front of everything, rather than a check per route.

    A raw ASGI middleware and not `@app.middleware("http")` on purpose:
    BaseHTTPMiddleware passes WebSocket scopes straight through, and the
    WebSockets are where the worst of it was. Sitting above the router also
    means a route added later is covered the day it is written, instead of
    the day somebody remembers.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope.get("type")
        # Wrapped before anything else so the refusals written below carry
        # the headers too — they never reach the router, so a middleware
        # further in could not have added them.
        if kind == "http":
            send = self._with_security_headers(send)
        # The Host check covers every method, reads included: it is the only
        # evidence of a DNS rebind, which is how a page makes a loopback GET
        # same-origin and so needs no CORS from us at all.
        if kind in ("http", "websocket") and not host_allowed(
                _header(scope, b"host")):
            self._log_refusal(scope, f"{kind} (host)")
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(
                    status_code=403,
                    content={"error": "Refused: that Host is not one JARVIS "
                                      "answers to."})(scope, receive, send)
            return
        if kind == "websocket":
            if not scope_authorized(scope):
                self._log_refusal(scope, "websocket")
                # Closing before `accept` is how ASGI says "no": uvicorn
                # turns it into an HTTP 403 and the socket never opens.
                await send({"type": "websocket.close", "code": 1008})
                return
        elif kind == "http" and scope.get("method", "").upper() in MUTATING_METHODS:
            if not scope_authorized(scope):
                self._log_refusal(scope, scope.get("method", "?"))
                response = JSONResponse(
                    status_code=403,
                    content={"error": "Refused: this request did not come "
                                      "from a page JARVIS serves, and "
                                      "carried no tool token."})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _with_security_headers(send):
        """Add SECURITY_HEADERS to the response start message.

        A raw ASGI wrapper rather than a `BaseHTTPMiddleware` on top,
        because BaseHTTPMiddleware would buffer every streamed response
        (`/ws` aside, the run event feed and static files both stream) and
        because this guard already has to be raw ASGI to see WebSockets.

        Only `http.response.start` is touched; body chunks and the
        WebSocket message types pass through untouched.
        """
        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = apply_security_headers(
                    message.get("headers"))
            await send(message)

        return send_wrapper

    @staticmethod
    def _log_refusal(scope: dict, what: str) -> None:
        log.warning("refused %s %s from origin %r",
                    what, scope.get("path", "?"),
                    _header(scope, b"origin") or "<none>")
