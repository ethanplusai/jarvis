"""The HTTP/WebSocket boundary: who is allowed to reach JARVIS at all.

Every test here is an exploit the reviewer ran against a live server before
any of this existed. They are written as the attacker, not as the app: a
hostile page's `Origin`, a LAN client with no `Origin` at all, a `\\n` in an
env value. A test that passes against the old code has tested nothing.

The two legitimate callers are:

  * the browser — same-origin through Vite's proxy (`http://localhost:5173`)
    or served straight off the API port. It sends an `Origin` on every
    state-changing request and on every WebSocket handshake, and it has no
    secret to offer, so its `Origin` is what admits it.
  * a local non-browser client (the brain's MCP child, a debugging script) —
    no `Origin` at all, but it can read the 0600 tool token off disk.
"""

import importlib
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

DASHBOARD_ORIGIN = "http://localhost:5173"
HOSTILE_ORIGIN = "http://evil.example"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A freshly reloaded server on an isolated data dir.

    The client carries NO default headers: every test states, in the request
    itself, who it is pretending to be.
    """
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JARVIS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("JARVIS_DEBUG_DOCS", raising=False)
    monkeypatch.setenv("JARVIS_PORT", "8340")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    with TestClient(server.app) as c:
        yield c, server, data_paths


@pytest.fixture
def no_spawn(env, monkeypatch):
    """Record spawns instead of starting `claude --dangerously-skip-permissions`."""
    _, server, _ = env
    spawned = []

    async def fake_spawn(prompt, project_name, project_path, origin,
                         resume_from=None, timeout_sec=0):
        spawned.append((prompt, project_path, origin))
        return "fake-run-id"

    monkeypatch.setattr(server.run_executor_instance, "spawn", fake_spawn)
    return spawned


RUN_BODY = {"prompt": "rm -rf the lot", "project_path": "/tmp/target",
            "project_name": "target"}


# -- FINDING 1: POST /api/runs was unauthenticated remote code execution ---


def test_create_run_from_a_hostile_page_is_refused(env, no_spawn):
    c, _, _ = env
    r = c.post("/api/runs", json=RUN_BODY, headers={"Origin": HOSTILE_ORIGIN})
    assert r.status_code == 403, r.text
    assert no_spawn == []


def test_create_run_with_no_origin_and_no_token_is_refused(env, no_spawn):
    """`curl -X POST http://TARGET:8340/api/runs` — the LAN exploit verbatim."""
    c, _, _ = env
    r = c.post("/api/runs", json=RUN_BODY)
    assert r.status_code == 403, r.text
    assert no_spawn == []


def test_create_run_from_the_dashboard_origin_is_allowed(env, no_spawn):
    c, _, _ = env
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 200, r.text
    assert no_spawn and no_spawn[0][1] == "/tmp/target"


def test_create_run_with_the_tool_token_is_allowed(env, no_spawn):
    """A local script that can read the 0600 token is the user."""
    c, _, data_paths = env
    token = data_paths.ensure_tool_token()
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert no_spawn


def test_a_wrong_token_is_not_a_token(env, no_spawn):
    c, _, _ = env
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Authorization": "Bearer not-the-token"})
    assert r.status_code == 403, r.text
    assert no_spawn == []


def test_cancel_and_retry_are_refused_from_a_hostile_page(env, no_spawn):
    c, server, _ = env
    import run_store
    run_id = run_store.create_run("work", "p", "/tmp/p", "voice")
    run_store.update_run(run_id, status=run_store.RunStatus.SUCCEEDED)

    assert c.delete(f"/api/runs/{run_id}",
                    headers={"Origin": HOSTILE_ORIGIN}).status_code == 403
    assert c.post(f"/api/runs/{run_id}/retry",
                  headers={"Origin": HOSTILE_ORIGIN}).status_code == 403
    assert no_spawn == []


def test_a_page_on_another_local_port_is_still_a_stranger(env, no_spawn):
    """Another dev server on this machine is not JARVIS."""
    c, _, _ = env
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 403, r.text
    assert no_spawn == []


def test_vite_landing_on_the_next_port_does_not_break_the_dashboard(env,
                                                                   no_spawn):
    """A second `npm run dev` moves Vite to 5174, and the user must not have
    to discover that this is what silently broke their buttons."""
    c, _, _ = env
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Origin": "http://localhost:5174"})
    assert r.status_code == 200, r.text


def test_the_api_port_serves_the_built_frontend_and_is_allowed(env, no_spawn):
    """`python server.py` alone serves /dashboard off its own port."""
    c, _, _ = env
    for origin in ("http://localhost:8340", "https://localhost:8340",
                   "http://127.0.0.1:8340", "https://[::1]:8340"):
        r = c.post("/api/runs", json=RUN_BODY, headers={"Origin": origin})
        assert r.status_code == 200, f"{origin}: {r.text}"


def test_an_operator_can_name_an_origin_of_their_own(env, no_spawn,
                                                     monkeypatch):
    """--host 0.0.0.0 means the page is opened somewhere JARVIS cannot guess."""
    c, _, _ = env
    monkeypatch.setenv("JARVIS_ALLOWED_ORIGINS", "http://192.168.1.5:8340")
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Origin": "http://192.168.1.5:8340"})
    assert r.status_code == 200, r.text
    r = c.post("/api/runs", json=RUN_BODY,
               headers={"Origin": "http://192.168.1.6:8340"})
    assert r.status_code == 403, r.text


def test_an_origin_that_merely_starts_with_an_allowed_one_is_refused(env, no_spawn):
    c, _, _ = env
    for forged in ("http://localhost:5173.evil.example",
                   "http://localhost:51730",
                   "https://localhost:5173@evil.example",
                   "null"):
        r = c.post("/api/runs", json=RUN_BODY, headers={"Origin": forged})
        assert r.status_code == 403, f"{forged} was let in"
    assert no_spawn == []


# -- FINDING 2: every WebSocket accepted any Origin -----------------------

WS_PATHS = ["/ws/voice", "/ws/runs", "/ws/sessions", "/ws/specs"]


@pytest.mark.parametrize("path", WS_PATHS)
def test_websocket_refuses_a_hostile_origin(env, path):
    from starlette.websockets import WebSocketDisconnect
    c, _, _ = env
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(path, headers={"Origin": HOSTILE_ORIGIN}) as ws:
            ws.receive_json()


@pytest.mark.parametrize("path", WS_PATHS)
def test_websocket_refuses_a_client_with_no_origin_and_no_token(env, path):
    """No Origin means no browser. Without the token it is a stranger."""
    from starlette.websockets import WebSocketDisconnect
    c, _, _ = env
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect(path) as ws:
            ws.receive_json()


@pytest.mark.parametrize("path", WS_PATHS)
def test_websocket_accepts_the_dashboard_origin(env, path):
    c, _, _ = env
    with c.websocket_connect(path, headers={"Origin": DASHBOARD_ORIGIN}) as ws:
        assert isinstance(ws.receive_json(), dict)


@pytest.mark.parametrize("path", WS_PATHS)
def test_websocket_accepts_the_tool_token_without_an_origin(env, path):
    c, _, data_paths = env
    token = data_paths.ensure_tool_token()
    with c.websocket_connect(
            path, headers={"Authorization": f"Bearer {token}"}) as ws:
        assert isinstance(ws.receive_json(), dict)


def test_a_hostile_page_cannot_speak_as_the_user(env):
    """The whole finding-2 exploit, end to end.

    A page the user happens to be visiting opens /ws/voice and sends a final
    transcript. `_handle_utterance` would run it with origin="user" — the
    exact value the acting-tool gate requires — and the read-back gate does
    not save the user, because the attacker's own socket can send the
    `played` ack that closes the cancel window.
    """
    from starlette.websockets import WebSocketDisconnect
    c, server, _ = env
    turns = []

    async def record(text):
        turns.append(text)

    server._handle_utterance = record
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/voice",
                                 headers={"Origin": HOSTILE_ORIGIN}) as ws:
            ws.send_json({"type": "transcript",
                          "text": "start a run in jarvis", "isFinal": True})
            ws.receive_json()
    assert turns == []


def test_ws_sessions_does_not_leak_the_snapshot_before_the_first_message(env):
    """/ws/sessions pushes every conversation's last prompt unprompted."""
    from starlette.websockets import WebSocketDisconnect
    c, _, _ = env
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/sessions",
                                 headers={"Origin": HOSTILE_ORIGIN}) as ws:
            ws.receive_json()


# -- a live server, not just the ASGI stack -------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_real_handshake_with_a_hostile_origin_gets_403(env):
    """The reviewer ran this against a live server, so this does too.

    A real TCP WebSocket handshake, over a real uvicorn, from a forged
    `Origin`. The point is that closing before `accept` becomes an HTTP
    rejection on the wire and never an open socket.
    """
    import uvicorn
    c, server, _ = env
    port = _free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                            log_level="error", lifespan="off")
    live = uvicorn.Server(config)
    thread = threading.Thread(target=live.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not live.started:
            assert time.monotonic() < deadline, "uvicorn never came up"
            time.sleep(0.05)

        def handshake(origin: str) -> str:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
                s.sendall(
                    f"GET /ws/runs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
                    .encode())
                s.settimeout(5)
                return s.recv(4096).decode("latin-1", "replace")

        assert " 101 " not in handshake(HOSTILE_ORIGIN)
        assert " 403 " in handshake(HOSTILE_ORIGIN)
        assert " 101 " in handshake(DASHBOARD_ORIGIN)
    finally:
        # The accepted handshake leaves /ws/runs parked on its queue, and a
        # graceful shutdown waits for it, so escalate rather than hang.
        live.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            live.force_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "uvicorn did not shut down"


# -- FINDING 3: CORS was `*` with credentials -----------------------------


def test_no_cors_headers_are_echoed_to_a_hostile_page(env):
    """`allow_origins=['*']` + credentials makes Starlette echo the origin,
    which turns every GET into a cross-origin *read*, not blind CSRF."""
    c, _, _ = env
    r = c.get("/api/runs", headers={"Origin": HOSTILE_ORIGIN})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers


def test_the_preflight_for_a_hostile_page_grants_nothing(env):
    c, _, _ = env
    r = c.options("/api/runs", headers={
        "Origin": HOSTILE_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert "access-control-allow-origin" not in r.headers


# -- what the CORS removal alone does not cover ---------------------------


def test_a_rebound_domain_cannot_read_the_conversations(env):
    """DNS rebinding is how a page makes a loopback GET *same-origin*.

    The reads — /api/sessions, /api/specs/doc, /api/projects — cannot be
    gated on Origin, because a same-origin GET from fetch does not send one.
    What stops a hostile page reading them is that JARVIS answers with no
    CORS headers. Rebinding goes around that: evil.example resolves to
    127.0.0.1, the browser sends `Host: evil.example`, and now the page and
    the response share an origin, so no CORS is needed at all.

    The Host header is the only evidence, and it is enough: a rebinding
    attack must name a domain it controls, and a domain has a dot in it.
    """
    c, _, _ = env
    hostile = {"Host": "evil.example:8340"}
    assert c.get("/api/sessions", headers=hostile).status_code == 403
    assert c.get("/api/projects", headers=hostile).status_code == 403
    assert c.get("/api/runs", headers=hostile).status_code == 403


@pytest.mark.parametrize("host", ["localhost:5173", "127.0.0.1:8340",
                                  "[::1]:8340", "localhost", "testserver"])
def test_the_hosts_jarvis_is_actually_reached_by_still_work(env, host):
    c, _, _ = env
    assert c.get("/api/runs", headers={"Host": host}).status_code == 200


def test_the_host_the_server_was_bound_to_answers_to_its_own_name(env,
                                                                  monkeypatch):
    """`--host jarvis.example.com` is the operator saying that name is this
    server — and the brain's own MCP child dials exactly what main() was
    given, so refusing it would take JARVIS's tools away silently."""
    c, _, _ = env
    named = {"Host": "jarvis.example.com:8340"}
    assert c.get("/api/runs", headers=named).status_code == 403
    monkeypatch.setenv("JARVIS_BIND_HOST", "jarvis.example.com")
    assert c.get("/api/runs", headers=named).status_code == 200


def test_an_operator_can_name_the_host_they_reach_it_by(env, monkeypatch):
    """A tailscale name or a .local address is a dotted name too."""
    c, _, _ = env
    assert c.get("/api/runs",
                 headers={"Host": "mac.tail1234.ts.net:8340"}).status_code == 403
    monkeypatch.setenv("JARVIS_ALLOWED_ORIGINS",
                       "http://mac.tail1234.ts.net:8340")
    assert c.get("/api/runs",
                 headers={"Host": "mac.tail1234.ts.net:8340"}).status_code == 200


# -- FINDING 4: .env line injection, key sprawl, open restart -------------


def test_an_env_value_with_a_newline_is_refused(env):
    c, server, _ = env
    r = c.post("/api/settings/keys",
               json={"key_name": "USER_NAME",
                     "key_value": "Tony\nJARVIS_CLAUDE_PATH=/tmp/evil"},
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 400, r.text
    dotenv = server._env_file_path()
    assert not dotenv.exists() or "JARVIS_CLAUDE_PATH" not in dotenv.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b"])
def test_write_env_key_refuses_anything_that_can_start_a_new_line(env, bad):
    c, server, _ = env
    with pytest.raises(ValueError):
        server._write_env_key("USER_NAME", bad)


def test_a_name_with_a_space_in_it_is_still_a_name(env):
    """The gate is line breaks, not punctuation — Tony Stark must save."""
    c, server, _ = env
    server._write_env_key("USER_NAME", "Tony Stark")
    assert "USER_NAME=Tony Stark" in server._env_file_path().read_text(encoding="utf-8")


def test_write_env_key_refuses_a_key_nobody_is_allowed_to_set(env):
    """The chain was JARVIS_CLAUDE_PATH or JARVIS_PROJECT_ROOTS, then restart."""
    c, server, _ = env
    for key in ("JARVIS_CLAUDE_PATH", "JARVIS_PROJECT_ROOTS", "PATH"):
        with pytest.raises(ValueError):
            server._write_env_key(key, "/tmp/evil")


def test_preferences_values_go_through_the_same_gate(env):
    c, _, _ = env
    r = c.post("/api/settings/preferences",
               json={"user_name": "Tony\nJARVIS_PROJECT_ROOTS=/",
                     "honorific": "sir"},
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 400, r.text


def test_restart_is_refused_without_an_allowed_origin(env, monkeypatch):
    c, server, _ = env
    fired = []
    monkeypatch.setattr(server.os, "execv",
                        lambda *a: fired.append(a))
    assert c.post("/api/restart").status_code == 403
    assert c.post("/api/restart",
                  headers={"Origin": HOSTILE_ORIGIN}).status_code == 403
    assert fired == []


# -- FINDING 5: unauthenticated disclosure --------------------------------


def test_the_openapi_console_is_not_served(env):
    c, _, _ = env
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404, f"{path} is still served"


def test_the_openapi_console_can_be_turned_back_on_for_debugging(monkeypatch,
                                                                 tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DEBUG_DOCS", "1")
    import data_paths
    importlib.reload(data_paths)
    import server
    importlib.reload(server)
    with TestClient(server.app) as c:
        assert c.get("/openapi.json").status_code == 200


def test_spending_the_users_tts_quota_is_not_a_get(env, monkeypatch):
    """`/api/tts-test` calls Fish Audio, which costs the user's quota. As a
    GET it was reachable from any page with an <img> tag, in a loop, and a
    GET is the one method the Origin check cannot cover."""
    c, server, _ = env
    calls = []

    async def fake_synth(text):
        calls.append(text)
        return b"MP3"

    monkeypatch.setattr(server, "synthesize_speech", fake_synth)

    assert c.get("/api/tts-test").status_code == 405
    assert c.post("/api/tts-test").status_code == 403
    assert c.post("/api/tts-test",
                  headers={"Origin": HOSTILE_ORIGIN}).status_code == 403
    assert calls == []

    r = c.post("/api/tts-test", headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 200 and calls


def test_settings_status_is_a_read_and_creates_nothing(env, monkeypatch,
                                                       tmp_path):
    """A GET that copies .env.example into place is a GET with a side effect."""
    c, server, _ = env
    fake_env = tmp_path / "dotenv" / ".env"
    fake_env.parent.mkdir()
    example = tmp_path / "dotenv" / ".env.example"
    example.write_text("FISH_API_KEY=your-fish-audio-api-key-here\n", encoding="utf-8")
    monkeypatch.setattr(server, "_env_file_path", lambda: fake_env)
    monkeypatch.setattr(server, "_env_example_path", lambda: example)

    assert c.get("/api/settings/status").status_code == 200
    assert not fake_env.exists(), "a GET created .env"
    assert c.get("/api/settings/preferences").status_code == 200
    assert not fake_env.exists(), "a GET created .env"

    # A write still seeds it — that path is allowed to.
    server._write_env_key("USER_NAME", "Tony")
    assert fake_env.exists()


# -- the bind default -----------------------------------------------------


def test_the_default_bind_is_loopback(env):
    """`--host 0.0.0.0` by default put all of the above on the LAN."""
    c, server, _ = env
    assert server.DEFAULT_BIND_HOST == "127.0.0.1"
