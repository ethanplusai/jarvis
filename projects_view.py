"""The Projects tab: a JOIN of what already exists, nothing new captured.

The user's words: "we need in the dashboard the projects where you can click
into them to see more info about each project, the repo location, etc." Every
fact this module surfaces is already recorded somewhere else —
`session_watch.py` knows the conversations, `run_store.py` knows the runs,
`builds.py` knows the plan, `repo_read.py` knows the repository. This module
only joins them by project name and orders the result.

Two passes, deliberately split by cost:

  * `build_project_views` is the CHEAP join — sessions and runs already held
    in memory, plus one `os.path.isdir` per project. It is what backs the
    list on the left of the master-detail view, and every project on the
    machine can be joined on every poll without walking a single repository.
  * `repo_summary` / `build_summary` are the EXPENSIVE half — a bounded
    filesystem walk (`repo_read.overview`) and a plan-file read
    (`builds.plan_progress`) — computed only for the one project a user has
    actually clicked into, off the event loop.

Three honesty rules this module exists to uphold (see CLAUDE.md):

  1. A project with no runs is `runs: []`, never a run count coerced to 0 and
     drawn as a measurement.
  2. A project's directory can be gone by the time someone clicks it —
     `directory_exists` says so plainly rather than a detail pane rendering
     empty as though the project were merely quiet.
  3. JARVIS's own spawned runs are not the user's conversations. This module
     takes `sessions` as an argument rather than reading the roster itself,
     so the caller supplies `server._snapshot_or_empty()` — which already
     filters JARVIS's own run sessions out — not the raw snapshot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import builds
import repo_read
import run_store
import session_watch

# Session states that mean "something is actively happening here right now",
# for the purpose of the project ordering rule. `needs_you` is its own,
# higher-priority bucket and is deliberately not included here.
_ACTIVE_SESSION_STATES = frozenset({session_watch.WORKING, session_watch.SHELL})

# Conversations that have STOPPED being conversations. `session_count`
# includes them, which is right for "what does this project know about" and
# wrong for "is anything going on here": a project whose windows have all
# been closed rendered with the same green dot as one somebody is sitting
# at. Counted separately rather than removed, because both questions have a
# reader.
_DEAD_SESSION_STATES = frozenset({session_watch.GONE, session_watch.FRESH})

# How many of a project's most recent runs the detail view carries. "Its
# runs: recent" — not the project's whole history.
DETAIL_RUN_LIMIT = 20


@dataclass
class ProjectView:
    """One project: every conversation and run known for it, joined."""
    name: str
    primary_path: str
    paths: list[str] = field(default_factory=list)
    directory_exists: bool = False
    sessions: list[session_watch.SessionState] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    needs_you: list[session_watch.SessionState] = field(default_factory=list)
    active: bool = False
    last_activity: float | None = None

    @property
    def latest_run(self) -> dict | None:
        return self.runs[0] if self.runs else None


def _session_activity(s: session_watch.SessionState) -> float | None:
    """When this conversation last did something, for ordering purposes.

    `since` (when the CURRENT state began) is preferred over `started` (when
    the conversation began) for the same reason `Snapshot.needing_you` prefers
    it: a conversation that has sat idle for days must not out-rank one that
    just started, and vice versa.
    """
    return s.since if s.since is not None else s.started


def _pick_primary_path(paths: list[str],
                       sessions: list[session_watch.SessionState],
                       runs: list[dict]) -> str:
    """Which of a project's known directories to call "the" repo location.

    A project can genuinely live in more than one directory — the same repo
    checked out twice, or a Claude Code worktree under `.claude/worktrees/`
    (see `session_watch.project_name`, which deliberately collapses those to
    one project). Rather than guess, the directory with the most RECENT
    activity against it wins; ties break alphabetically so the choice is
    stable across polls.
    """
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    scores: dict[str, float] = {}
    for s in sessions:
        if not s.cwd:
            continue
        t = _session_activity(s) or 0.0
        scores[s.cwd] = max(scores.get(s.cwd, 0.0), t)
    for r in runs:
        p = r.get("project_path")
        if not p:
            continue
        t = r.get("created_at") or 0.0
        scores[p] = max(scores.get(p, 0.0), t)
    return min(paths, key=lambda p: (-scores.get(p, 0.0), p))


def build_project_views(
    sessions: list[session_watch.SessionState],
    runs: list[dict],
    *, known: list[dict] = (),
    exists=os.path.isdir,
) -> list[ProjectView]:
    """The JOIN: every project name known to a conversation, a run or the
    folder scan, folded into one summary each, ordered by what deserves
    attention first.

    `known` is the folder scan (`{"name", "path"}` rows): repos with no
    conversation and no run yet. They join with no activity, so they sort
    after everything that has some — present, never promoted.

    `sessions` and `runs` are handed in rather than read here so this stays a
    pure function over data the caller already fetched — no I/O beyond one
    `exists()` check per project, which callers can stub out in tests.
    """
    buckets: dict[str, dict] = {}

    def bucket(name: str) -> dict:
        return buckets.setdefault(name, {"sessions": [], "runs": [], "paths": set()})

    for s in sessions:
        if not s.project:
            continue
        b = bucket(s.project)
        b["sessions"].append(s)
        if s.cwd:
            b["paths"].add(s.cwd)

    for r in runs:
        name = r.get("project_name")
        if not name:
            continue
        b = bucket(name)
        b["runs"].append(r)
        path = r.get("project_path")
        if path:
            b["paths"].add(path)

    for k in known:
        name, path = k.get("name"), k.get("path")
        if name and path:
            bucket(name)["paths"].add(path)

    views: list[ProjectView] = []
    for name, b in buckets.items():
        paths = sorted(b["paths"])
        runs_sorted = sorted(
            b["runs"], key=lambda r: r.get("created_at") or 0, reverse=True)
        sessions_list = b["sessions"]
        needs_you = [s for s in sessions_list if s.state == session_watch.NEEDS_YOU]
        active_session = any(s.state in _ACTIVE_SESSION_STATES for s in sessions_list)
        active_run = any(r.get("status") in run_store.RunStatus.ACTIVE
                         for r in runs_sorted)

        candidates = [t for t in (_session_activity(s) for s in sessions_list)
                     if t is not None]
        candidates += [r["created_at"] for r in runs_sorted
                      if r.get("created_at") is not None]
        last_activity = max(candidates) if candidates else None

        primary_path = _pick_primary_path(paths, sessions_list, runs_sorted)
        views.append(ProjectView(
            name=name,
            primary_path=primary_path,
            paths=paths,
            directory_exists=bool(primary_path) and exists(primary_path),
            sessions=sessions_list,
            runs=runs_sorted[:DETAIL_RUN_LIMIT],
            needs_you=needs_you,
            active=active_session or active_run,
            last_activity=last_activity,
        ))

    return order_projects(views)


def order_projects(views: list[ProjectView]) -> list[ProjectView]:
    """Anything needing the user first, then anything active, then the rest
    by recency. Never alphabetical — that would bury the project someone is
    actually waiting on below one whose name starts with "a"."""
    def key(v: ProjectView):
        urgency = 0 if v.needs_you else (1 if v.active else 2)
        return (urgency, -(v.last_activity or 0.0), v.name.lower())
    return sorted(views, key=key)


# --- The expensive half: only for the one project a user clicked into ------

def repo_summary(path: str, name: str) -> dict:
    """`repo_overview`'s own facts, exactly as it computes them — no second
    repository reader. `exists: False` when the directory is gone, so a
    detail pane can say so instead of rendering an empty summary."""
    if not path or not Path(path).is_dir():
        return {"exists": False, "headline": "", "body": ""}
    headline, body = repo_read.overview(Path(path), name)
    return {"exists": True, "headline": headline, "body": body}


def _plan_task_dict(t: "builds.PlanTask") -> dict:
    return {
        "number": t.number, "title": t.title,
        "steps_done": t.steps_done, "steps_total": t.steps_total,
        "done": t.done,
    }


def build_summary(path: str) -> dict:
    """Spec presence, plan presence, and progress off the plan's own
    checkboxes — reusing `builds.plan_progress`, which already parses them,
    rather than re-reading the plan file here."""
    if not path or not Path(path).is_dir():
        return {"has_spec": False, "has_plan": False, "progress": None}

    root = Path(path)
    try:
        has_spec = (root / builds.SPEC_DIR).is_dir() and \
            any((root / builds.SPEC_DIR).glob("*.md"))
    except OSError:
        has_spec = False
    try:
        has_plan = (root / builds.PLAN_DIR).is_dir() and \
            any((root / builds.PLAN_DIR).glob("*.md"))
    except OSError:
        has_plan = False

    progress = builds.plan_progress(path)
    progress_dict = None
    if progress is not None:
        current = progress.current
        progress_dict = {
            "total": progress.total,
            "done": progress.done,
            "finished": progress.finished,
            "current_task": _plan_task_dict(current) if current else None,
            "tasks": [_plan_task_dict(t) for t in progress.tasks],
        }
    return {"has_spec": has_spec, "has_plan": has_plan, "progress": progress_dict}


# --- JSON shapes -------------------------------------------------------------

def list_item(v: ProjectView) -> dict:
    """The cheap shape: everything the left-hand project list needs, and
    nothing that required a filesystem walk."""
    return {
        "name": v.name,
        "primary_path": v.primary_path,
        "paths": v.paths,
        "directory_exists": v.directory_exists,
        "session_count": len(v.sessions),
        # Of those, the ones that are still conversations — see
        # _DEAD_SESSION_STATES. A count of zero here with a non-zero
        # `session_count` is a project that has finished, not one that is
        # idling.
        "live_session_count": sum(
            1 for s in v.sessions if s.state not in _DEAD_SESSION_STATES),
        "needs_you_count": len(v.needs_you),
        "active": v.active,
        "last_activity": v.last_activity,
        "latest_run": v.latest_run,
    }


def detail_item(v: ProjectView, repo: dict, build: dict) -> dict:
    """The full shape for the right-hand detail pane."""
    out = list_item(v)
    out["sessions"] = [session_watch.session_to_dict(s) for s in v.sessions]
    out["runs"] = v.runs
    out["repo"] = repo
    out["build"] = build
    return out
