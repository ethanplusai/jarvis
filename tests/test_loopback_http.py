"""`loopback_http.post_json`: JARVIS's children calling JARVIS, and nobody else.

The PreToolUse hook and the stdio MCP child both carry the loopback bearer
token to JARVIS's own server. This is the one transport they share, and it
is held here to what it must NOT do as much as to what it does: it never
asks the environment (or, on Windows, Internet Options) for a proxy, and it
never follows a redirect — a 3xx is an answer, reported like any other
status that is not 2xx.

The callers' own behaviour — the hook failing closed, the MCP child's
sentences — is in tests/test_pretool_hook.py and tests/test_jarvis_mcp.py.
"""
from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.request

import pytest

import loopback_http
from tests.loopback_servers import (  # noqa: F401  (fixtures)
    Endpoint, Listener, elsewhere, endpoint, proxy, server_tls, tls_endpoint, use_proxy)

TOKEN = "s3cret"
BODY = json.dumps({"tool": "list_sessions", "arguments": {}}).encode()


def _unverified() -> ssl.SSLContext:
    return ssl._create_unverified_context()


def test_it_posts_the_body_with_the_token_and_returns_the_answer(endpoint):
    endpoint.answer(200, {"ok": True, "text": "fine"})
    raw = loopback_http.post_json(endpoint.origin + "/internal/tool?x=1", BODY, TOKEN,
                                  timeout=10)
    assert json.loads(raw) == {"ok": True, "text": "fine"}
    [call] = endpoint.requests
    assert call["method"] == "POST" and call["path"] == "/internal/tool?x=1"
    assert call["body"] == BODY
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["content-type"] == "application/json"
    # web_auth checks Host on every request; urllib sent the URL's netloc.
    assert call["headers"]["host"] == endpoint.netloc
    # Nothing the environment could have added (a Proxy-Authorization, a
    # cookie) rides along.
    assert set(call["headers"]) == {"host", "accept-encoding", "content-length",
                                    "content-type", "authorization"}


def test_every_2xx_is_an_answer(endpoint):
    endpoint.answer(201, {"ok": True})
    assert json.loads(loopback_http.post_json(endpoint.origin, BODY, TOKEN, timeout=10)) == {"ok": True}


def test_ipv6_loopback_is_dialled_and_named_in_brackets():
    """`server._tool_connect_host` maps a `::` or `::1` bind to `[::1]`."""
    try:
        server = Endpoint(host="::1")
    except OSError:
        pytest.skip("this machine has no IPv6 loopback")
    try:
        server.answer(200, {"ok": True})
        assert json.loads(loopback_http.post_json(server.origin + "/internal/tool", BODY,
                                                  TOKEN, timeout=10)) == {"ok": True}
        assert server.requests[0]["headers"]["host"] == f"[::1]:{server.port}"
    finally:
        server.close()


# --- TLS: the context given is the context used ----------------------------

def test_https_with_the_unverified_context_accepts_the_self_signed_certificate(tls_endpoint):
    tls_endpoint.answer(200, {"ok": True})
    raw = loopback_http.post_json(tls_endpoint.origin + "/internal/tool", BODY, TOKEN,
                                  timeout=10, context=_unverified())
    assert json.loads(raw) == {"ok": True}


def test_https_with_no_context_verifies_and_refuses_the_self_signed_certificate(tls_endpoint):
    """`jarvis_mcp` passes None for a host that is NOT loopback, meaning
    "verify as usual". A transport that dropped verification there would be
    trusting any certificate on a network hop."""
    with pytest.raises(ssl.SSLCertVerificationError):
        loopback_http.post_json(tls_endpoint.origin + "/internal/tool", BODY, TOKEN, timeout=10)
    assert tls_endpoint.requests == []


# --- no proxy ---------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_proxy_in_the_environment_is_not_used(request, scheme, proxy, monkeypatch):
    server = request.getfixturevalue("endpoint" if scheme == "http" else "tls_endpoint")
    server.answer(200, {"ok": True})
    use_proxy(monkeypatch, proxy.url)
    assert urllib.request.getproxies()["http"] == proxy.url   # it is really set
    raw = loopback_http.post_json(server.origin + "/internal/tool", BODY, TOKEN, timeout=10,
                                  context=_unverified() if scheme == "https" else None)
    assert json.loads(raw) == {"ok": True}
    assert proxy.connections == []


def test_a_windows_system_proxy_is_not_used_either(endpoint, monkeypatch):
    """urllib reads Internet Options when no proxy variable is set."""
    system_proxy = Listener()
    try:
        monkeypatch.setattr(urllib.request, "getproxies",
                            lambda: {"http": system_proxy.url, "https": system_proxy.url})
        endpoint.answer(200, {"ok": True})
        loopback_http.post_json(endpoint.origin, BODY, TOKEN, timeout=10)
        assert system_proxy.connections == []
    finally:
        system_proxy.close()


# --- no redirect ------------------------------------------------------------

@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 305, 307, 308])
def test_a_redirect_is_a_status_error_and_is_not_followed(endpoint, elsewhere, status):
    endpoint.answer(status, {}, {"Location": elsewhere.origin + "/internal/tool"})
    elsewhere.answer(200, {"ok": True, "text": "answered by somebody else"})
    with pytest.raises(loopback_http.StatusError) as raised:
        loopback_http.post_json(endpoint.origin + "/internal/tool", BODY, TOKEN, timeout=10)
    assert raised.value.status == status
    assert elsewhere.requests == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 503])
def test_an_error_status_is_a_status_error_with_its_code(endpoint, status):
    endpoint.answer(status, {"ok": True})
    with pytest.raises(loopback_http.StatusError) as raised:
        loopback_http.post_json(endpoint.origin, BODY, TOKEN, timeout=10)
    assert raised.value.status == status
    assert str(status) in str(raised.value)


@pytest.mark.parametrize("status", [200, 302, 500])
def test_the_connection_is_closed_on_return_even_while_the_error_is_held(status):
    """With `Connection: close`, http.client hands the socket to the response
    object; an error raised past an unclosed response keeps the socket open
    for as long as anybody holds that error (its traceback holds the frame)."""
    reply = (f"HTTP/1.0 {status} X\r\nConnection: close\r\nLocation: http://127.0.0.1:9/\r\n"
             f"Content-Length: 2\r\n\r\n{{}}").encode()
    server = Listener(reply=reply)
    try:
        try:
            loopback_http.post_json(server.url + "/internal/tool", BODY, TOKEN, timeout=10)
            held = None
        except loopback_http.StatusError as e:
            held = e   # as a caller that logs it, or pytest.raises, would
        assert server.served.wait(10)
        assert server.client_closed == [True], "the socket outlived the call"
        assert held is None or held.status == status
    finally:
        server.close()


def test_a_status_error_does_not_carry_the_token(endpoint):
    """Callers put the exception's class, or its status, into words the
    brain and the user see."""
    endpoint.answer(500, {})
    with pytest.raises(loopback_http.StatusError) as raised:
        loopback_http.post_json(endpoint.origin, BODY, TOKEN, timeout=10)
    assert TOKEN not in repr(raised.value) and TOKEN not in str(raised.value)


# --- what it will not dial --------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://127.0.0.1:21/internal/tool",
    "file:///etc/passwd",
    "127.0.0.1:8340/internal/tool",
    "http:///internal/tool",
    "http://127.0.0.1:notaport/internal/tool",
    "",
])
def test_a_url_that_is_not_http_is_refused_before_anything_is_dialled(url, monkeypatch):
    def no_dialling(*args, **kwargs):
        raise AssertionError("dialled " + url)
    monkeypatch.setattr(socket, "create_connection", no_dialling)
    with pytest.raises(ValueError):
        loopback_http.post_json(url, BODY, TOKEN, timeout=10)


# --- the budget -------------------------------------------------------------

def test_a_server_that_never_answers_is_a_timeout_within_the_budget():
    """The hook's budget (pretool_hook.TIMEOUT_SEC) sits inside the CLI's own;
    that nesting is only real if the transport honours it."""
    silent = Listener(hang_up=False)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            loopback_http.post_json(silent.url, BODY, TOKEN, timeout=0.5)
        assert time.monotonic() - started < 5
    finally:
        silent.close()


def test_a_refused_connection_is_an_oserror():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(OSError):
        loopback_http.post_json(f"http://127.0.0.1:{port}/internal/tool", BODY, TOKEN, timeout=10)
