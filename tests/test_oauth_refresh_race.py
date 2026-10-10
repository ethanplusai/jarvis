"""A login that is busy is not a login that has expired.

Measured 2026-09-26, the first time JARVIS was started by its sign-in task
with other Claude Code processes running: the brain's warm-up failed with

    Failed to refresh OAuth token: another Claude Code process is refreshing
    it or exited mid-refresh. This is usually transient; retry in a minute,
    and if it persists close other Claude Code processes.

It contains "OAuth", so `_classify_fatal_failure` read it as the expired
login the fatal rule exists for, and JARVIS gave up for good: "Claude Code's
OAuth login has expired ... log in, then restart JARVIS". The login was fine.
Restarting the brain by hand two minutes later worked at once.

At sign-in that race is the likely case, not a rare one: the desktop app and
any other Claude Code process start at the same moment and refresh the same
token. So the race is transient, and it is retried at the pace the CLI asks
for -- not three times inside four seconds, which would all land inside the
other process's refresh.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_brain import _config, _wait_until  # noqa: E402

import brain  # noqa: E402

RACE = ("Failed to refresh OAuth token: another Claude Code process is refreshing it "
        "or exited mid-refresh. This is usually transient; retry in a minute, and if it "
        "persists close other Claude Code processes.")
EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


def test_the_refresh_race_is_not_an_expired_login():
    assert brain._classify_fatal_failure(RACE) is None


def test_an_expired_login_is_still_fatal():
    """The rule this narrows exists for a real incident: an expired login
    burned the whole restart budget in five seconds. That stays fatal."""
    assert brain._classify_fatal_failure(EXPIRED) == "auth"


@pytest.mark.asyncio
async def test_a_start_that_loses_the_race_comes_up_on_the_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "AUTH_REFRESH_RETRY_SEC", 0.6)
    flag = tmp_path / "race-once"
    flag.write_text("x", encoding="utf-8")
    monkeypatch.setenv("FAKE_BRAIN_REFRESH_RACE_ONCE", str(flag))
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=3))
    try:
        b.on_state(lambda s, info: states.append((s, info)))
        started = time.monotonic()
        assert await b.start() is False, "the first warm-up lost the race"
        assert not b.failed, "a busy login must not be declared expired"

        assert await _wait_until(lambda: b.ready, 15.0), "the retry never came up"
        waited = time.monotonic() - started

        assert b.failure_reason is None
        assert not any(s == "failed" for s, _ in states), states
        assert b.generation == 2
        assert waited >= 0.6, f"retried after {waited:.2f}s, inside the other refresh"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_race_that_persists_is_retried_at_the_clis_pace_and_never_called_expired(
        tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "AUTH_REFRESH_RETRY_SEC", 0.4)
    monkeypatch.setenv("FAKE_BRAIN_FORCE", "REFRESHRACE")
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=2))
    try:
        b.on_state(lambda s, info: states.append((s, info)))
        started = time.monotonic()
        assert await b.start() is False
        assert await _wait_until(lambda: b.failed, 15.0)
        waited = time.monotonic() - started

        assert b.failure_reason is None, "it gave up, but not by claiming the login expired"
        backoffs = [info.get("backoff") for s, info in states if s == "restarting"]
        assert len(backoffs) == 2 and all(x >= 0.4 for x in backoffs), backoffs
        assert waited >= 0.8
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_an_ordinary_failure_keeps_the_quick_first_retry(tmp_path, monkeypatch):
    """Only the refresh race waits. Anything else still gets the restart
    loop's own half-second first backoff."""
    monkeypatch.setattr(brain, "AUTH_REFRESH_RETRY_SEC", 30.0)
    monkeypatch.setenv("FAKE_BRAIN_FORCE", "APIERROR")
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=1))
    try:
        b.on_state(lambda s, info: states.append((s, info)))
        assert await b.start() is False
        assert await _wait_until(lambda: b.failed, 8.0)
        backoffs = [info.get("backoff") for s, info in states if s == "restarting"]
        assert backoffs == [0.5], backoffs
    finally:
        await b.stop()
