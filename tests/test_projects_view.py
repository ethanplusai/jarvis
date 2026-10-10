"""The Projects tab JOIN: session_watch + run_store + builds + repo_read,
folded into one summary per project and ordered by what deserves attention.
"""

import time

import pytest

import projects_view as pv
import run_store
import session_watch as sw


def _session(session_id, project, cwd, state=sw.IDLE, since=None, started=None):
    return sw.SessionState(
        session_id=session_id, cwd=cwd, project=project, state=state,
        roster_name=project, voice_name=project, steerable=True,
        since=since, started=started,
    )


def _run(project_name, project_path, status=run_store.RunStatus.SUCCEEDED,
        created_at=1000.0):
    return {
        "id": f"run-{project_name}-{created_at}",
        "project_name": project_name,
        "project_path": project_path,
        "status": status,
        "created_at": created_at,
        "prompt": "do the thing",
    }


ALWAYS_EXISTS = lambda p: True
NEVER_EXISTS = lambda p: False


# --- the join ----------------------------------------------------------------

def test_a_project_known_only_from_a_session_appears(tmp_path):
    views = pv.build_project_views(
        [_session("s1", "chitauri", "/p/chitauri")], [], exists=ALWAYS_EXISTS)
    assert [v.name for v in views] == ["chitauri"]
    assert views[0].primary_path == "/p/chitauri"
    assert views[0].runs == []


def test_a_project_known_only_from_a_run_appears():
    """A build can finish and its session age out of the roster's gone-cache
    long before anyone opens the dashboard; the run alone must still surface
    the project."""
    views = pv.build_project_views(
        [], [_run("hammer", "/p/hammer")], exists=ALWAYS_EXISTS)
    assert [v.name for v in views] == ["hammer"]
    assert views[0].primary_path == "/p/hammer"
    assert views[0].sessions == []


def test_a_project_seen_in_both_is_joined_not_duplicated():
    views = pv.build_project_views(
        [_session("s1", "chitauri", "/p/chitauri", since=500.0)],
        [_run("chitauri", "/p/chitauri", created_at=200.0)],
        exists=ALWAYS_EXISTS)
    assert len(views) == 1
    v = views[0]
    assert len(v.sessions) == 1
    assert len(v.runs) == 1


def test_runs_of_other_projects_are_not_pulled_in():
    views = pv.build_project_views(
        [_session("s1", "chitauri", "/p/chitauri")],
        [_run("hammer", "/p/hammer")], exists=ALWAYS_EXISTS)
    names = {v.name for v in views}
    assert names == {"chitauri", "hammer"}
    chitauri = next(v for v in views if v.name == "chitauri")
    assert chitauri.runs == []


def test_runs_are_sorted_most_recent_first():
    views = pv.build_project_views(
        [], [_run("p", "/p", created_at=1.0), _run("p", "/p", created_at=99.0)],
        exists=ALWAYS_EXISTS)
    assert [r["created_at"] for r in views[0].runs] == [99.0, 1.0]


# --- directory existence ------------------------------------------------------

def test_directory_exists_is_reported_honestly():
    views = pv.build_project_views(
        [_session("s1", "chitauri", "/p/chitauri")], [], exists=NEVER_EXISTS)
    assert views[0].directory_exists is False


def test_a_project_with_no_known_path_never_calls_exists():
    """No session cwd and no run project_path: nothing to check, and no
    primary_path to hand to a filesystem call."""
    calls = []

    def spy(p):
        calls.append(p)
        return True

    r = _run("ghost", "", created_at=1.0)
    views = pv.build_project_views([], [r], exists=spy)
    assert views[0].primary_path == ""
    assert views[0].directory_exists is False
    assert calls == []


# --- primary path when a project spans several directories --------------------

def test_primary_path_is_the_most_recently_active_one():
    views = pv.build_project_views(
        [_session("old", "jarvis", "/old/jarvis", since=100.0),
         _session("new", "jarvis", "/new/jarvis", since=900.0)],
        [], exists=ALWAYS_EXISTS)
    assert views[0].primary_path == "/new/jarvis"
    assert views[0].paths == ["/new/jarvis", "/old/jarvis"]


def test_primary_path_considers_runs_too():
    views = pv.build_project_views(
        [_session("s", "jarvis", "/old/jarvis", since=100.0)],
        [_run("jarvis", "/new/jarvis", created_at=900.0)],
        exists=ALWAYS_EXISTS)
    assert views[0].primary_path == "/new/jarvis"


def test_primary_path_ties_break_alphabetically():
    views = pv.build_project_views(
        [_session("a", "jarvis", "/b/jarvis"), _session("b", "jarvis", "/a/jarvis")],
        [], exists=ALWAYS_EXISTS)
    assert views[0].primary_path == "/a/jarvis"


# --- needs_you and active ------------------------------------------------------

def test_needs_you_sessions_are_collected():
    views = pv.build_project_views(
        [_session("s1", "chitauri", "/p", state=sw.NEEDS_YOU),
         _session("s2", "chitauri", "/p", state=sw.IDLE)],
        [], exists=ALWAYS_EXISTS)
    assert [s.session_id for s in views[0].needs_you] == ["s1"]


def test_a_working_session_makes_a_project_active():
    views = pv.build_project_views(
        [_session("s1", "p", "/p", state=sw.WORKING)], [], exists=ALWAYS_EXISTS)
    assert views[0].active is True


def test_an_idle_session_does_not_make_a_project_active():
    views = pv.build_project_views(
        [_session("s1", "p", "/p", state=sw.IDLE)], [], exists=ALWAYS_EXISTS)
    assert views[0].active is False


def test_an_active_run_makes_a_project_active_with_no_live_session():
    views = pv.build_project_views(
        [], [_run("p", "/p", status=run_store.RunStatus.RUNNING)],
        exists=ALWAYS_EXISTS)
    assert views[0].active is True


def test_a_finished_run_does_not_make_a_project_active():
    views = pv.build_project_views(
        [], [_run("p", "/p", status=run_store.RunStatus.SUCCEEDED)],
        exists=ALWAYS_EXISTS)
    assert views[0].active is False


# --- ordering: needs-you, then active, then recency ---------------------------

def test_needs_you_projects_sort_first():
    views = pv.build_project_views(
        [_session("a", "aaa", "/a", state=sw.WORKING),
         _session("b", "zzz", "/z", state=sw.NEEDS_YOU)],
        [], exists=ALWAYS_EXISTS)
    assert [v.name for v in views] == ["zzz", "aaa"]


def test_active_sorts_before_quiet_recency():
    views = pv.build_project_views(
        [_session("a", "recent", "/r", state=sw.IDLE, since=900.0),
         _session("b", "working", "/w", state=sw.WORKING, since=1.0)],
        [], exists=ALWAYS_EXISTS)
    assert [v.name for v in views] == ["working", "recent"]


def test_within_a_bucket_recency_wins():
    views = pv.build_project_views(
        [_session("a", "older", "/o", since=100.0),
         _session("b", "newer", "/n", since=500.0)],
        [], exists=ALWAYS_EXISTS)
    assert [v.name for v in views] == ["newer", "older"]


def test_never_active_never_seen_sorts_last_by_stable_name_order():
    views = pv.build_project_views(
        [_session("a", "b-project", "/b"), _session("c", "a-project", "/a")],
        [], exists=ALWAYS_EXISTS)
    # Neither has any activity timestamp at all: falls back to name.
    assert [v.name for v in views] == ["a-project", "b-project"]


# --- the expensive half: repo + build summaries --------------------------------

def test_repo_summary_says_the_directory_is_gone(tmp_path):
    missing = str(tmp_path / "nope")
    out = pv.repo_summary(missing, "nope")
    assert out == {"exists": False, "headline": "", "body": ""}


def test_repo_summary_reuses_repo_read_overview(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n\nA small tool.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    out = pv.repo_summary(str(tmp_path), "demo")
    assert out["exists"] is True
    assert "demo" in out["headline"]
    assert "Python" in out["headline"]


def test_build_summary_with_no_docs_dir_at_all(tmp_path):
    out = pv.build_summary(str(tmp_path))
    assert out == {"has_spec": False, "has_plan": False, "progress": None}


def test_build_summary_with_a_missing_directory():
    out = pv.build_summary("/definitely/not/a/real/path")
    assert out == {"has_spec": False, "has_plan": False, "progress": None}


def test_build_summary_reports_spec_and_progress(tmp_path):
    spec_dir = tmp_path / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "2026-01-01-thing-design.md").write_text("# Thing\n", encoding="utf-8")

    plan_dir = tmp_path / "docs" / "superpowers" / "plans"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.md").write_text(
        "## Task 1: First\n- [x] step one\n- [ ] step two\n"
        "## Task 2: Second\n- [ ] step one\n", encoding="utf-8")

    out = pv.build_summary(str(tmp_path))
    assert out["has_spec"] is True
    assert out["has_plan"] is True
    assert out["progress"]["total"] == 2
    assert out["progress"]["done"] == 0
    assert out["progress"]["current_task"]["title"] == "First"


# --- JSON shapes ---------------------------------------------------------------

def test_a_project_whose_conversations_are_all_dead_says_so():
    """`session_count` counts DEAD ones too — `gone` and `fresh` are both in
    it — and the page turned any non-zero count into a green "idle" dot. A
    project nobody has a window open on is not idle; it is finished.
    """
    views = pv.build_project_views(
        [_session("s1", "p", "/p", state=sw.GONE),
         _session("s2", "p", "/p", state=sw.FRESH)],
        [], exists=ALWAYS_EXISTS)
    item = pv.list_item(views[0])

    assert item["session_count"] == 2
    assert item["live_session_count"] == 0


def test_a_live_conversation_counts_as_live():
    views = pv.build_project_views(
        [_session("s1", "p", "/p", state=sw.IDLE),
         _session("s2", "p", "/p", state=sw.GONE)],
        [], exists=ALWAYS_EXISTS)

    assert pv.list_item(views[0])["live_session_count"] == 1


def test_list_item_has_no_sessions_or_runs_payload():
    views = pv.build_project_views(
        [_session("s1", "p", "/p")], [_run("p", "/p")], exists=ALWAYS_EXISTS)
    item = pv.list_item(views[0])
    assert "sessions" not in item
    assert "runs" not in item
    assert item["session_count"] == 1
    assert item["latest_run"]["id"].startswith("run-p-")


def test_detail_item_carries_full_sessions_and_runs():
    views = pv.build_project_views(
        [_session("s1", "p", "/p")], [_run("p", "/p")], exists=ALWAYS_EXISTS)
    item = pv.detail_item(views[0], {"exists": False, "headline": "", "body": ""},
                          {"has_spec": False, "has_plan": False, "progress": None})
    assert item["sessions"][0]["session_id"] == "s1"
    assert len(item["runs"]) == 1
    assert item["repo"]["exists"] is False
    assert item["build"]["has_spec"] is False


# --- the endpoint must not run this on the event loop -----------------------
#
# `_project_views()` is a SQLite read of up to 500 runs plus an `os.path.isdir`
# per project. Every sibling endpoint is `to_thread`'d and the list one was
# not, so an open Projects tab put that walk on the event loop every ten
# seconds — and an `isdir` on a sleeping external drive blocks the voice
# channel for as long as the disk takes to spin up.

def test_the_list_endpoint_does_not_freeze_the_event_loop(monkeypatch):
    import asyncio
    import server

    def slow():
        time.sleep(0.5)          # stands in for a disk that has to spin up
        return []

    monkeypatch.setattr(server, "_project_views", slow)

    async def go():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        body = await server.api_projects_view()
        beat.cancel()
        return body, ticks

    body, ticks = asyncio.run(go())

    assert body["projects"] == []
    assert ticks > 5, f"the event loop was blocked for the whole walk ({ticks=})"


def test_the_detail_endpoint_does_not_freeze_the_event_loop(monkeypatch):
    """The detail endpoint threads its repo walk but called `_project_views()`
    inline first — the same blocking read, on the same loop."""
    import asyncio
    import server

    def slow():
        time.sleep(0.5)
        return []

    monkeypatch.setattr(server, "_project_views", slow)

    async def go():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        response = await server.api_project_view_detail("nope")
        beat.cancel()
        return response, ticks

    response, ticks = asyncio.run(go())

    assert response.status_code == 404
    assert ticks > 5, f"the event loop was blocked for the whole walk ({ticks=})"


def test_the_open_endpoint_does_not_freeze_the_event_loop(monkeypatch):
    """The last of the three. A single click rather than a ten-second poll,
    but the same walk on the same loop, and a disk does not know the
    difference."""
    import asyncio
    import server

    def slow():
        time.sleep(0.5)
        return []

    monkeypatch.setattr(server, "_project_views", slow)

    async def go():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        response = await server.api_project_open(
            server.ProjectOpenRequest(name="nope", path="/nowhere", target="editor"))
        beat.cancel()
        return response, ticks

    response, ticks = asyncio.run(go())

    assert response.status_code in (400, 404)
    assert ticks > 5, f"the event loop was blocked for the whole walk ({ticks=})"
