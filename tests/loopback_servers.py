"""Stand-ins for the far end of a call JARVIS makes to itself.

WHY THIS EXISTS
---------------
The brain's children — the PreToolUse hook, the stdio MCP child, the setup
scripts — call JARVIS's own server with the loopback bearer token. That call
must go STRAIGHT to the address it names: not through an HTTP(S)_PROXY from
the environment (which `claude_env.child_env` deliberately passes through,
because a user's own tools may need it), and not on to wherever a redirect
points. Either would hand the token to somebody else, and the TLS on this
hop is not verified (the certificate is JARVIS's own self-signed one), so an
HTTPS CONNECT through a proxy is one the proxy could open.

These are the three things a test needs to show that, all on 127.0.0.1:

  Endpoint   a server that records every request it is sent and answers
             whatever it is told to; over TLS with `tls_endpoint`.
  Listener   a "proxy" that is listening: it records the first bytes of
             every connection (a proxied request carries the token in the
             clear; a tunnelled one starts `CONNECT`) and hangs up.
  a closed port, for a proxy that is configured but unreachable.

WHAT IT NEEDS
-------------
Nothing new. The TLS certificate comes from `openssl`, the same command
CLAUDE.md's quick start has the user run; without it the HTTPS tests skip
rather than fail — a real check on this machine, not a new requirement for
anyone cloning the repo.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

# Every variable urllib or httpx reads to pick a proxy, in both cases (POSIX
# honours both; Windows folds them together). REQUEST_METHOD is here too:
# urllib ignores HTTP_PROXY when it is set (the httpoxy guard), which would
# make a proxy test pass for the wrong reason.
_PROXY_NAMES = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "request_method"}


def _json(value) -> bytes:
    return json.dumps(value).encode("utf-8")


class Endpoint:
    """A loopback HTTP(S) server that records every request and answers with
    `status` / `body` / `headers`, whatever the method or path."""

    def __init__(self, tls: ssl.SSLContext | None = None, host: str = "127.0.0.1"):
        self.requests: list[dict] = []
        self.host = host
        self.status = 200
        self.body: bytes = _json({})
        self.headers: dict[str, str] = {}
        self.scheme = "https" if tls else "http"
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length) if length else b"",
                })
                self.send_response(owner.status)
                for name, value in owner.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(owner.body)))
                self.end_headers()
                self.wfile.write(owner.body)

            do_GET = do_POST = do_PUT = _answer

            def log_message(self, *args):
                pass

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

            def finish_request(self, request, client_address):
                # The handshake happens here, on the connection's own
                # thread, so a client that never finishes one cannot stall
                # the accept loop.
                if tls is None:
                    return super().finish_request(request, client_address)
                with tls.wrap_socket(request, server_side=True) as wrapped:
                    super().finish_request(wrapped, client_address)

            def handle_error(self, request, client_address):
                # A refused handshake is a result the test asserts on, not
                # a traceback for the log.
                pass

        self._server = Server((host, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def netloc(self) -> str:
        """What a client names in `Host`: an IPv6 literal is bracketed."""
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}"

    def answer(self, status: int = 200, body=None, headers: dict | None = None) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else _json({} if body is None else body)
        self.headers = dict(headers or {})

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class Listener:
    """A proxy that is listening: it records the first bytes of every
    connection made to it, then hangs up without answering — or, with
    `hang_up=False`, a server that takes a request and never answers it.

    With `reply`, it sends those bytes, closes its own side, and waits (two
    seconds) for the CLIENT to close: `client_closed` gets one entry per
    connection, and `served` is set once one is recorded."""

    def __init__(self, hang_up: bool = True, reply: bytes | None = None):
        self.connections: list[bytes] = []
        self.client_closed: list[bool] = []
        self.served = threading.Event()
        self._hang_up = hang_up
        self._reply = reply
        self._held: list[socket.socket] = []
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            conn.settimeout(2.0)
            seen = b""
            try:
                while b"\r\n\r\n" not in seen and len(seen) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    seen += chunk
            except OSError:
                pass
            self.connections.append(seen)
            if self._reply is not None:
                self.client_closed.append(self._wait_for_the_client(conn))
                conn.close()
                self.served.set()
            elif self._hang_up:
                conn.close()
            else:
                self._held.append(conn)

    def _wait_for_the_client(self, conn: socket.socket) -> bool:
        try:
            conn.sendall(self._reply)
            conn.shutdown(socket.SHUT_WR)
            while conn.recv(4096):   # the request body, if any, then EOF
                pass
            return True
        except OSError:              # the two seconds ran out
            return False

    def close(self) -> None:
        self._sock.close()
        for conn in self._held:
            conn.close()


class Unreachable:
    """A proxy that is configured and not there: a port nothing listens on."""

    def __init__(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.connections: list[bytes] = []   # nothing can reach it to record

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        pass


def unproxied_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `base` (default: this process) with no proxy variable in it,
    so a test that is not about proxies does not depend on the developer's."""
    return {k: v for k, v in (os.environ if base is None else base).items()
            if k.lower() not in _PROXY_NAMES}


def proxied_env(proxy_url: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of `base` (default: this process) with every proxy variable
    pointing at `proxy_url` and NO_PROXY gone — the exact configuration in
    which urllib and httpx send a loopback request through the proxy."""
    env = unproxied_env(base)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = proxy_url
        if os.name != "nt":
            env[name.lower()] = proxy_url
    return env


def use_proxy(monkeypatch, proxy_url: str) -> None:
    """`proxied_env`, applied to this process for an in-process test."""
    for name in list(os.environ):
        if name.lower() in _PROXY_NAMES:
            monkeypatch.delenv(name, raising=False)
    for name, value in proxied_env(proxy_url, {}).items():
        monkeypatch.setenv(name, value)


def self_signed_pair(directory: Path) -> tuple[Path, Path] | None:
    """cert.pem / key.pem for CN=localhost, made by the command in CLAUDE.md's
    quick start. None when `openssl` is not on PATH."""
    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-nodes", "-subj", "/CN=localhost"],
                   check=True, capture_output=True, timeout=120)
    return cert, key


# --- fixtures ---------------------------------------------------------------
#
# Imported by the test modules that use them (`from tests.loopback_servers
# import ...`), the way `tests.dashboard_page` is.

_SERVER_TLS: list[ssl.SSLContext] = []


@pytest.fixture(scope="session")
def server_tls(tmp_path_factory) -> ssl.SSLContext:
    """One certificate for the whole run. Every module that imports this
    fixture gets a session fixture of its own, so the context is cached
    here rather than by pytest."""
    if not _SERVER_TLS:
        # tempfile, not `mktemp`: a numbered directory comes with a
        # `…current` symlink, which conftest turns into a skip on a Windows
        # account that may not make symlinks — and this is set up mid-test,
        # after that patch is on.
        directory = Path(tempfile.mkdtemp(prefix="loopback-tls-",
                                          dir=tmp_path_factory.getbasetemp()))
        pair = self_signed_pair(directory)
        if pair is None:
            pytest.skip("openssl is not on PATH, so there is no self-signed "
                        "certificate to serve HTTPS with")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(pair[0]), str(pair[1]))
        _SERVER_TLS.append(ctx)
    return _SERVER_TLS[0]


@pytest.fixture
def endpoint():
    """Stands in for JARVIS's own server, over plain HTTP."""
    server = Endpoint()
    yield server
    server.close()


@pytest.fixture
def tls_endpoint(server_tls):
    """Stands in for JARVIS's own server, over HTTPS with a self-signed
    certificate — CLAUDE.md's quick-start setup."""
    server = Endpoint(tls=server_tls)
    yield server
    server.close()


@pytest.fixture
def elsewhere():
    """Where a redirect points. Anything it records is a leak."""
    server = Endpoint()
    yield server
    server.close()


@pytest.fixture(params=["unreachable", "listening"])
def proxy(request):
    """A proxy the environment names and the call must not use: one that is
    not there (the call would fail), and one that is (the call would hand it
    the token, and it records what it was handed)."""
    stand_in = Unreachable() if request.param == "unreachable" else Listener()
    yield stand_in
    stand_in.close()
