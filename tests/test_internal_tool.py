import sys
import pytest

_NEEDS_POSIX_MODES = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows has no POSIX mode bits to check")
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    # An Origin the dashboard itself would send, so these tests reach the
    # endpoint's OWN bearer check rather than being turned away at the door
    # by OriginGuard. A good Origin is deliberately not a substitute for the
    # token here: the assertions below are still 401.
    with TestClient(server_module.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        yield c, server_module


def test_internal_tool_requires_the_bearer_token(client):
    c, server = client
    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}})
    assert r.status_code == 401

    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}},
               headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_internal_tool_answers_with_the_token(client, monkeypatch):
    c, server = client
    token = server.data_paths.ensure_tool_token()
    monkeypatch.setitem(server.TOOL_HANDLERS, "list_sessions", lambda args: "ok")
    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_an_unknown_tool_is_refused_cleanly(client):
    c, server = client
    token = server.data_paths.ensure_tool_token()
    r = c.post("/internal/tool", json={"tool": "rm_rf", "arguments": {}},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "unknown tool" in r.json()["text"].lower()


def test_every_tool_result_is_capped_at_1500_characters(client, monkeypatch):
    c, server = client
    token = server.data_paths.ensure_tool_token()
    monkeypatch.setitem(server.TOOL_HANDLERS, "list_sessions",
                        lambda args: "x" * 5000)
    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}},
               headers={"Authorization": f"Bearer {token}"})
    text = r.json()["text"]
    assert len(text) <= 1500
    assert "ask for more" in text.lower()


def test_a_tool_that_raises_becomes_a_refusal_not_a_500(client, monkeypatch):
    c, server = client
    token = server.data_paths.ensure_tool_token()

    def boom(args):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_sessions", boom)
    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["ok"] is False


def test_a_long_exception_message_is_still_capped_at_1500_characters(client, monkeypatch):
    """The cap protects the brain's context budget; a refusal or exception
    string must not be able to skip it just because it wasn't the success
    path."""
    c, server = client
    token = server.data_paths.ensure_tool_token()

    def boom(args):
        raise RuntimeError("x" * 5000)

    monkeypatch.setitem(server.TOOL_HANDLERS, "list_sessions", boom)
    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert len(body["text"]) <= 1500


def test_a_non_ascii_authorization_header_is_a_clean_401_not_a_500(client):
    """secrets.compare_digest raises TypeError on non-ASCII str input; a
    malformed header must never surface as an unauthenticated 500."""
    c, server = client
    # httpx/starlette headers are ASCII-only for str values; a raw, non-ASCII
    # header (as curl would send verbatim) has to go in as bytes, the same
    # as it would arrive over the wire.
    r = c.post("/internal/tool", json={"tool": "list_sessions", "arguments": {}},
               headers={"Authorization": "Bearer é".encode("utf-8")})
    assert r.status_code == 401


def test_a_non_dict_json_body_is_refused_cleanly(client):
    c, server = client
    token = server.data_paths.ensure_tool_token()
    r = c.post("/internal/tool", json=[1, 2],
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


@_NEEDS_POSIX_MODES
def test_the_token_file_is_not_world_readable(client):
    c, server = client
    server.data_paths.ensure_tool_token()
    mode = server.data_paths.tool_token_path().stat().st_mode & 0o777
    assert mode == 0o600


@_NEEDS_POSIX_MODES
def test_ensure_tool_token_fixes_permissions_of_a_pre_existing_file(client):
    """A local process that pre-creates the token path with looser
    permissions must not get to keep read access to it."""
    c, server = client
    path = server.data_paths.tool_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pre-existing-token", encoding="utf-8")
    path.chmod(0o644)

    token = server.data_paths.ensure_tool_token()

    assert token == "pre-existing-token"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def _permissions(path):
    """What a restriction that followed the link would change: the mode
    bits, and on Windows — where `chmod` only toggles read-only and 0o644
    reads back as 0o666 — the ACL `restrict_to_owner` would rewrite."""
    mode = path.stat().st_mode & 0o777
    if sys.platform == "win32":
        import windows_acl
        return mode, windows_acl.dacl_sids(path)
    return mode


def test_a_symlink_in_the_token_path_is_refused_not_followed(client, tmp_path):
    """Adopting a pre-existing file is required across restarts. Adopting
    whatever a *symlink* points at is not.

    Two things went wrong through one link: `chmod` followed it, so any file
    the user owns could be forced to 0600 by pointing the path at it; and
    `read_text` followed it, so the token JARVIS then trusted was one the
    attacker wrote. Since the token now admits a caller to every
    state-changing route, that is the whole boundary.
    """
    import pytest as _pytest
    c, server = client
    path = server.data_paths.tool_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    planted = tmp_path / "planted"
    planted.write_text("attacker-chosen-token", encoding="utf-8")
    planted.chmod(0o644)
    before = _permissions(planted)
    path.unlink(missing_ok=True)          # the server made a real one at boot
    path.symlink_to(planted)

    with _pytest.raises(OSError):
        server.data_paths.ensure_tool_token()

    assert planted.read_text(encoding="utf-8") == "attacker-chosen-token"
    assert _permissions(planted) == before, "the restriction followed the link"


class _FakeBrain:
    """Stands in for brain_instance; needs an async stop() for lifespan teardown."""

    def __init__(self, origin):
        self._origin = origin

    @property
    def current_origin(self):
        return self._origin

    async def stop(self):
        pass


def test_an_acting_tool_is_refused_outside_a_user_turn(client, monkeypatch):
    """The origin gate lives in the server, not the prompt: a hostile string in
    somebody else's transcript must never be able to make JARVIS act."""
    c, server = client
    token = server.data_paths.ensure_tool_token()
    monkeypatch.setitem(server.TOOL_HANDLERS, "steer_session",
                        lambda args: "sent")
    for origin in (None, "watcher", "system"):
        monkeypatch.setattr(server, "brain_instance", _FakeBrain(origin))
        r = c.post("/internal/tool", json={"tool": "steer_session",
                                           "arguments": {"name": "x", "prompt": "y"}},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and "not_allowed_from_event" in body["text"]


def test_an_acting_tool_is_allowed_during_a_user_turn(client, monkeypatch):
    c, server = client
    token = server.data_paths.ensure_tool_token()
    monkeypatch.setitem(server.TOOL_HANDLERS, "steer_session",
                        lambda args: "sent")
    monkeypatch.setattr(server, "brain_instance", _FakeBrain("user"))
    r = c.post("/internal/tool", json={"tool": "steer_session",
                                       "arguments": {"name": "x", "prompt": "y"}},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json() == {"ok": True, "text": "sent"}


def test_every_acting_tool_has_a_real_registered_handler(client):
    """Break-confirmed by review: renaming the real registration at
    server.py's TOOL_HANDLERS.update({...}) from "steer_session" to some
    other string leaves every other test in this file green (they all
    monkeypatch a stub into TOOL_HANDLERS themselves) while the origin gate
    in ACTING_TOOLS would then match nothing actually registered — silently
    ungating the only tool in the system that acts. This must exercise the
    real, unpatched TOOL_HANDLERS dict."""
    c, server = client
    assert server.ACTING_TOOLS, "the acting-tools set must not be empty"
    assert server.ACTING_TOOLS <= set(server.TOOL_HANDLERS), (
        "every name in ACTING_TOOLS must be a real, registered handler — "
        f"missing: {server.ACTING_TOOLS - set(server.TOOL_HANDLERS)}")


def test_steer_session_is_both_registered_and_gated(client):
    """The specific pairing the review called out: 'steer_session' must be
    the name that is both registered as a handler AND present in
    ACTING_TOOLS — not just any two sets that happen to intersect."""
    c, server = client
    assert "steer_session" in server.TOOL_HANDLERS
    assert "steer_session" in server.ACTING_TOOLS
