import base64
import json
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import jarvis_mcp

REPO = Path(__file__).resolve().parents[1]


class Server:
    """Drive jarvis_mcp.py over its stdio pipes, as the CLI does."""

    # `call()` blocks on readline(), so a child that never answers would hang
    # the whole suite rather than fail it — and a test that hangs has told you
    # nothing at all. The watchdog kills the child; readline() then returns ''
    # at EOF and the assert in `call` fires with the child's stderr attached.
    DEADLINE_SEC = 30.0

    def __init__(self, env):
        self.p = subprocess.Popen(
            [sys.executable, str(REPO / "jarvis_mcp.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env, cwd=str(REPO))
        self._watchdog = threading.Timer(self.DEADLINE_SEC, self._expire)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _expire(self):
        try:
            self.p.kill()
        except Exception:
            pass

    def call(self, method, params=None, rid=1):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        assert line, f"no reply to {method}; stderr={self.p.stderr.read()}"
        return json.loads(line)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def close(self):
        self._watchdog.cancel()
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()
            self.p.wait(timeout=5)
        self.p.stdout.close()
        self.p.stderr.close()


@pytest.fixture
def fake_endpoint(tmp_path):
    """A one-request-at-a-time HTTP stand-in for the server's /internal/tool."""
    import http.server, threading

    calls = []
    body_holder = {"body": {"ok": True, "text": "fine"}, "status": 200}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            calls.append({"path": self.path, "payload": payload,
                          "auth": self.headers.get("Authorization")})
            data = json.dumps(body_holder["body"]).encode()
            self.send_response(body_holder["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1], calls, body_holder
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env_for(tmp_path, fake_endpoint):
    port, calls, holder = fake_endpoint
    token = tmp_path / "tool-token"
    token.write_text("s3cret", encoding="utf-8")
    env = dict(os.environ)
    env.update({"JARVIS_TOOL_URL": f"http://127.0.0.1:{port}/internal/tool",
                "JARVIS_TOOL_TOKEN_FILE": str(token)})
    return env, calls, holder


def test_initialize_handshake_and_tool_listing(env_for):
    env, _, _ = env_for
    s = Server(env)
    try:
        r = s.call("initialize", {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo": {"name": "t"}})
        assert r["result"]["serverInfo"]["name"] == "jarvis"
        assert "tools" in r["result"]["capabilities"]
        s.notify("notifications/initialized")

        r = s.call("tools/list", {}, rid=2)
        names = [t["name"] for t in r["result"]["tools"]]
        assert "list_sessions" in names and "session_detail" in names
        assert "steer_session" in names
        for t in r["result"]["tools"]:
            assert t["description"] and t["inputSchema"]["type"] == "object"
    finally:
        s.close()


def test_a_tool_call_is_forwarded_with_the_bearer_token(env_for):
    env, calls, _ = env_for
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/call", {"name": "list_sessions", "arguments": {}}, rid=3)
        assert r["result"]["content"][0]["text"] == "fine"
        assert r["result"].get("isError") in (False, None)
        assert calls[0]["payload"] == {"tool": "list_sessions", "arguments": {}}
        assert calls[0]["auth"] == "Bearer s3cret"
    finally:
        s.close()


def test_a_refused_tool_comes_back_as_an_error_result_not_a_crash(env_for):
    env, _, holder = env_for
    holder["body"] = {"ok": False, "text": "not_allowed_from_event"}
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/call", {"name": "steer_session",
                                  "arguments": {"name": "x", "prompt": "y"}}, rid=4)
        assert r["result"]["isError"] is True
        assert "not_allowed_from_event" in r["result"]["content"][0]["text"]
    finally:
        s.close()


def test_an_unreachable_server_is_reported_not_raised(tmp_path):
    token = tmp_path / "t"
    token.write_text("x", encoding="utf-8")
    env = dict(os.environ)
    env.update({"JARVIS_TOOL_URL": "http://127.0.0.1:1/internal/tool",
                "JARVIS_TOOL_TOKEN_FILE": str(token)})
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/call", {"name": "list_sessions", "arguments": {}}, rid=5)
        assert r["result"]["isError"] is True
        assert "unreachable" in r["result"]["content"][0]["text"].lower()
    finally:
        s.close()


def test_an_unknown_method_returns_a_json_rpc_error(env_for):
    env, _, _ = env_for
    s = Server(env)
    try:
        r = s.call("no/such/method", {}, rid=6)
        assert r["error"]["code"] == -32601
    finally:
        s.close()


def test_ping_is_answered(env_for):
    env, _, _ = env_for
    s = Server(env)
    try:
        assert s.call("ping", {}, rid=7)["result"] == {}
    finally:
        s.close()


def test_a_garbage_line_does_not_kill_the_server(env_for):
    env, _, _ = env_for
    s = Server(env)
    try:
        s.p.stdin.write("this is not json\n")
        s.p.stdin.flush()
        assert s.call("ping", {}, rid=8)["result"] == {}
    finally:
        s.close()


def test_a_notification_gets_no_reply(env_for):
    """{"method": "ping"} with no "id" member is a JSON-RPC notification: it
    must get NO reply at all, not {"id": null, ...}."""
    env, _, _ = env_for
    s = Server(env)
    try:
        s.notify("ping")
        # If the notification had (wrongly) produced a reply, it would be
        # sitting first in the pipe and this call would read it instead,
        # coming back with the wrong id.
        r = s.call("ping", {}, rid=99)
        assert r == {"jsonrpc": "2.0", "id": 99, "result": {}}
    finally:
        s.close()


def test_a_notification_for_an_unknown_method_gets_no_reply(env_for):
    env, _, _ = env_for
    s = Server(env)
    try:
        s.notify("no/such/method")
        r = s.call("ping", {}, rid=100)
        assert r == {"jsonrpc": "2.0", "id": 100, "result": {}}
    finally:
        s.close()


def test_loopback_https_endpoint_builds_an_unverified_ssl_context():
    for host in ("127.0.0.1", "[::1]", "localhost"):
        ctx = jarvis_mcp._ssl_context_for(f"https://{host}:8340/internal/tool")
        assert ctx is not None
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE


def test_bracketed_ipv6_loopback_https_endpoint_builds_an_unverified_context():
    ctx = jarvis_mcp._ssl_context_for("https://[::1]:8340/internal/tool")
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE


def test_non_loopback_https_endpoint_keeps_certificate_verification():
    ctx = jarvis_mcp._ssl_context_for("https://example.com:8340/internal/tool")
    assert ctx is None


def test_http_endpoint_needs_no_ssl_context():
    assert jarvis_mcp._ssl_context_for("http://127.0.0.1:8340/internal/tool") is None


# --- letting the brain SEE something --------------------------------------
#
# The brain is `claude -p` with `--tools` set to an ALLOWLIST naming only
# JARVIS's own MCP tools, so it has no Read tool: handing it the PATH of a PNG
# would hand it a string it can do nothing whatever with. The one route an
# image has into that process is an MCP `image` content block on a tool
# result.
#
# That was VERIFIED before any of it was built, rather than assumed. A
# throwaway stdio MCP server returning `{"type": "image", ...}` for a 1280x800
# PNG reading "PURPLE WALRUS 7421", driven by
#
#   claude -p --model sonnet --setting-sources project --strict-mcp-config \
#     --mcp-config ./mcp.json --tools mcp__t__see --dangerously-skip-permissions \
#     "Call the 'see' tool, then tell me the exact words that appear in the
#      image it returns. If no image reached you, reply exactly: NO IMAGE
#      REACHED ME."
#
# answered: "The image shows the exact words: PURPLE WALRUS 7421".
#
# These tests pin this end of that mechanism: the child must turn the server's
# `image` field into a real content block, and must not let a malformed one
# take the whole result down with it.


def test_an_image_from_the_server_becomes_an_image_content_block(env_for):
    env, _, holder = env_for
    data = base64.b64encode(b"\x89PNG\r\n\x1a\npretend").decode()
    holder["body"] = {"ok": True, "text": "A screenshot of Stark Industries.",
                      "image": {"data": data, "mimeType": "image/png"}}
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/call", {"name": "look_at_page",
                                  "arguments": {"url": "https://stark.example/"}},
                   rid=7)
        content = r["result"]["content"]
        assert content[0] == {"type": "text",
                              "text": "A screenshot of Stark Industries."}
        assert content[1] == {"type": "image", "data": data,
                              "mimeType": "image/png"}
        assert r["result"]["isError"] is False
    finally:
        s.close()


def test_a_tool_with_no_image_still_returns_text_alone(env_for):
    env, _, holder = env_for
    holder["body"] = {"ok": True, "text": "fine"}
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/call", {"name": "list_sessions", "arguments": {}}, rid=8)
        assert r["result"]["content"] == [{"type": "text", "text": "fine"}]
    finally:
        s.close()


@pytest.mark.parametrize("bad", [
    {"data": "", "mimeType": "image/png"},
    {"data": 12, "mimeType": "image/png"},
    {"mimeType": "image/png"},
    {"data": "abc"},
    "not-an-object",
    None,
])
def test_a_malformed_image_is_dropped_not_forwarded(env_for, bad):
    """A broken content block would take down the whole tool result, and the
    text half of the answer is still worth having."""
    env, _, holder = env_for
    holder["body"] = {"ok": True, "text": "No luck there, sir.", "image": bad}
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/call", {"name": "look_at_page",
                                  "arguments": {"url": "https://x.example/"}},
                   rid=9)
        assert r["result"]["content"] == [{"type": "text",
                                           "text": "No luck there, sir."}]
        assert r["result"]["isError"] is False
    finally:
        s.close()


def test_the_new_tools_are_advertised_with_tight_descriptions(env_for):
    """Descriptions cost the brain context on EVERY turn."""
    env, _, _ = env_for
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        r = s.call("tools/list", {}, rid=10)
        specs = {t["name"]: t for t in r["result"]["tools"]}
        for name in ("read_page", "look_at_page", "usage_status"):
            assert name in specs
            assert specs[name]["description"]
            assert len(specs[name]["description"]) < 600, name
            assert specs[name]["inputSchema"]["type"] == "object"
        assert "browser" in specs["open_in_browser"]["inputSchema"]["properties"]
    finally:
        s.close()


# --- the call goes straight to JARVIS -------------------------------------
#
# `_forward` carries the loopback bearer token, and the TLS on it is not
# verified (the certificate is JARVIS's own self-signed one). urllib's
# default opener sent it through HTTP(S)_PROXY — which `claude_env.
# child_env` passes through on purpose — because nothing names 127.0.0.1 in
# NO_PROXY, and followed a 301/302/303 with the Authorization header still
# on. The stand-ins are in tests/loopback_servers.py.

from tests.loopback_servers import (  # noqa: E402,F401  (fixtures)
    Listener, elsewhere, endpoint, proxied_env, proxy, server_tls, tls_endpoint,
    unproxied_env)


def _tool_env(tmp_path, url: str, base: dict) -> dict:
    token = tmp_path / "tool-token"
    token.write_text("s3cret", encoding="utf-8")
    env = dict(base)
    env.update({"JARVIS_TOOL_URL": url, "JARVIS_TOOL_TOKEN_FILE": str(token)})
    return env


def _one_tool_call(env: dict) -> dict:
    s = Server(env)
    try:
        s.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        return s.call("tools/call", {"name": "list_sessions", "arguments": {}},
                      rid=11)["result"]
    finally:
        s.close()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_proxy_in_the_environment_is_not_used(request, scheme, proxy, tmp_path):
    """HTTP_PROXY / HTTPS_PROXY / ALL_PROXY set, NO_PROXY not: the call still
    reaches the server, and the proxy is handed nothing — not the token in
    the clear, not a CONNECT it could open with a certificate of its own."""
    server = request.getfixturevalue("endpoint" if scheme == "http" else "tls_endpoint")
    server.answer(200, {"ok": True, "text": "straight through"})
    result = _one_tool_call(_tool_env(tmp_path, server.origin + "/internal/tool",
                                      proxied_env(proxy.url)))
    assert proxy.connections == [], f"the proxy was handed: {proxy.connections!r}"
    assert result["content"][0]["text"] == "straight through"
    assert result["isError"] is False
    [call] = server.requests
    assert call["headers"]["authorization"] == "Bearer s3cret"
    assert call["headers"]["host"] == f"127.0.0.1:{server.port}"
    assert json.loads(call["body"]) == {"tool": "list_sessions", "arguments": {}}


def test_a_windows_system_proxy_is_not_used_either(endpoint, tmp_path, monkeypatch):
    """With no proxy variable at all, urllib on Windows falls back to the
    proxy set in Internet Options (`getproxies_registry`). Stood in for here
    by the function urllib asks."""
    import urllib.request
    system_proxy = Listener()
    try:
        monkeypatch.setattr(urllib.request, "getproxies",
                            lambda: {"http": system_proxy.url, "https": system_proxy.url})
        for name, value in _tool_env(tmp_path, endpoint.origin + "/internal/tool",
                                     {}).items():
            monkeypatch.setenv(name, value)
        endpoint.answer(200, {"ok": True, "text": "straight through"})
        assert jarvis_mcp._forward("list_sessions", {}) == (True, "straight through", None)
        assert system_proxy.connections == []
    finally:
        system_proxy.close()


def test_the_self_signed_certificate_is_still_accepted_on_loopback(tls_endpoint, tmp_path):
    """What makes the unverified context safe is that the hop never leaves
    the machine; what makes it NECESSARY is that no CA signed JARVIS's
    certificate. A transport that verified it would fail every call."""
    tls_endpoint.answer(200, {"ok": True, "text": "fine"})
    result = _one_tool_call(_tool_env(tmp_path, tls_endpoint.origin + "/internal/tool",
                                      unproxied_env()))
    assert result["content"][0]["text"] == "fine" and result["isError"] is False


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(
        endpoint, elsewhere, tmp_path, status):
    endpoint.answer(status, {}, {"Location": elsewhere.origin + "/internal/tool"})
    elsewhere.answer(200, {"ok": True, "text": "answered by somebody else"})
    result = _one_tool_call(_tool_env(tmp_path, endpoint.origin + "/internal/tool",
                                      unproxied_env()))
    assert elsewhere.requests == [], "the redirect was followed, token and all"
    assert result["isError"] is True
    assert result["content"][0]["text"] == f"JARVIS refused the call ({status})."


@pytest.mark.parametrize("status", [401, 403, 500])
def test_an_error_status_is_reported_with_its_code(endpoint, tmp_path, status):
    endpoint.answer(status, {"ok": True, "text": "not this"})
    result = _one_tool_call(_tool_env(tmp_path, endpoint.origin + "/internal/tool",
                                      unproxied_env()))
    assert result["isError"] is True
    assert result["content"][0]["text"] == f"JARVIS refused the call ({status})."


@pytest.mark.parametrize("body", [b"not json", b""])
def test_an_unreadable_answer_is_reported_as_such(endpoint, tmp_path, body):
    endpoint.answer(200, body)
    result = _one_tool_call(_tool_env(tmp_path, endpoint.origin + "/internal/tool",
                                      unproxied_env()))
    assert result["isError"] is True
    assert result["content"][0]["text"] == "The JARVIS server sent something unreadable."


@pytest.mark.parametrize("url", ["ftp://127.0.0.1:21/internal/tool",
                                 "file:///etc/passwd", "127.0.0.1:8340/internal/tool"])
def test_a_tool_url_that_is_not_http_is_unreachable_not_a_crash(tmp_path, url):
    result = _one_tool_call(_tool_env(tmp_path, url, unproxied_env()))
    assert result["isError"] is True
    assert result["content"][0]["text"] == "The JARVIS server is unreachable."
