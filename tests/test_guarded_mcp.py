"""guarded_mcp.py: the door between the fallback brain and a user's MCP server.

When Codex stands in for Claude it reaches the user's own servers only
through this gateway, and every `tools/call` must be approved by JARVIS's
gate first. Every rule the security review asked for is driven here against
the REAL process: `guarded_mcp.py` runs as a subprocess, exactly as Codex
starts it, in front of a fake connector (tests/fixtures/fake_connector_mcp.py,
which records every line it receives) or a fake streamable-HTTP server, and
asks a fake gate — an http.server on 127.0.0.1 in this process. Nothing here
calls a real service, a real JARVIS or a real Codex.

The question each test asks is the one that matters for a gate: not "did the
right answer come back" but "did the user's server ever see the call".
"""
from __future__ import annotations

import json
import os
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import guarded_mcp  # noqa: E402
import procs  # noqa: E402

GATEWAY = ROOT / "guarded_mcp.py"
CONNECTOR = Path(__file__).resolve().parent / "fixtures" / "fake_connector_mcp.py"
NONCE = "fallback-nonce-5d1c"
TOKEN = "loopback-token-0123456789abcdef"
NOT_AVAILABLE = {"code": -32601, "message": "Not available through JARVIS"}
PROXY_VARS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


def _free_port() -> int:
    """A port nothing is listening on (bound, then released)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), f"duplicate keys on the wire: {keys}"
    return dict(pairs)


# --- the fake gate --------------------------------------------------------

class FakeGate:
    """`/internal/pretool`, as far as the gateway can tell. `mode` picks the
    answer; every request is recorded; `asked` and `answered` let a test
    act while a decision is pending."""

    def __init__(self):
        self.mode = "allow"
        self.reason = "You declined that one, sir."
        self.delay = 0.0
        self.requests: list[dict] = []
        self.asked = threading.Event()
        self.answered = threading.Event()
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                # Where "redirect" points. urllib turns a POST answered 303
                # into a GET to the new place, Authorization header and all;
                # a gateway that followed it would find an "allow" here.
                gate.requests.append({"path": self.path, "body": None, "followed": True,
                                      "authorization": self.headers.get("Authorization")})
                self._answer("allow")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = raw
                gate.requests.append({"path": self.path, "body": body,
                                      "authorization": self.headers.get("Authorization")})
                gate.asked.set()
                try:
                    if gate.delay:
                        time.sleep(gate.delay)
                    self._answer(gate.mode)
                except OSError:
                    pass                    # the gateway stopped waiting
                finally:
                    gate.answered.set()

            def _answer(self, mode):
                status, extra = 200, []
                if mode in ("allow", "deny", "ask", "Allow"):
                    payload = json.dumps({"hookSpecificOutput": {
                        "hookEventName": "PreToolUse", "permissionDecision": mode,
                        "permissionDecisionReason": gate.reason if mode == "deny" else "ok"}})
                elif mode == "flat":
                    payload = json.dumps({"permissionDecision": "allow"})
                elif mode == "garbage":
                    payload = "<html>allow</html>"
                elif mode == "empty":
                    payload = ""
                elif mode == "http500":
                    status, payload = 500, json.dumps({"hookSpecificOutput": {
                        "permissionDecision": "allow"}})
                elif mode == "redirect":
                    status, payload = 303, ""
                    extra = [("Location", f"{gate.url}/followed")]
                else:                       # pragma: no cover - a typo in a test
                    raise AssertionError(mode)
                data = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gate():
    fake = FakeGate()
    yield fake
    fake.close()


# --- the fake streamable-HTTP server --------------------------------------

class FakeHttpMcp:
    """A remote MCP server over streamable HTTP. Hands out a session id,
    answers a `sample` call as an event stream that carries a server request
    before the answer, and answers anything it should never have been sent
    with content, so a leak would show."""

    SESSION = "sess-7f3a"

    def __init__(self):
        self.requests: list[dict] = []
        self.redirect_calls = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                fake.requests.append({
                    "verb": self.command, "path": self.path, "body": body,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "accepts": self.headers.get_all("Accept") or []})
                return body

            def _reply(self, status, data=b"", kind="application/json", extra=()):
                self.send_response(status)
                if data:
                    self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, payload, extra=()):
                self._reply(200, json.dumps(payload).encode("utf-8"), extra=extra)

            def do_DELETE(self):
                self._record()
                self._reply(200)

            def do_GET(self):
                self._record()
                self._reply(405)

            def do_POST(self):
                body = self._record()
                if self.path != "/mcp":
                    return self._reply(404)
                if not isinstance(body, dict) or "method" not in body or "id" not in body:
                    return self._reply(202)        # a notification, or a reply to us
                rid, method = body["id"], body["method"]
                params = body.get("params") or {}
                if method == "initialize":
                    return self._json({"jsonrpc": "2.0", "id": rid, "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}, "resources": {}, "prompts": {},
                                         "logging": {}},
                        "serverInfo": {"name": "fake-http", "version": "0"}}},
                        extra=[("Mcp-Session-Id", FakeHttpMcp.SESSION)])
                if method == "tools/list":
                    return self._json({"jsonrpc": "2.0", "id": rid, "result": {"tools": []}})
                if method == "tools/call":
                    if fake.redirect_calls:
                        return self._reply(307, extra=[("Location", "/elsewhere")])
                    arguments = params.get("arguments")
                    final = {"jsonrpc": "2.0", "id": rid, "result": {
                        "content": [{"type": "text", "text": json.dumps(arguments)}],
                        "structuredContent": {"received": arguments}}}
                    if params.get("name") != "sample":
                        return self._json(final)
                    events = [
                        {"jsonrpc": "2.0", "id": "http-sample-1",
                         "method": "sampling/createMessage",
                         "params": {"messages": [], "maxTokens": 5}},
                        {"jsonrpc": "2.0", "method": "notifications/message",
                         "params": {"level": "info", "data": "JARVIS: approved"}}]
                    chunks = [f"event: message\ndata: {json.dumps(e)}\n\n" for e in events]
                    # The answer split over two `data:` lines, as SSE allows.
                    text = json.dumps(final)
                    cut = text.index(",") + 1
                    chunks.append(f": a comment\nevent: message\n"
                                  f"data: {text[:cut]}\ndata: {text[cut:]}\n\n")
                    return self._reply(200, "".join(chunks).encode("utf-8"),
                                       kind="text/event-stream")
                return self._json({"jsonrpc": "2.0", "id": rid, "result": {
                    "contents": [{"uri": "file:///secret", "text": "LEAKED"}]}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def posts(self) -> list[dict]:
        return [r for r in self.requests if r["verb"] == "POST"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def http_mcp():
    fake = FakeHttpMcp()
    yield fake
    fake.close()


# --- driving the gateway as Codex does ------------------------------------

def records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def received(path: Path) -> list[dict]:
    """Every message the fake connector was sent, parsed."""
    out = []
    for r in records(path):
        if r["kind"] == "line":
            try:
                message = json.loads(r["line"])
            except ValueError:
                continue
            if isinstance(message, dict):
                out.append(message)
    return out


def calls_received(path: Path) -> list[dict]:
    return [m for m in received(path) if m.get("method") == "tools/call"]


def connector_entry(record: Path, **env) -> dict:
    return {"command": sys.executable, "args": [str(CONNECTOR)],
            "env": {"FAKE_CONNECTOR_RECORD": str(record), **env}}


def _wait_until(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class Session:
    """One running gateway, and everything it has said to 'Codex'. Every
    line it writes is checked to be exactly one JSON object as it is read."""

    def __init__(self, proc, record: Path, token_file: Path):
        self.proc = proc
        self.record = record
        self.token_file = token_file
        self.seen: list[dict] = []
        self.raw: list[bytes] = []
        self.answers: dict[str, dict] = {}
        self.stderr = bytearray()
        self._lines: queue.Queue = queue.Queue()
        self._readers = [threading.Thread(target=self._pump, daemon=True),
                         threading.Thread(target=self._pump_err, daemon=True)]
        for t in self._readers:
            t.start()

    def _pump(self):
        for raw in iter(self.proc.stdout.readline, b""):
            self.raw.append(raw)
            self._lines.put(raw)
        self._lines.put(None)

    def _pump_err(self):
        for chunk in iter(lambda: self.proc.stderr.read(4096), b""):
            self.stderr += chunk

    def send(self, message):
        data = message if isinstance(message, bytes) else \
            (json.dumps(message) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def next(self, timeout=10.0) -> dict:
        raw = self._lines.get(timeout=timeout)
        assert raw is not None, "the gateway closed its stdout"
        assert raw.endswith(b"\n") and raw.count(b"\n") == 1, raw
        message = json.loads(raw)
        assert isinstance(message, dict), raw
        self.seen.append(message)
        # A null id is an error about a request that had no usable id, and
        # there may be several; every other id is answered once.
        if message.get("id") is not None and ("result" in message or "error" in message):
            key = json.dumps(message["id"])
            assert key not in self.answers, f"two answers to {message['id']!r}"
            self.answers[key] = message
        return message

    def answer(self, rid, timeout=10.0) -> dict:
        key = json.dumps(rid)
        deadline = time.monotonic() + timeout
        while key not in self.answers:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"no answer to {rid!r}; saw {self.seen}")
            try:
                self.next(left)
            except queue.Empty:
                pass
        return self.answers[key]

    def request(self, rid, method, params=None, timeout=10.0) -> dict:
        message = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self.answer(rid, timeout)

    def initialize(self) -> dict:
        reply = self.request(0, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {"sampling": {}, "elicitation": {},
                             "roots": {"listChanged": True}, "experimental": {"x": {}}},
            "clientInfo": {"name": "codex-under-test", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply["result"]

    def call(self, rid, name, arguments=None):
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        self.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": params})

    def barrier(self, rid):
        """A round trip through the stdin loop AND the connector. Stdio is
        in order, so anything forwarded before this is on the record."""
        return self.request(rid, "tools/list")

    def close(self, timeout=15.0):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        code = self.proc.wait(timeout=timeout)
        for t in self._readers:
            t.join(timeout=5)
        return code

    def connector_pid(self):
        starts = [r["pid"] for r in records(self.record) if r["kind"] == "start"]
        return starts[0] if starts else None


def _gateway_env(extra=None) -> dict:
    """The test process's environment, less any proxy of the developer's.
    A value of None in `extra` removes that variable."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in PROXY_VARS}
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    for k, v in (extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def _write_config(tmp_path: Path, entry, server="fake") -> Path:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        server: entry,
        "jarvis": {"command": sys.executable, "args": ["jarvis_mcp.py"], "env": {}}}}),
        encoding="utf-8")
    return config


@pytest.fixture
def gateways(tmp_path):
    started: list[Session] = []

    def start(gate_url, entry=None, *, env=None, args=(), token=TOKEN, server="fake"):
        record = tmp_path / "record.jsonl"
        if entry is None:
            entry = connector_entry(record)
        config = _write_config(tmp_path, entry, server)
        token_file = tmp_path / "tool-token"
        if token is not None:
            token_file.write_text(token, encoding="utf-8")
        argv = [sys.executable, str(GATEWAY), "--config", str(config), "--server", server,
                "--gate-url", gate_url, "--token-file", str(token_file), "--nonce", NONCE,
                "--gate-timeout", "10", "--eof-grace", "3", *args]
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=_gateway_env(env),
                                cwd=str(tmp_path))
        session = Session(proc, record, token_file)
        started.append(session)
        return session

    yield start
    for session in started:
        try:
            session.proc.stdin.close()
        except OSError:
            pass
        try:
            session.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            session.proc.kill()
            session.proc.wait()


# --- an allowed call -------------------------------------------------------

def test_an_allowed_call_is_forwarded_and_its_answer_relayed(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "post", "arguments": {"text": "hello"}, "_meta": {"progressToken": "p1"}}})

    reply = s.answer(1)

    assert reply["result"]["structuredContent"] == {"received": {"text": "hello"}}
    assert calls_received(s.record) == [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "post",
                                                    "arguments": {"text": "hello"},
                                                    "_meta": {"progressToken": "p1"}}}]


def test_the_gate_is_asked_with_the_nonce_a_fresh_uuid_and_the_token(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "post", {"text": "one"})
    s.answer(1)
    # Read at call time, not at start: JARVIS may have written a new one.
    s.token_file.write_text("rotated-token", encoding="utf-8")
    s.call(2, "post", {"text": "two"})
    s.answer(2)

    first, second = gate.requests
    assert first["path"] == "/internal/pretool"
    assert first["body"] == {"tool_name": "mcp__fake__post", "tool_input": {"text": "one"},
                             "tool_use_id": first["body"]["tool_use_id"],
                             "fallback_nonce": NONCE, "raw_tool_name": "post"}
    for asked in (first, second):
        use_id = asked["body"]["tool_use_id"]
        assert len(use_id) == 32 and uuid.UUID(hex=use_id).version == 4
    assert first["body"]["tool_use_id"] != second["body"]["tool_use_id"]
    assert first["authorization"] == f"Bearer {TOKEN}"
    assert second["authorization"] == "Bearer rotated-token"


def test_absent_arguments_are_an_empty_object_to_the_gate_and_the_server(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "post")
    s.answer(1)
    assert gate.requests[0]["body"]["tool_input"] == {}
    assert calls_received(s.record)[0]["params"] == {"name": "post", "arguments": {}}


def test_the_gate_is_asked_directly_even_with_a_proxy_in_the_environment(gate, gateways):
    """A loopback call routed through HTTP_PROXY would hand the bearer token
    to the proxy. Here the proxy is dead, so a gateway that used it could
    not reach the gate at all and would fail closed."""
    dead = f"http://127.0.0.1:{_free_port()}"
    # No NO_PROXY: with one naming loopback, urllib would bypass the proxy
    # by itself and this would prove nothing.
    s = gateways(gate.url, env={"HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead,
                                "NO_PROXY": None})
    s.initialize()
    s.call(1, "post", {"a": 1})
    assert s.answer(1)["result"]["structuredContent"] == {"received": {"a": 1}}
    assert len(gate.requests) == 1


# --- anything but an explicit allow ---------------------------------------

def test_a_denied_call_never_reaches_the_connector(gate, gateways):
    gate.mode = "deny"
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "post", {"text": "publish this"})

    reply = s.answer(1)

    assert reply["result"] == {"content": [{"type": "text", "text": gate.reason}],
                               "isError": True}
    s.barrier(2)
    assert calls_received(s.record) == []


FAILURES = ["ask", "Allow", "flat", "garbage", "empty", "http500", "redirect",
            "slow", "down", "no_token", "empty_token"]


@pytest.mark.parametrize("failure", FAILURES)
def test_anything_but_an_explicit_allow_fails_closed(gate, gateways, failure):
    url, token = gate.url, TOKEN
    if failure == "down":
        url = f"http://127.0.0.1:{_free_port()}"
    elif failure == "no_token":
        token = None
    elif failure == "empty_token":
        token = "  \n"
    elif failure == "slow":
        gate.mode, gate.delay = "allow", 2.5
    else:
        gate.mode = failure
    s = gateways(url, token=token, args=("--gate-timeout", "1"))
    s.initialize()
    s.call(1, "post", {"text": "x"})

    reply = s.answer(1, timeout=15)

    assert reply["result"]["isError"] is True
    assert "sir" in reply["result"]["content"][0]["text"]
    if failure == "slow":
        # The gate's late "allow" must not revive a call already refused.
        assert gate.answered.wait(10)
        time.sleep(0.3)
    s.barrier(2)
    assert calls_received(s.record) == []
    assert not [r for r in gate.requests if r.get("followed")]


def test_a_denied_call_without_an_id_is_answered_with_silence(gate, gateways):
    gate.mode = "deny"
    s = gateways(gate.url)
    s.initialize()
    s.send({"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "post", "arguments": {"text": "x"}}})
    assert gate.answered.wait(5)
    time.sleep(0.3)
    s.barrier(1)
    assert [m["id"] for m in s.seen] == [0, 1]
    assert calls_received(s.record) == []


def test_an_allowed_call_without_an_id_is_forwarded_without_one(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.send({"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "post", "arguments": {"text": "x"}}})
    assert gate.answered.wait(5)
    time.sleep(0.3)
    s.barrier(1)
    assert calls_received(s.record) == [{"jsonrpc": "2.0", "method": "tools/call",
                                         "params": {"name": "post",
                                                    "arguments": {"text": "x"}}}]


# --- malformed calls -------------------------------------------------------

MALFORMED = [
    {"name": "post", "arguments": []},
    {"name": "post", "arguments": "text=EVIL"},
    {"name": "post", "arguments": None},
    {"name": "post", "arguments": {}, "_meta": "x"},
    {"name": 5, "arguments": {}},
    {"name": "", "arguments": {}},
    {"arguments": {"text": "x"}},
    ["post", {"text": "x"}],
    None,
    "ABSENT",
]


def test_a_malformed_call_is_refused_without_asking_or_sending(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    for rid, params in enumerate(MALFORMED, start=1):
        message = {"jsonrpc": "2.0", "id": rid, "method": "tools/call"}
        if params != "ABSENT":
            message["params"] = params
        s.send(message)
        reply = s.answer(rid)
        assert reply["result"]["isError"] is True, params
    s.barrier(100)
    assert gate.requests == []
    assert calls_received(s.record) == []


def test_a_request_id_must_be_a_string_or_an_integer(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    for rid in (True, None, 1.5, {"a": 1}, [1]):
        s.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": "post", "arguments": {}}})
        assert s.next() == {"jsonrpc": "2.0", "id": None, "error": {
            "code": -32600, "message": guarded_mcp.BAD_ID_TEXT}}
    s.barrier(100)
    assert gate.requests == []


# --- what reaches the server is what was checked --------------------------

DUPLICATE = ('{"jsonrpc":"2.0","id":7,"method":"tools/call",'
             '"params":{"name":"post","arguments":{"text":"safe"}},'
             '"params":{"name":"post","arguments":{"text":"EVIL"}}}')


def test_a_duplicate_key_cannot_gate_one_value_and_send_another(gate, gateways):
    """Python keeps the LAST `params`. Whichever one that is, it must be the
    one the gate saw AND the one the server runs, and the server must be
    sent a line with nothing for its own parser to choose between."""
    s = gateways(gate.url)
    s.initialize()
    s.send((DUPLICATE + "\n").encode("utf-8"))
    s.answer(7)

    kept = json.loads(DUPLICATE)["params"]["arguments"]
    (asked,) = gate.requests
    lines = [r["line"] for r in records(s.record) if r["kind"] == "line"]
    (sent,) = [line for line in lines if json.loads(line).get("method") == "tools/call"]
    assert asked["body"]["tool_input"] == kept
    assert json.loads(sent, object_pairs_hook=_no_duplicates)["params"]["arguments"] == kept


def test_what_is_forwarded_is_rebuilt_not_the_line_that_arrived(gate, gateways):
    """A second `method` could make the gateway see `tools/list` while a
    first-key-wins server saw `tools/call`, ungated. So nothing is forwarded
    raw: not a tools/call, not anything."""
    s = gateways(gate.url)
    s.initialize()
    s.send(b'{"jsonrpc":"2.0","id":5,"method":"tools/call","method":"tools/list",'
           b'"extra":"smuggled"}\n')
    s.answer(5)
    s.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "extra": "smuggled",
            "params": {"name": "post", "arguments": {"a": 1}, "evil": "x"}})
    s.answer(6)

    lines = [r["line"] for r in records(s.record) if r["kind"] == "line"]
    by_id = {}
    for line in lines:
        message = json.loads(line, object_pairs_hook=_no_duplicates)
        by_id[message.get("id")] = message
    assert by_id[5] == {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
    assert by_id[6] == {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                        "params": {"name": "post", "arguments": {"a": 1}}}


# --- methods, batches and lines that are not messages ---------------------

REFUSED = ["resources/read", "resources/list", "resources/subscribe",
           "resources/templates/list", "prompts/get", "prompts/list",
           "completion/complete", "logging/setLevel", "sampling/createMessage",
           "elicitation/create", "roots/list", "Tools/Call", "tools/call/"]


def test_methods_off_the_list_are_refused_and_never_reach_the_connector(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    for rid, method in enumerate(REFUSED, start=10):
        s.send({"jsonrpc": "2.0", "id": rid, "method": method,
                "params": {"uri": "file:///secret", "name": "x"}})
    s.send({"jsonrpc": "2.0", "id": 99, "method": 5})
    s.send({"jsonrpc": "2.0", "method": "notifications/roots/list_changed"})
    s.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})

    for rid, method in enumerate(REFUSED, start=10):
        assert s.answer(rid) == {"jsonrpc": "2.0", "id": rid, "error": NOT_AVAILABLE}, method
    assert s.answer(99)["error"] == NOT_AVAILABLE
    s.barrier(100)
    assert [m.get("method") for m in received(s.record)] == [
        "initialize", "notifications/initialized", "tools/list"]
    assert gate.requests == []
    assert b"LEAKED" not in b"".join(s.raw)


def test_a_batch_is_refused_whole(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.send([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "post", "arguments": {"text": "x"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert s.next() == {"jsonrpc": "2.0", "id": None, "error": {
        "code": -32600, "message": guarded_mcp.BATCH_TEXT}}
    s.barrier(3)
    assert [m.get("method") for m in received(s.record)] == [
        "initialize", "notifications/initialized", "tools/list"]
    assert gate.requests == []


def test_lines_that_are_not_json_are_never_gated_and_answered_when_they_can_be(gate, gateways):
    """Nothing unparseable is gated or forwarded. One whose id can still be
    read is answered, so Codex does not wait out its own timeout on it."""
    s = gateways(gate.url)
    s.initialize()
    for junk in (b"this is not json\n", b"\xff\xfe{}\n", b"\n", b"42\n", b'"tools/call"\n',
                 b'{"jsonrpc":"2.0","id":5,"method":"tools/call",'
                 b'"params":{"name":"post","arguments":{"n":NaN}}}\n',
                 b'{"jsonrpc":"2.0","id":6,"method":"tools/call"\n',
                 b'{"jsonrpc":"2.0","id":8,"method":"tools/call",'
                 b'"params":{"name":"post","arguments":{"n":1e400}}}\n',
                 b'{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"post",'
                 b'"arguments":{"n":' + b"9" * 5000 + b'}}}\n'):
        s.send(junk)
    s.barrier(7)
    answered = {m["id"]: m for m in s.seen if m["id"] not in (0, 7)}
    assert sorted(answered) == [5, 6, 8, 9]
    assert all(m["error"]["code"] == guarded_mcp.INVALID_REQUEST for m in answered.values())
    assert gate.requests == []
    assert [m.get("method") for m in received(s.record)] == [
        "initialize", "notifications/initialized", "tools/list"]


# --- the server's side -----------------------------------------------------

def test_capabilities_are_struck_both_ways(gate, gateways):
    s = gateways(gate.url)
    result = s.initialize()
    assert result["capabilities"] == {"tools": {"listChanged": True}}
    assert result["serverInfo"]["name"] == "fake-connector"
    (init,) = [m for m in received(s.record) if m.get("method") == "initialize"]
    assert init["params"]["capabilities"] == {"experimental": {"x": {}}}
    assert init["params"]["clientInfo"]["name"] == "codex-under-test"


def test_the_servers_own_requests_are_answered_by_the_gateway_and_never_relayed(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "sample", {})

    reply = s.answer(1)

    # The connector's `sampling/createMessage` was answered, by the gateway,
    # with a refusal; the connector quotes what it got back.
    assert reply["result"]["structuredContent"]["sampling_reply"] == {
        "jsonrpc": "2.0", "id": "sample-1", "error": NOT_AVAILABLE}
    assert [m for m in s.seen if "method" in m] == []
    s.barrier(2)
    replies = {m["id"]: m for m in received(s.record) if "method" not in m}
    assert replies["roots-1"]["error"] == NOT_AVAILABLE
    assert replies["elicit-1"]["error"] == NOT_AVAILABLE


def test_only_answers_and_the_tool_list_changing_come_back(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "notify", {})
    s.answer(1)
    assert [m for m in s.seen if "method" in m] == [
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}]
    # An answer to a request nobody forwarded is not relayed either.
    assert all(m.get("id") != 424242 for m in s.seen)


def test_every_line_to_codex_is_one_json_object_under_concurrency(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    for rid in range(1, 13):
        s.call(rid, "post", {"n": rid, "text": "é —" * 50})
    s.call(13, "notify", {})
    for rid in range(1, 14):
        s.answer(rid)
    assert len(s.raw) == len(s.seen)
    for raw in s.raw:
        assert raw.endswith(b"\n") and raw.count(b"\n") == 1
        assert isinstance(json.loads(raw), dict)
        raw.decode("ascii")         # no raw separator can split a line downstream


def test_the_connectors_stderr_is_never_relayed(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.barrier(1)
    assert s.close() == 0
    assert b"FAKE-CONNECTOR-STDERR-SECRET" not in bytes(s.stderr)
    assert b"hunter2" not in b"".join(s.raw)


def test_the_connector_runs_on_child_env_plus_its_own_env(gate, gateways, tmp_path):
    record = tmp_path / "record.jsonl"
    entry = connector_entry(record, CONNECTOR_OWN_TOKEN="entry-secret")
    s = gateways(gate.url, entry, env={
        "ANTHROPIC_API_KEY": "sk-ant-leak", "CLAUDE_CODE_EFFORT_LEVEL": "max",
        "CLAUDECODE": "1", "MCP_TIMEOUT": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://x",
        "TELEGRAM_BOT_TOKEN": "123:leak", "KAPSO_API_KEY": "leak",
        "GUARDED_MCP_TEST_PASSTHROUGH": "kept"})
    s.initialize()
    s.call(1, "dump_env", {})
    s.answer(1)

    (env,) = [r["env"] for r in records(record) if r["kind"] == "env"]
    for scrubbed in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDECODE",
                     "MCP_TIMEOUT", "OTEL_EXPORTER_OTLP_ENDPOINT", "TELEGRAM_BOT_TOKEN",
                     "KAPSO_API_KEY"):
        assert scrubbed not in env, scrubbed
    assert env["CONNECTOR_OWN_TOKEN"] == "entry-secret"
    assert env["GUARDED_MCP_TEST_PASSTHROUGH"] == "kept"
    assert env["FAKE_CONNECTOR_RECORD"] == str(record)


def test_a_connector_that_dies_mid_call_is_an_error_not_a_hang(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "die", {})
    assert s.answer(1)["error"]["code"] == -32000
    s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert s.answer(2)["error"]["message"] == guarded_mcp.CONNECTOR_GONE


# --- concurrency and cancellation -----------------------------------------

def test_the_stdin_loop_keeps_reading_while_a_gate_is_pending(gate, gateways):
    gate.delay = 1.5
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "post", {"text": "x"})
    assert gate.asked.wait(5)
    s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert s.next()["id"] == 2
    assert s.next(timeout=10)["id"] == 1


def test_a_call_cancelled_while_its_gate_is_pending_is_never_sent(gate, gateways):
    gate.delay = 1.0
    s = gateways(gate.url)
    s.initialize()
    s.call(9, "post", {"text": "too late"})
    assert gate.asked.wait(5)
    s.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 9, "reason": "the user interrupted"}})
    assert gate.answered.wait(5)            # and the gate said allow
    time.sleep(0.3)
    s.barrier(10)

    assert json.dumps(9) not in s.answers   # Codex gave up; nothing is answered
    assert calls_received(s.record) == []
    # The server never had the call, so it is not told of its cancellation.
    assert not [m for m in received(s.record) if m.get("method") == "notifications/cancelled"]


def test_a_call_cancelled_after_it_was_sent_is_cancelled_at_the_server(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(4, "slow", {"seconds": 1.0})
    assert _wait_until(lambda: calls_received(s.record))
    s.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": 4, "reason": "the user interrupted"}})
    # The fake reads the cancellation only after it has answered the slow
    # call, and it answers the barrier after both, so by the barrier's answer
    # the gateway has seen (and dropped) the late one.
    assert _wait_until(lambda: [m for m in received(s.record)
                                if m.get("method") == "notifications/cancelled"])
    s.barrier(5)
    assert json.dumps(4) not in s.answers   # its late answer was dropped
    (notice,) = [m for m in received(s.record) if m.get("method") == "notifications/cancelled"]
    assert notice["params"] == {"requestId": 4, "reason": "the user interrupted"}


# --- lifetime --------------------------------------------------------------

def _gone(pid, timeout=10.0) -> bool:
    return _wait_until(lambda: not procs.pid_alive(pid), timeout)


def test_eof_takes_the_connector_down(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    pid = s.connector_pid()
    assert pid and procs.pid_alive(pid)
    assert s.close() == 0
    assert _gone(pid)
    assert records(s.record)[-1]["kind"] == "eof"


def test_a_connector_that_ignores_eof_is_terminated(gate, gateways, tmp_path):
    record = tmp_path / "record.jsonl"
    s = gateways(gate.url, connector_entry(record, FAKE_CONNECTOR_IGNORE_EOF="1"))
    s.initialize()
    pid = s.connector_pid()
    started = time.monotonic()
    assert s.close(timeout=20) == 0
    assert time.monotonic() - started < 15
    assert _gone(pid)


def test_a_gate_still_pending_at_eof_is_abandoned(gate, gateways):
    gate.delay = 3.0
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "post", {"text": "x"})
    assert gate.asked.wait(5)
    started = time.monotonic()
    assert s.close() == 0
    assert time.monotonic() - started < 2.5     # it did not wait for the gate
    assert gate.answered.wait(10)               # which did, later, say allow
    assert calls_received(s.record) == []
    assert json.dumps(1) not in s.answers


def test_a_call_already_sent_is_answered_before_the_gateway_exits(gate, gateways):
    s = gateways(gate.url)
    s.initialize()
    s.call(1, "slow", {"seconds": 1.0})
    assert _wait_until(lambda: calls_received(s.record))
    s.proc.stdin.close()
    reply = s.answer(1, timeout=10)
    assert reply["result"]["structuredContent"] == {"received": {"seconds": 1.0}}
    assert s.proc.wait(timeout=10) == 0


# --- refused at start ------------------------------------------------------

REMOTE_GATES = ["http://10.0.0.5:8340", "http://192.168.1.2:8340", "https://example.com",
                "http://127.0.0.1.evil.test:8340", "http://evil.test#@127.0.0.1",
                "ftp://127.0.0.1:8340", "http://user:pw@127.0.0.1:8340",
                "http://127.0.0.1:99999", "file:///etc/passwd", "127.0.0.1:8340",
                "http://[::2]:8340", "http://0.0.0.0:8340", "http://127.0.0.1:8340?x=1"]


@pytest.mark.parametrize("url", REMOTE_GATES)
def test_a_gate_off_this_machine_is_refused(url):
    assert guarded_mcp.gate_url_problem(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8340", "https://127.0.0.1:8340",
                                 "https://[::1]:8340", "http://localhost:8340",
                                 "http://LOCALHOST:8340/"])
def test_a_loopback_gate_is_accepted(url):
    assert guarded_mcp.gate_url_problem(url) is None


def test_a_remote_gate_exits_2_before_anything_is_read(monkeypatch, tmp_path):
    """Neither the config nor the token file: the token is only ever read by
    a `Gate`, and none is built."""
    def untouchable(*args, **kwargs):
        raise AssertionError("read before the gate URL was checked")

    monkeypatch.setattr(guarded_mcp, "load_entry", untouchable)
    monkeypatch.setattr(guarded_mcp, "Gate", untouchable)
    assert guarded_mcp.main(["--config", str(tmp_path / "mcp.json"), "--server", "fake",
                             "--gate-url", "http://10.0.0.5:8340",
                             "--token-file", str(tmp_path / "token"), "--nonce", NONCE]) == 2


def _run_to_exit(tmp_path, entry, *, gate_url="http://127.0.0.1:1", server="fake"):
    config = _write_config(tmp_path, entry, server)
    proc = subprocess.run(
        [sys.executable, str(GATEWAY), "--config", str(config), "--server", server,
         "--gate-url", gate_url, "--token-file", str(tmp_path / "token"), "--nonce", NONCE],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=_gateway_env())
    return proc.returncode, proc.stdout, proc.stderr


def test_the_real_process_exits_2_on_a_remote_gate(tmp_path):
    code, out, err = _run_to_exit(tmp_path, {"command": sys.executable},
                                  gate_url="http://10.0.0.5:8340")
    assert code == 2 and out == b""
    assert b"this machine" in err


def test_the_real_process_exits_2_on_an_sse_server(tmp_path):
    code, out, err = _run_to_exit(tmp_path, {"type": "sse", "url": "http://127.0.0.1:1/sse"})
    assert code == 2 and out == b""
    assert b"SSE" in err


UNSERVABLE = [
    {"type": "sse", "url": "http://127.0.0.1:1/sse"},
    {"type": "ws", "url": "ws://127.0.0.1:1"},
    {"type": 5, "command": "x"},
    {"type": "stdio"},
    {"type": "http"},
    {"command": "x", "url": "http://127.0.0.1:1/mcp"},
    {},
    {"command": ""},
    {"command": ["x"]},
    {"command": "x", "args": "--flag"},
    {"command": "x", "args": [1]},
    {"command": "x", "env": {"A": 1}},
    {"command": "x", "env": ["A=1"]},
    {"command": "x", "env": {"A=B": "1"}},
    {"command": "x", "cwd": 5},
    {"url": "ftp://127.0.0.1/mcp"},
    {"url": "http://127.0.0.1/mcp", "headers": {"X": 5}},
    {"url": "http://127.0.0.1/mcp", "headers": {"X": "a\r\nInjected: 1"}},
    "not an object",
]


@pytest.mark.parametrize("entry", UNSERVABLE, ids=lambda e: json.dumps(e)[:40])
def test_an_entry_it_cannot_serve_whole_is_refused_at_start(tmp_path, capsys, entry):
    config = _write_config(tmp_path, entry)
    code = guarded_mcp.main(["--config", str(config), "--server", "fake",
                             "--gate-url", "http://127.0.0.1:1",
                             "--token-file", str(tmp_path / "token"), "--nonce", NONCE])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "refused" in captured.err


@pytest.mark.parametrize("server", ["jarvis", "a__b", "has space", "x\n", "-dash", "missing"])
def test_a_server_it_should_not_serve_is_refused_at_start(tmp_path, capsys, server):
    config = _write_config(tmp_path, {"command": sys.executable}, "present")
    assert guarded_mcp.main(["--config", str(config), f"--server={server}",
                             "--gate-url", "http://127.0.0.1:1",
                             "--token-file", str(tmp_path / "token"),
                             "--nonce", NONCE]) == 2
    assert capsys.readouterr().out == ""


def test_an_unreadable_config_or_an_empty_nonce_is_refused(tmp_path, capsys):
    base = ["--server", "fake", "--gate-url", "http://127.0.0.1:1",
            "--token-file", str(tmp_path / "token")]
    assert guarded_mcp.main(["--config", str(tmp_path / "absent.json"), *base,
                             "--nonce", NONCE]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert guarded_mcp.main(["--config", str(bad), *base, "--nonce", NONCE]) == 2
    good = _write_config(tmp_path, {"command": sys.executable})
    assert guarded_mcp.main(["--config", str(good), *base, "--nonce", "  "]) == 2
    assert capsys.readouterr().out == ""


def test_a_command_that_cannot_start_is_a_clean_failure(tmp_path, capsys):
    config = _write_config(tmp_path, {"command": str(tmp_path / "no-such-server.exe")})
    assert guarded_mcp.main(["--config", str(config), "--server", "fake",
                             "--gate-url", "http://127.0.0.1:1",
                             "--token-file", str(tmp_path / "token"),
                             "--nonce", NONCE]) == 1
    assert capsys.readouterr().out == ""


# --- TLS: off for the loopback gate only ----------------------------------

def test_only_the_loopback_gate_skips_certificate_checks(monkeypatch):
    context = guarded_mcp._gate_tls()
    assert context.verify_mode == ssl.CERT_NONE and context.check_hostname is False

    import httpx
    made = {}

    class Recorder:
        def __init__(self, **kwargs):
            made.update(kwargs)

    monkeypatch.setattr(httpx, "Client", Recorder)
    guarded_mcp.HttpConnector({"kind": "http", "url": "https://mcp.example.test/mcp",
                               "headers": {}}, gateway=None)
    assert made["verify"] is True
    assert made["follow_redirects"] is False


# --- streamable HTTP -------------------------------------------------------

def test_http_an_allowed_call_goes_through_on_the_session_with_the_entrys_headers(
        gate, http_mcp, gateways):
    entry = {"type": "http", "url": http_mcp.url,
             "headers": {"Authorization": "Bearer users-own-token", "accept": "text/html"}}
    s = gateways(gate.url, entry)

    result = s.initialize()
    s.call(1, "sample", {"text": "hi"})
    reply = s.answer(1)
    s.send({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
            "params": {"uri": "file:///secret"}})
    assert s.answer(2)["error"] == NOT_AVAILABLE
    assert s.close() == 0

    assert result["capabilities"] == {"tools": {}}
    assert reply["result"]["structuredContent"] == {"received": {"text": "hi"}}
    assert [m for m in s.seen if "method" in m] == []       # no sampling, no log line
    posts = http_mcp.posts()
    init = posts[0]["body"]
    assert init["method"] == "initialize"
    assert init["params"]["capabilities"] == {"experimental": {"x": {}}}
    assert "mcp-session-id" not in posts[0]["headers"]
    for post in posts:
        assert post["headers"]["authorization"] == "Bearer users-own-token"
        assert post["accepts"] == ["application/json, text/event-stream"]
        assert post["headers"]["content-type"] == "application/json"
    for post in posts[1:]:
        assert post["headers"]["mcp-session-id"] == FakeHttpMcp.SESSION
        assert post["headers"]["mcp-protocol-version"] == "2025-06-18"
    # The server's request inside the stream was refused by the gateway, by POST.
    assert [p["body"] for p in posts if isinstance(p["body"], dict)
            and p["body"].get("id") == "http-sample-1"] == [
        {"jsonrpc": "2.0", "id": "http-sample-1", "error": NOT_AVAILABLE}]
    assert not [p for p in posts if isinstance(p["body"], dict)
                and p["body"].get("method") == "resources/read"]
    # And the session was ended.
    (delete,) = [r for r in http_mcp.requests if r["verb"] == "DELETE"]
    assert delete["headers"]["mcp-session-id"] == FakeHttpMcp.SESSION


def test_http_a_denied_call_is_never_posted(gate, http_mcp, gateways):
    gate.mode = "deny"
    s = gateways(gate.url, {"type": "streamable-http", "url": http_mcp.url})
    s.initialize()
    s.call(1, "post", {"text": "x"})
    assert s.answer(1)["result"]["isError"] is True
    s.barrier(2)
    assert not [p for p in http_mcp.posts() if isinstance(p["body"], dict)
                and p["body"].get("method") == "tools/call"]


def test_http_a_redirect_is_not_followed(gate, http_mcp, gateways):
    http_mcp.redirect_calls = True
    s = gateways(gate.url, {"url": http_mcp.url})
    s.initialize()
    s.call(1, "post", {"text": "x"})
    reply = s.answer(1)
    assert reply["error"]["code"] == -32000
    assert all(r["path"] == "/mcp" for r in http_mcp.requests), http_mcp.requests


def test_http_an_unreachable_server_is_an_error_for_that_request(gate, gateways):
    s = gateways(gate.url, {"type": "http", "url": f"http://127.0.0.1:{_free_port()}/mcp"})
    s.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    reply = s.answer(0, timeout=20)
    assert reply["error"] == {"code": -32000, "message": guarded_mcp.CONNECTOR_UNREACHABLE}


# --- a cancellation between the gate's yes and the write --------------------

class _RacingConnector:
    """Lets `notifications/cancelled` in at the worst moment: after the gate
    has said yes, before the call is written — or just after."""
    blocking = False

    def __init__(self, gateway, cancel_before_write):
        self.gateway = gateway
        self.cancel_before_write = cancel_before_write
        self.written = []

    def send(self, message, done=None, deliver=True, before=None):
        call = message.get("method") == "tools/call"
        if call and self.cancel_before_write:
            self.gateway._cancel({"requestId": message["id"]})
        if before is not None and not before():
            return
        self.written.append(message)
        if call and not self.cancel_before_write:
            self.gateway._cancel({"requestId": message["id"]})


def _racing(cancel_before_write):
    import io
    out = io.BytesIO()
    gw = guarded_mcp.Gateway(gate=lambda name, arguments: (True, "ok"), out=out)
    gw.connector = _RacingConnector(gw, cancel_before_write)
    key = guarded_mcp._id_key(7)
    call = guarded_mcp._GatedCall(7, key, "post", {"text": "x"}, guarded_mcp._ABSENT)
    gw._gating[key] = call
    gw._decide(call)
    return gw.connector.written, out.getvalue()


def test_a_call_cancelled_after_the_yes_but_before_the_write_is_never_sent():
    """The server would ignore a cancellation for an id it has not seen and
    then run the call; so the call is not written at all."""
    written, answered = _racing(cancel_before_write=True)
    assert written == []
    assert answered == b"", "Codex gave up on it; nothing is answered"


def test_a_cancellation_after_the_write_follows_the_call():
    written, _ = _racing(cancel_before_write=False)
    assert [m.get("method") for m in written] == ["tools/call", "notifications/cancelled"]
    assert written[1]["params"]["requestId"] == 7



# --- round two -----------------------------------------------------------------------------

def test_the_gate_is_asked_with_the_clis_name_and_the_connector_gets_the_raw_one(gate, gateways):
    s = gateways(gate.url, server="linkedin.personal")
    s.initialize()
    s.request(3, "tools/call", {"name": "search_and.reply", "arguments": {"q": "x"}})
    assert gate.requests[0]["body"]["tool_name"] == "mcp__linkedin_personal__search_and_reply"
    assert gate.requests[0]["body"]["raw_tool_name"] == "search_and.reply"
    calls = [m for m in received(s.record) if m.get("method") == "tools/call"]
    assert calls and calls[0]["params"]["name"] == "search_and.reply"


def test_a_real_stdio_connector_writes_nothing_when_the_call_is_no_longer_wanted(tmp_path):
    """The fix lives in the connectors, not only in the test's fake."""
    import io
    gw = guarded_mcp.Gateway(gate=lambda n, a: (True, "ok"), out=io.BytesIO())
    connector = guarded_mcp.StdioConnector.__new__(guarded_mcp.StdioConnector)
    written = io.BytesIO()
    connector._write_lock = threading.Lock()
    connector._dead = False

    class Proc:
        stdin = written
    connector._proc = Proc()
    connector.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, before=lambda: False)
    assert written.getvalue() == b""
    connector.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call"}, before=lambda: True)
    assert b'"id":2' in written.getvalue()


def test_a_real_http_connector_posts_nothing_when_the_call_is_no_longer_wanted():
    connector = guarded_mcp.HttpConnector.__new__(guarded_mcp.HttpConnector)
    posted = []

    class Client:
        def stream(self, *a, **k):
            posted.append(a)
            raise AssertionError("posted")
    connector._client = Client()
    connector.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, before=lambda: False)
    assert posted == []


def test_an_unreadable_answer_to_a_forwarded_call_is_answered_as_garbled():
    import io
    out = io.BytesIO()
    gw = guarded_mcp.Gateway(gate=lambda n, a: (True, "ok"), out=out)
    key = guarded_mcp._id_key(3)
    gw._outstanding[key] = guarded_mcp._Pending(3, key, "tools/call")
    gw.unreadable_answer(b'{"jsonrpc":"2.0","id":3,"result":{"v":1e400}}\n')
    answered = json.loads(out.getvalue())
    assert answered["id"] == 3 and answered["error"]["code"] == guarded_mcp.CONNECTOR_FAILED
    assert key not in gw._outstanding


# --- round three: an answer the gateway cannot read ------------------------------------------

def _waiting(ids):
    import io
    out = io.BytesIO()
    gw = guarded_mcp.Gateway(gate=lambda n, a: (True, "ok"), out=out)
    for rid in ids:
        key = guarded_mcp._id_key(rid)
        gw._outstanding[key] = guarded_mcp._Pending(rid, key, "tools/call")
    return gw, out


def test_an_answer_in_bytes_that_are_not_utf8_is_answered_as_garbled():
    """Only the id is taken from the line, so it is read leniently."""
    gw, out = _waiting([4])
    gw.unreadable_answer(b'{"jsonrpc":"2.0","id":4,"result":{"text":"caf\xe9 \x93quoted\x94"}}\n')
    answered = json.loads(out.getvalue())
    assert answered["id"] == 4 and answered["error"]["code"] == guarded_mcp.CONNECTOR_FAILED


def test_only_the_outermost_id_is_the_answers():
    """The MCP TypeScript SDK writes `result` before `id`: an id nested in
    the result belongs to something else, and settling it would fail another
    call and leave this one hanging."""
    gw, out = _waiting([5, 99])
    gw.unreadable_answer(b'{"result":{"content":[{"id":99}],"v":1e400},"jsonrpc":"2.0","id":5}\n')
    answered = json.loads(out.getvalue())
    assert answered["id"] == 5
    assert guarded_mcp._id_key(99) in gw._outstanding


@pytest.mark.parametrize("line", [
    b'{"jsonrpc":"2.0","id":6,"method":"sampling/createMessage","params":{"v":1e400}}\n',
    b'{"id":6,"id":7,"result":1e400}\n',
    b'{"result":{"id":6},"error":1e400}\n',
])
def test_a_line_whose_id_is_not_plainly_the_answers_settles_nothing(line):
    """A request of the server's own (its id is the server's), two ids, or
    only a nested one: left to its own time rather than failing a call."""
    gw, out = _waiting([6, 7])
    gw.unreadable_answer(line)
    assert out.getvalue() == b""
    assert len(gw._outstanding) == 2


def test_codexs_own_unreadable_request_is_still_answered():
    """Codex's side is a request, `method` and all: its id is answered."""
    import io
    out = io.BytesIO()
    gw = guarded_mcp.Gateway(gate=lambda n, a: (True, "ok"), out=out)
    gw.from_client(b'{"jsonrpc":"2.0","id":8,"method":"tools/call",'
                   b'"params":{"name":"post","arguments":{"n":1e400}}}\n')
    assert json.loads(out.getvalue())["id"] == 8


def test_an_unreadable_event_over_http_settles_its_call_at_once():
    """Not when the server finally closes the stream."""
    gw, out = _waiting([9])
    connector = guarded_mcp.HttpConnector.__new__(guarded_mcp.HttpConnector)
    connector._gateway = gw

    class Stream:
        def iter_lines(self):
            yield 'data: {"jsonrpc":"2.0","id":9,"result":{"v":1e400}}'
            yield ""
            raise AssertionError("read past the answer")
    connector._read_events(Stream(), done=lambda: guarded_mcp._id_key(9) not in gw._outstanding)
    answered = json.loads(out.getvalue())
    assert answered["id"] == 9 and answered["error"]["code"] == guarded_mcp.CONNECTOR_FAILED
