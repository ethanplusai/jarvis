"""The interim LinkedIn limits and the automatic stop (`linkedin_guard.py`).

Owner decision, 2026-10-08: LinkedIn's rules forbid "bots or other
unauthorized automated methods" to post, comment, like or share, and the
connector JARVIS has drives the owner's own signed-in browser. Until the
official API route is live, and enforced in code rather than by convention:

  * at most 1 post a day per account, at least 6 hours between posts on one
    account;
  * at most 5 comments a day;
  * on any LinkedIn challenge, captcha, "unusual activity" notice,
    restriction or failed login, every LinkedIn call stops — reads too — and
    the owner is told. Never an attempt to get past it. Only the owner
    resumes it, and not through anything the brain can call.

Fakes everywhere: no test talks to LinkedIn or a real `claude`.
"""
from __future__ import annotations

import importlib
import json
import time
from datetime import datetime, timedelta

import pytest

CREATE = "mcp__linkedin__create_post"
COMMENT = "mcp__linkedin__comment_on_post"
READ = "mcp__linkedin__get_feed"
POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000002/"


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    for name in ("LINKEDIN_POSTS_PER_DAY", "LINKEDIN_COMMENTS_PER_DAY",
                 "LINKEDIN_MIN_POST_GAP_HOURS", "LINKEDIN_CONNECTOR_SERVERS"):
        monkeypatch.delenv(name, raising=False)
    import data_paths
    importlib.reload(data_paths)
    import business_store
    importlib.reload(business_store)
    business_store.init_db()
    import linkedin_guard
    importlib.reload(linkedin_guard)
    return linkedin_guard


def _sent(operation, payload, *, at, provider="connector:linkedin", state="submitted"):
    """A card released at `at`, as the gate or the desk would leave it."""
    import business_store
    card = business_store.propose(provider, operation, payload)
    with business_store.connect() as conn:
        conn.execute("UPDATE business_actions SET state=?, updated=? WHERE id=?",
                     (state, at, card["id"]))
    return card


# Noon today: the tests that count a day must not depend on when they run.
NOON = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


def _post(text="hello world", confirm=True):
    return {"text": text, "confirm_post": confirm}


def _comment(text="a comment", confirm=True):
    return {"post_url": POST_URL, "text": text, "confirm_comment": confirm}


# --- what counts --------------------------------------------------------------

def test_a_confirmed_post_and_a_confirmed_comment_are_what_count(guard):
    assert guard.connector_kind("linkedin", "create_post", _post()) == "post"
    assert guard.connector_kind("linkedin", "comment_on_post", _comment()) == "comment"
    assert guard.connector_kind("linkedin", "reply_to_comment",
                                {"confirm_reply": True, "text": "x"}) == "comment"
    assert guard.connector_kind("linkedin", "create_post", _post(confirm=False)) is None, \
        "a dry run publishes nothing"
    assert guard.connector_kind("linkedin", "get_feed", {}) is None
    assert guard.connector_kind("paperclip", "create_post", _post()) is None, \
        "another server's tool of the same name is not LinkedIn"


def test_the_official_api_cards_count_the_same(guard):
    assert guard.provider_kind("post") == "post"
    assert guard.provider_kind("comment") == "comment"
    assert guard.provider_kind("report") is None


# --- the limits ------------------------------------------------------------------

def test_the_first_post_of_the_day_is_allowed(guard):
    assert guard.check("post", "member").ok


def test_a_second_post_the_same_day_is_refused_and_says_when(guard):
    now = NOON + 3 * 3600
    _sent(CREATE, _post(), at=NOON - 4 * 3600)
    verdict = guard.check("post", "member", now=now)
    assert not verdict.ok
    assert "1 post a day" in verdict.reason
    assert verdict.next_at is not None and verdict.next_at > now


def test_six_hours_must_pass_between_posts_even_across_midnight(guard):
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_late = (midnight - timedelta(hours=1)).timestamp()
    _sent(CREATE, _post(), at=yesterday_late)
    early = (midnight + timedelta(hours=2)).timestamp()
    verdict = guard.check("post", "member", now=early)
    assert not verdict.ok and "6 hours" in verdict.reason
    assert abs(verdict.next_at - (yesterday_late + 6 * 3600)) < 1
    assert guard.check("post", "member", now=(midnight + timedelta(hours=6)).timestamp()).ok


def test_a_post_that_errored_still_counts(guard):
    """It may have gone out: 2026-10-01 is why."""
    now = NOON
    _sent(CREATE, _post(), at=now - 3600, state="submitted")
    assert not guard.check("post", "member", now=now).ok
    import business_store
    business_store.clear_completed()
    assert not guard.check("post", "member", now=now).ok, "clearing the desk does not unsend it"


def test_a_dry_run_and_a_refusal_do_not_count(guard):
    now = NOON
    _sent(CREATE, _post(confirm=False), at=now - 3600)
    _sent(CREATE, _post("other"), at=now - 3600, state="rejected")
    assert guard.check("post", "member", now=now).ok


def test_five_comments_a_day_and_not_six(guard):
    now = NOON
    for n in range(4):
        _sent(COMMENT, _comment(f"c{n}"), at=now - 3600 - n)
    assert guard.check("comment", "member", now=now).ok
    _sent(COMMENT, _comment("c4"), at=now - 60)
    verdict = guard.check("comment", "member", now=now)
    assert not verdict.ok and "5 comments a day" in verdict.reason


def test_an_api_post_and_a_browser_post_share_one_account(guard):
    now = NOON
    _sent("post", {"target": {}, "request": {"account": "member", "text": "x"}},
          at=now - 3600, provider="linkedin")
    assert not guard.check("post", "member", now=now).ok
    assert guard.check("post", "organization", now=now).ok, "the company page is its own account"


def test_the_owner_can_step_the_limits_up_later(guard, monkeypatch):
    now = NOON + 3 * 3600
    _sent(CREATE, _post(), at=NOON - 4 * 3600)
    monkeypatch.setenv("LINKEDIN_POSTS_PER_DAY", "2")
    assert guard.check("post", "member", now=now).ok


# --- the stop --------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "Let's do a quick security check",
    "Please complete this CAPTCHA to continue",
    "We've detected unusual activity on your account",
    "Your account has been temporarily restricted",
    "A LinkedIn login window is open and login is still in progress.",
    "redirected to https://www.linkedin.com/checkpoint/challenge/AgF",
    "HTTP 999",
])
def test_these_mean_stop(guard, said):
    assert guard.looks_like_challenge(said), said


@pytest.mark.parametrize("said", [
    "Posted. It is at the top of the feed.",
    "Commented. It is under the post.",
    "Dry run: the composer held this text and was discarded.",
])
def test_these_do_not(guard, said):
    assert not guard.looks_like_challenge(said), said


def test_a_halt_is_kept_until_the_owner_resumes(guard):
    assert guard.halted() is None
    first = guard.halt("Let's do a quick security check", source=CREATE)
    assert guard.halted()["reason"] == first["reason"]
    guard.halt("something later", source=READ)
    assert "security check" in guard.halted()["reason"], "the first cause is the one kept"
    importlib.reload(guard)
    assert guard.halted() is not None, "it survives a restart"
    guard.resume()
    assert guard.halted() is None


# --- the gate enforces it ------------------------------------------------------

class _Turn:
    current_origin = "user"

    def own_tool_names(self, cli_name):
        return frozenset({cli_name.split("__", 2)[2]})

    def own_tools_read_only(self, cli_name):
        return False

    async def stop(self):
        pass


@pytest.fixture
def wired(guard, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import run_store
    importlib.reload(run_store)
    import tool_log
    importlib.reload(tool_log)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    tool_log.init_db()
    monkeypatch.setattr(server_module, "GATE_APPROVAL_WAIT_SEC", 0.05)
    return server_module


class _client:
    def __init__(self, server):
        from fastapi.testclient import TestClient
        self.server, self.client = server, TestClient(server.app)

    def __enter__(self):
        entered = self.client.__enter__()
        self.server.brain_instance = _Turn()
        return entered

    def __exit__(self, *exc):
        return self.client.__exit__(*exc)


def _pre(client, tool, tool_input, tool_use_id="toolu_x"):
    import data_paths
    r = client.post("/internal/pretool",
                    headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"},
                    json={"tool_name": tool, "tool_input": tool_input, "tool_use_id": tool_use_id})
    out = r.json()["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def _cards(operation=CREATE):
    import business_store
    return [a for a in business_store.list_actions() if a["operation"] == operation]


def test_a_post_over_the_limit_is_refused_before_any_card(wired):
    _sent(CREATE, _post("earlier"), at=time.time() - 3600)
    with _client(wired) as client:
        decision, reason = _pre(client, CREATE, _post("a second one today"))
    assert decision == "deny" and "LinkedIn limit" in reason
    assert [c for c in _cards() if c["state"] == "pending"] == [], \
        "nothing is put to the owner that could not go out"


def test_an_approval_spent_after_the_limit_filled_does_not_post(wired):
    """Two cards staged, both approved: only the first goes."""
    import business_store
    with _client(wired) as client:
        _pre(client, CREATE, _post("one"), "toolu_1")
        _pre(client, CREATE, _post("two"), "toolu_2")
        for card in _cards():
            business_store.transition(card["id"], card["digest"], "pending", "approved")
        assert _pre(client, CREATE, _post("one"), "toolu_3")[0] == "allow"
        decision, reason = _pre(client, CREATE, _post("two"), "toolu_4")
    assert decision == "deny" and "LinkedIn limit" in reason
    two = [c for c in _cards() if c["payload"]["text"] == "two"][0]
    assert business_store.get_action(two["id"])["state"] == "approved", \
        "the approval is not burnt: it can go once the limit allows"


def test_a_halt_stops_every_linkedin_call_reads_too(wired):
    import linkedin_guard
    linkedin_guard.halt("Let's do a quick security check", source=CREATE)
    with _client(wired) as client:
        read = _pre(client, READ, {})
        post = _pre(client, CREATE, _post())
        other = _pre(client, "mcp__paperclip__paperclipListIssues", {})
    assert read[0] == "deny" and "stopped" in read[1].lower()
    assert post[0] == "deny"
    assert other[0] == "allow", "only LinkedIn stops"
    assert _cards() == []


def test_a_challenge_in_a_result_halts_linkedin_and_tells_the_owner(wired, monkeypatch):
    import linkedin_guard
    told = []
    monkeypatch.setattr(wired, "_tell_owner_linkedin_halted",
                        lambda record: told.append(record), raising=True)
    import data_paths
    with _client(wired) as client:
        assert _pre(client, READ, {}, "toolu_feed")[0] == "allow"
        client.post("/internal/posttool",
                    headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"},
                    json={"hook_event_name": "PostToolUseFailure", "tool_name": READ,
                          "tool_input": {}, "tool_use_id": "toolu_feed",
                          "error": "Navigation ended at /checkpoint/challenge: Let's do a quick security check"})
    assert linkedin_guard.halted() is not None
    assert told and "security check" in told[0]["reason"]


def test_feed_text_that_mentions_a_captcha_does_not_halt(wired):
    """A read's CONTENT is somebody else's words; only its own status counts."""
    import linkedin_guard
    import data_paths
    with _client(wired) as client:
        _pre(client, READ, {}, "toolu_feed")
        body = {"status": "ok", "posts": [{"text": "We hit a captcha and unusual activity " * 20}]}
        client.post("/internal/posttool",
                    headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"},
                    json={"hook_event_name": "PostToolUse", "tool_name": READ, "tool_input": {},
                          "tool_use_id": "toolu_feed",
                          "tool_response": [{"type": "text", "text": json.dumps(body)}]})
    assert linkedin_guard.halted() is None


def test_only_the_owner_resumes(wired):
    """Not the brain: the tool token is refused, the desk's Origin is not."""
    import linkedin_guard
    import data_paths
    linkedin_guard.halt("captcha", source=CREATE)
    with _client(wired) as client:
        token = client.post("/api/linkedin/resume",
                            headers={"Authorization": f"Bearer {data_paths.ensure_tool_token()}"})
        assert token.status_code in (401, 403)
        assert linkedin_guard.halted() is not None
        desk = client.post("/api/linkedin/resume", headers={"Origin": "http://localhost:5173"})
        assert desk.status_code == 200, desk.text
    assert linkedin_guard.halted() is None


def test_the_status_says_the_limits_and_any_halt(wired):
    with _client(wired) as client:
        status = client.get("/api/linkedin/status").json()
    assert status["limits"]["posts_per_day"] == 1
    assert status["limits"]["comments_per_day"] == 5
    assert status["limits"]["min_post_gap_hours"] == 6
    assert status["halted"] is None
