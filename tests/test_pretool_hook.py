"""`pretool_hook.py`, run the way the Claude CLI runs it: a process per tool
call, the payload on stdin, the verdict on stdout.

Two things are held here.

It FAILS CLOSED. By Claude Code's hook contract, a hook that exits 0 has its
verdict read, and one that exits with anything but 0 or 2 is a "non-blocking
error" — the tool call PROCEEDS. So a hook that crashes lets the call
through, and every failure below has to come back as exit 0 with `deny`.

Its call goes STRAIGHT to JARVIS. It carries the loopback bearer token, and
the TLS on it is not verified (the certificate is JARVIS's own self-signed
one). urllib's default opener sent it through HTTP(S)_PROXY — which
`claude_env.child_env` passes through on purpose — because nothing names
127.0.0.1 in NO_PROXY; and it followed a 301/302/303 with the Authorization
header still on, and took the verdict from wherever it landed.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

import pretool_hook
from tests.loopback_servers import (  # noqa: F401  (fixtures)
    Listener, Unreachable, elsewhere, endpoint, proxied_env, proxy, server_tls,
    tls_endpoint, unproxied_env)

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "pretool_hook.py"
TOKEN = "s3cret-loopback-token"
PAYLOAD = {"tool_name": "mcp__linkedin__create_post",
           "tool_input": {"text": "hello"}, "tool_use_id": "toolu_1"}


def _verdict(decision: str, reason: str = "") -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}


ALLOW = _verdict("allow")


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "tool-token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    return path


def run_hook(url: str, token_file: Path, cwd: Path, *, env: dict | None = None,
             stdin: str | None = None, script: Path = HOOK):
    """As the CLI runs it: its own process, a working directory that is not
    the repo, the payload on stdin."""
    return subprocess.run(
        [sys.executable, str(script), "--url", url, "--token-file", str(token_file)],
        input=json.dumps(PAYLOAD) if stdin is None else stdin,
        capture_output=True, text=True, timeout=60, cwd=str(cwd),
        env=unproxied_env() if env is None else env)


def decision_of(done) -> tuple[str, str]:
    assert done.returncode == 0, (
        f"the hook exited {done.returncode} — the CLI lets the call RUN on "
        f"that. stderr: {done.stderr}")
    verdict = json.loads(done.stdout)["hookSpecificOutput"]
    assert verdict["hookEventName"] == "PreToolUse"
    return verdict["permissionDecision"], verdict["permissionDecisionReason"]


def server_for(request, scheme: str):
    return request.getfixturevalue("endpoint" if scheme == "http" else "tls_endpoint")


# --- what it already did, still held --------------------------------------

@pytest.mark.parametrize("scheme", ["http", "https"])
def test_the_gates_allow_is_passed_on_and_the_call_is_authenticated(
        request, scheme, token_file, tmp_path):
    server = server_for(request, scheme)
    server.answer(200, ALLOW)
    assert decision_of(run_hook(server.origin + "/internal/pretool", token_file,
                                tmp_path)) == ("allow", "")
    [call] = server.requests
    assert call["method"] == "POST" and call["path"] == "/internal/pretool"
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["content-type"] == "application/json"
    assert call["headers"]["host"] == f"127.0.0.1:{server.port}"
    assert json.loads(call["body"]) == PAYLOAD


def test_the_gates_deny_is_passed_on_with_its_reason(endpoint, token_file, tmp_path):
    endpoint.answer(200, _verdict("deny", "Held for your approval, sir."))
    assert decision_of(run_hook(endpoint.origin + "/internal/pretool", token_file,
                                tmp_path)) == ("deny", "Held for your approval, sir.")


def test_an_unreachable_gate_is_a_deny(token_file, tmp_path):
    down = Unreachable()
    decision, reason = decision_of(run_hook(down.url + "/internal/pretool", token_file, tmp_path))
    assert decision == "deny" and "did not answer" in reason


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_an_error_status_is_a_deny(endpoint, token_file, tmp_path, status):
    endpoint.answer(status, ALLOW)   # whatever the body says
    assert decision_of(run_hook(endpoint.origin + "/internal/pretool", token_file,
                                tmp_path))[0] == "deny"


@pytest.mark.parametrize("body", [
    b"not json", b"", b"[]", b"{}", json.dumps({"hookSpecificOutput": {}}).encode(),
    json.dumps(_verdict("maybe")).encode(), json.dumps(_verdict(None)).encode()])
def test_an_answer_that_is_not_a_verdict_is_a_deny(endpoint, token_file, tmp_path, body):
    endpoint.answer(200, body)
    assert decision_of(run_hook(endpoint.origin + "/internal/pretool", token_file,
                                tmp_path))[0] == "deny"


@pytest.mark.parametrize("reason", [{"text": "no"}, ["no"], 7, None])
def test_a_reason_that_is_not_text_is_dropped_so_the_verdict_stays_readable(
        endpoint, token_file, tmp_path, reason):
    """A verdict whose reason is not a string is one the CLI may not read,
    and a verdict it cannot read is not a deny."""
    endpoint.answer(200, {"hookSpecificOutput": {"permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}})
    assert decision_of(run_hook(endpoint.origin + "/internal/pretool", token_file,
                                tmp_path)) == ("deny", "")


def test_an_unreadable_token_is_a_deny_and_nothing_is_sent(endpoint, tmp_path):
    endpoint.answer(200, ALLOW)
    decision, _ = decision_of(run_hook(endpoint.origin + "/internal/pretool",
                                       tmp_path / "no-such-token", tmp_path))
    assert decision == "deny" and endpoint.requests == []


# --- a proxy in the environment is not used -------------------------------

@pytest.mark.parametrize("scheme", ["http", "https"])
def test_a_proxy_in_the_environment_is_not_used(request, scheme, proxy, token_file, tmp_path):
    """HTTP_PROXY / HTTPS_PROXY / ALL_PROXY set, NO_PROXY not: the call still
    reaches the server, and the proxy is handed nothing — not the token in
    the clear, not a CONNECT it could open with a certificate of its own."""
    server = server_for(request, scheme)
    server.answer(200, ALLOW)
    done = run_hook(server.origin + "/internal/pretool", token_file, tmp_path,
                    env=proxied_env(proxy.url))
    assert proxy.connections == [], f"the proxy was handed: {proxy.connections!r}"
    assert decision_of(done) == ("allow", "")
    assert server.requests[0]["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_a_windows_system_proxy_is_not_used_either(endpoint, token_file, monkeypatch, capsys):
    """With no proxy variable at all, urllib on Windows falls back to the
    proxy set in Internet Options (`getproxies_registry`). Stood in for here
    by the function urllib asks."""
    system_proxy = Listener()
    try:
        monkeypatch.setattr(urllib.request, "getproxies",
                            lambda: {"http": system_proxy.url, "https": system_proxy.url})
        endpoint.answer(200, ALLOW)
        monkeypatch.setattr(sys, "argv", ["pretool_hook.py", "--url",
                                          endpoint.origin + "/internal/pretool",
                                          "--token-file", str(token_file)])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(PAYLOAD)))
        assert pretool_hook.main() == 0
        verdict = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert system_proxy.connections == []
        assert verdict["permissionDecision"] == "allow"
    finally:
        system_proxy.close()


# --- a redirect is not followed -------------------------------------------

@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_a_deny_and_the_token_goes_nowhere_else(
        endpoint, elsewhere, token_file, tmp_path, status):
    """urllib followed 301/302/303 with the Authorization header on — and
    took the VERDICT from wherever it landed. Here that is an allow."""
    endpoint.answer(status, {}, {"Location": elsewhere.origin + "/internal/pretool"})
    elsewhere.answer(200, ALLOW)
    decision, _ = decision_of(run_hook(endpoint.origin + "/internal/pretool",
                                       token_file, tmp_path))
    assert elsewhere.requests == [], "the redirect was followed, token and all"
    assert decision == "deny"


# --- nothing it can hit makes it crash ------------------------------------

@pytest.mark.parametrize("stdin", ["[]", "null", "42", '"mcp__x__y"', "{not json", ""])
def test_a_payload_that_is_not_an_object_is_answered_not_crashed_on(
        endpoint, token_file, tmp_path, stdin):
    """The CLI always sends an object — but a hook that raised on anything
    else would exit 1, and exit 1 lets the call run."""
    endpoint.answer(200, _verdict("deny", "No."))
    assert decision_of(run_hook(endpoint.origin + "/internal/pretool", token_file,
                                tmp_path, stdin=stdin)) == ("deny", "No.")


def test_a_hook_that_cannot_load_its_transport_denies(endpoint, token_file, tmp_path):
    """The hook has one import beside the standard library. Were it at the top
    of the file and missing, the hook would die with a traceback — exit 1,
    and the call would run."""
    alone = tmp_path / "hook-alone"
    alone.mkdir()
    shutil.copy(HOOK, alone / "pretool_hook.py")
    endpoint.answer(200, ALLOW)
    decision, _ = decision_of(run_hook(endpoint.origin + "/internal/pretool", token_file,
                                       tmp_path, script=alone / "pretool_hook.py"))
    assert decision == "deny"
    assert endpoint.requests == []


# --- end to end: the real route, over a real socket -----------------------

class _HeardItsTools:
    """Connectors whose own names the CLI writes unchanged — as the brain
    would have heard from the CLI's `mcp_status` (tests/test_pretool_route.py).
    No turn of anybody's in flight, as with no brain at all."""
    current_origin = None

    def own_tool_names(self, cli_name):
        return frozenset({cli_name.split("__", 2)[2]})


@pytest.fixture
def live_gate(monkeypatch, tmp_path):
    """The real app on a real port, as the hook meets it in production:
    `/internal/pretool`, the token check and web_auth's Host check, on a
    data directory of the test's own. Never the running JARVIS."""
    import importlib
    import socket
    import threading
    import time

    import uvicorn

    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import business_store
    importlib.reload(business_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    business_store.init_db()
    # After the reload. This is about where the call goes, not the wait,
    # which tests/test_pretool_wait.py times on purpose.
    monkeypatch.setattr(server_module, "GATE_APPROVAL_WAIT_SEC", 0.05)
    # A brain that has heard the CLI's `mcp_status`: without one, a name
    # with `_` in its tool part is held for want of its own spelling, and
    # `get_feed` would be held for that, not read by the route.
    monkeypatch.setattr(server_module, "brain_instance", _HeardItsTools())
    data_paths.ensure_tool_token()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    live = uvicorn.Server(uvicorn.Config(server_module.app, host="127.0.0.1", port=port,
                                         log_level="error", lifespan="off"))
    thread = threading.Thread(target=live.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not live.started:
        assert time.monotonic() < deadline, "uvicorn never came up"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/internal/pretool", data_paths.tool_token_path()
    finally:
        live.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            live.force_exit = True
            thread.join(timeout=5)


def test_end_to_end_the_real_gate_answers_the_real_hook_past_a_proxy(live_gate, proxy, tmp_path):
    """A read passes and a post is held — the route's own verdicts, not the
    hook's "did not answer" — with every proxy variable pointing elsewhere."""
    url, token_path = live_gate
    env = proxied_env(proxy.url)

    def hook(tool: str, tool_input: dict) -> tuple[str, str]:
        return decision_of(run_hook(url, token_path, tmp_path, env=env, stdin=json.dumps(
            {"tool_name": tool, "tool_input": tool_input, "tool_use_id": "toolu_" + tool})))

    assert hook("mcp__linkedin__get_feed", {"count": 5})[0] == "allow"
    decision, reason = hook("mcp__linkedin__create_post", {"text": "hello"})
    assert decision == "deny"
    assert "approv" in reason.lower(), reason
    assert proxy.connections == []
