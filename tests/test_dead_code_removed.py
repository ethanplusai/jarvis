"""Task 10 — the superseded spawn machinery is gone, and every Claude-spawning
path that is actually reachable goes through `RunExecutor` and therefore shows
up in the run store.

Those paths were the inline `[ACTION:BUILD]` block in `voice_handler` and
`self_work_and_notify`. `handle_build` was NOT one of them: its only caller
was `_execute_build`, which nothing called, so five tests here were exercising
dead code while the live build path had none. Both are now deleted and the
live path is covered below instead.

`self_work_and_notify` has since gone the same way, along with the rest of
the voice dispatch chain and the `anthropic` dependency that only it and
`_report_run_result` kept alive — see `test_no_anthropic_sdk.py`. The live
spawn path is the `spawn_run` tool (`test_spawn_run_tool.py`)."""

import asyncio
import importlib
import json
import pathlib
import sys
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).parent.parent))
ROOT = Path(__file__).parent.parent


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    return server


# ---------------------------------------------------------------------------
# Deletions
# ---------------------------------------------------------------------------

def test_dispatch_registry_module_deleted():
    assert not (ROOT / "dispatch_registry.py").exists()


def test_claude_task_manager_gone(env):
    assert not hasattr(env, "ClaudeTaskManager")
    assert not hasattr(env, "task_manager")
    assert not hasattr(env, "ClaudeTask")


def test_focus_terminal_window_gone(env):
    assert not hasattr(env, "_focus_terminal_window")


def test_monitor_build_gone():
    import actions
    importlib.reload(actions)
    assert not hasattr(actions, "monitor_build")
    assert not hasattr(actions, "open_claude_in_project")


def test_work_session_gone():
    import work_mode
    importlib.reload(work_mode)
    assert not hasattr(work_mode, "WorkSession")
    assert not hasattr(work_mode, "SESSION_FILE")


def test_is_casual_question_survives():
    import work_mode
    importlib.reload(work_mode)
    assert work_mode.is_casual_question("what time is it") is True
    assert work_mode.is_casual_question("build me a dashboard") is False


def test_no_sentinel_string_remains():
    text = (ROOT / "actions.py").read_text(encoding="utf-8")
    assert "JARVIS TASK COMPLETE" not in text
    assert "JARVIS TASK COMPLETE" not in (ROOT / "server.py").read_text(encoding="utf-8")


def test_active_session_json_not_referenced():
    for name in ("server.py", "work_mode.py"):
        assert "active_session.json" not in (ROOT / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Migrations — both remaining spawn paths must record a run
# ---------------------------------------------------------------------------

class FakeExecutor:
    async def shutdown(self):
        pass
    """Records what was spawned and completes it immediately."""

    def __init__(self, store, result_text="Did the thing."):
        self.store = store
        self.result_text = result_text
        self.spawned: list[tuple] = []

    async def start_existing(self, run_id, prompt, project_path,
                             resume_from=None, timeout_sec=0):
        self.store.update_run(run_id, status=self.store.RunStatus.RUNNING)
        return run_id

    async def spawn(self, prompt, project_name, project_path, origin,
                    resume_from=None, timeout_sec=0):
        run_id = self.store.create_run(prompt, project_name, project_path,
                                       origin, resume_from)
        self.spawned.append((run_id, prompt, project_name, project_path,
                             origin, resume_from))
        return await self.start_existing(run_id, prompt, project_path)

    async def wait_for(self, run_id, timeout=30):
        self.store.update_run(run_id, status=self.store.RunStatus.SUCCEEDED,
                              result_text=self.result_text)
        return self.store.get_run(run_id)


def test_handle_build_and_execute_build_are_gone(env):
    """Nothing called `_execute_build`, and it was `handle_build`'s only
    caller — the [ACTION:BUILD] tag is handled inline in voice_handler."""
    assert not hasattr(env, "handle_build")
    assert not hasattr(env, "_execute_build")


def test_execute_research_is_gone(env):
    assert not hasattr(env, "_execute_research")


def test_classify_intent_and_execute_action_are_gone(env):
    """The keyword/LLM intent-classifier pipeline was superseded by the
    [ACTION:X] tags `extract_action` parses out of the response."""
    import actions
    importlib.reload(actions)
    assert not hasattr(env, "classify_intent")
    assert not hasattr(actions, "execute_action")
    assert not hasattr(actions, "prompt_existing_terminal")


def test_executor_injection_into_actions_is_gone(env):
    """`actions.execute_action`'s build branch was the only spawner there, so
    no run will ever have origin == "terminal" any more. That is expected."""
    import actions
    importlib.reload(actions)
    assert not hasattr(actions, "set_executor")
    assert not hasattr(actions, "_executor")
    assert "set_executor" not in (ROOT / "server.py").read_text(encoding="utf-8")


def test_self_work_and_notify_is_gone(env):
    """It was the second of the two RunExecutor-recording spawn paths this
    file was written to protect, and the last thing in the project that
    called the Anthropic SDK. Nothing but these tests ever called it: the
    brain reaches RunExecutor through the `spawn_run` tool now, which is
    covered by tests/test_spawn_run_tool.py. See test_no_anthropic_sdk.py."""
    assert not hasattr(env, "self_work_and_notify")


# ---------------------------------------------------------------------------
# /api/tasks is gone — it was an unguarded alias for POST /api/runs
# ---------------------------------------------------------------------------

@pytest.fixture
def api(env, monkeypatch):
    from fastapi.testclient import TestClient
    import run_store
    server = env
    fake = FakeExecutor(run_store)
    # A route that is gone must read as gone (404/405) rather than as
    # refused (403), so the client speaks with the dashboard's own Origin.
    with TestClient(server.app,
                    headers={"Origin": "http://localhost:5173"}) as c:
        # lifespan runs on enter; install the fake after it, not before.
        monkeypatch.setattr(server, "run_executor_instance", fake)
        yield c, run_store, fake


def test_task_request_model_gone(env):
    assert not hasattr(env, "TaskRequest")


def test_tasks_routes_are_gone(api):
    """POST /api/tasks defaulted working_dir to "." and spawned Claude with
    --dangerously-skip-permissions inside the jarvis repo itself, with none of
    the guards /api/runs applies. The whole alias is removed."""
    c, store, fake = api
    assert c.get("/api/tasks").status_code == 404
    assert c.get("/api/tasks/anything").status_code == 404
    assert c.delete("/api/tasks/anything").status_code == 404
    r = c.post("/api/tasks", json={"prompt": "do a thing", "working_dir": "."})
    assert r.status_code in (404, 405)
    assert store.list_runs(limit=10) == []
    assert fake.spawned == []


def test_fix_self_route_is_gone(api):
    """It shelled osascript to run Claude in the repo outside RunExecutor.
    The `fix_self` WebSocket message is the supported path."""
    c, _store, _fake = api
    assert c.post("/api/fix-self").status_code in (404, 405)


def test_no_tasks_or_fix_self_references_remain():
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "/api/tasks" not in text
    assert "fix-self" not in text


# ---------------------------------------------------------------------------
# JARVIS_SYSTEM_PROMPT / generate_response / handle_research — the dead
# keyword-era LLM path. `_handle_utterance` -> `brain_instance.turn()` -> the
# MCP tool channel is the only live conversational path now, but
# JARVIS_SYSTEM_PROMPT described abilities (screen, calendar, mail, notes)
# the brain does not have. During the milestone-1 live test the user caught
# JARVIS overclaiming exactly these — this text was inert but one
# copy-paste from lying to the user again.
# ---------------------------------------------------------------------------

def test_jarvis_system_prompt_and_generate_response_gone(env):
    assert not hasattr(env, "JARVIS_SYSTEM_PROMPT")
    assert not hasattr(env, "generate_response")
    assert not hasattr(env, "handle_research")


def test_generate_response_only_helpers_gone(env):
    """These existed only to build JARVIS_SYSTEM_PROMPT's context slots for
    generate_response. Once generate_response went, nothing else called
    them (format_runs_for_prompt is the one sibling helper that survives —
    it has its own direct test coverage below)."""
    assert not hasattr(env, "_active_runs_summary")
    assert not hasattr(env, "format_projects_for_prompt")
    assert not hasattr(env, "track_usage")
    assert not hasattr(env, "get_lookup_status")
    assert not hasattr(env, "PROJECT_DIR")


def test_format_runs_for_prompt_survives(env):
    """Unlike its generate_response sibling helpers, this one has its own
    direct test coverage (see test_voice_run_integration.py), so it stays."""
    assert hasattr(env, "format_runs_for_prompt")


def test_no_overclaimed_capability_strings_remain():
    """Guard against the exact class of bug that caused the M1 failure:
    JARVIS_SYSTEM_PROMPT told the user JARVIS could see their screen and
    read their calendar, mail, and notes autonomously — reactivating any of
    that text (even outside JARVIS_SYSTEM_PROMPT by name) would reintroduce
    the overclaim."""
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    banned_phrases = (
        "YOUR CAPABILITIES (these are REAL and ACTIVE",
        "You CAN see what's on",
        "You CAN read {user_name}'s calendar",
        "You CAN read {user_name}'s email",
        "You CAN read Apple Notes and create NEW notes",
        "JARVIS_SYSTEM_PROMPT",
    )
    for phrase in banned_phrases:
        assert phrase not in text, f"overclaim text reintroduced: {phrase!r}"


def test_no_module_level_string_claims_screen_calendar_mail_or_notes():
    """Broader net than the exact-phrase check above: scan every string
    literal in server.py (not just JARVIS_SYSTEM_PROMPT's old contents) for
    a first-person capability claim about screen/calendar/mail/notes access.
    A live, honest string like a docstring naming these subsystems is fine;
    what must never come back is "you/JARVIS CAN <verb> ... screen/calendar/
    mail/notes"."""
    import ast
    import re

    text = (ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    claim_re = re.compile(
        r"\byou can (see|read)\b.{0,40}\b(screen|calendar|mail|email|notes)\b",
        re.IGNORECASE,
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = claim_re.search(node.value)
            assert not match, f"capability-claim string reintroduced: {node.value[:120]!r}"


# ---------------------------------------------------------------------------
# The second batch: the calendar/mail/screen lookup subsystem and the rest of
# the [ACTION:X] tag machinery.
#
# This is not tidiness. `_do_screen_lookup`, `_do_calendar_lookup` and
# `_do_mail_lookup` implemented, in code, exactly the capabilities JARVIS's
# persona says he does NOT have — and during the milestone-1 live test he
# overclaimed those very three. A dead implementation sitting next to an
# honest prompt is an invitation to reconnect it; the prompt text was removed
# in the batch above, and this removes what it would have been reconnected to.
# ---------------------------------------------------------------------------

def test_the_lookup_subsystem_is_gone(env):
    """No caller anywhere in the repo: `_lookup_and_report` was never
    referenced, and the three `_do_*_lookup` coroutines were only ever passed
    to it."""
    assert not hasattr(env, "_lookup_and_report")
    assert not hasattr(env, "_do_calendar_lookup")
    assert not hasattr(env, "_do_mail_lookup")
    assert not hasattr(env, "_do_screen_lookup")
    assert not hasattr(env, "_active_lookups")
    assert not hasattr(env, "_short_sender")      # only _do_mail_lookup called it


def test_the_action_tag_machinery_is_gone(env):
    """`extract_action` parsed [ACTION:X] out of `generate_response`'s reply;
    that function went in the previous batch and nothing replaced the tags.
    `detect_action_fast` was its keyword-router sibling, and the handlers
    below were only ever reached through one of the two."""
    assert not hasattr(env, "extract_action")
    assert not hasattr(env, "detect_action_fast")
    assert not hasattr(env, "handle_browse")
    assert not hasattr(env, "handle_open_terminal")
    assert not hasattr(env, "handle_show_recent")
    assert not hasattr(env, "_execute_open_terminal")
    assert not hasattr(env, "recently_built")     # only handle_show_recent read it


def test_execute_browse_is_gone_too(env):
    """It outlived its siblings only because `_report_run_result` opened the
    localhost URL a finished run reported. That caller has since gone with
    the rest of the voice dispatch chain, leaving this with none — opening a
    URL is `open_url`/`open_path` in TOOL_HANDLERS now (tests/test_open_tools.py).
    `_find_project_dir`, its neighbour, went the same way."""
    assert not hasattr(env, "_execute_browse")
    assert not hasattr(env, "_find_project_dir")


def test_the_session_summary_and_usage_summary_helpers_are_gone(env):
    """`_update_session_summary` belonged to the three-tier memory of the
    keyword era; `get_usage_summary` was its spoken counterpart. /api/usage
    is the live usage path and keeps its own helpers."""
    assert not hasattr(env, "_update_session_summary")
    assert not hasattr(env, "get_usage_summary")
    assert hasattr(env, "_get_usage_for_period"), "the live /api/usage path stays"
    # `_cost_from_tokens` priced tokens at a hard-coded rate on a
    # subscription with no spend to report: a number made up. Gone.
    assert not hasattr(env, "_cost_from_tokens")


def test_server_no_longer_imports_the_screen_calendar_and_mail_readers_it_dropped():
    """The imports the deleted lookups were the only users of. Leaving them
    bound in the module namespace would keep `server.describe_screen` a live
    attribute — one line away from being called again."""
    server_text = (ROOT / "server.py").read_text(encoding="utf-8")
    for name in ("describe_screen", "get_active_windows", "refresh_calendar_cache",
                 "format_schedule_summary", "format_events_for_context",
                 "get_unread_messages", "format_unread_summary"):
        assert name not in server_text, f"{name} is bound in server.py again"

    # `open_terminal` is a live capability again — `open_in_terminal` is how
    # JARVIS puts the user in a project's directory — but it is reached
    # through the `actions` namespace, never bound as a bare module-level
    # name the way the deleted lookups were.
    import server as server_module
    assert not hasattr(server_module, "open_terminal")
    assert "from actions import" not in server_text \
        or "open_terminal" not in server_text.split("from actions import")[1].split("\n")[0]


def test_no_action_tag_is_emitted_or_parsed_any_more():
    """The [ACTION:X] protocol is gone end to end. Its emitter
    (JARVIS_SYSTEM_PROMPT/generate_response) went in the previous batch, its
    parser and router go in this one, and nothing left in the server writes a
    tag or looks for one."""
    text = (ROOT / "server.py").read_text(encoding="utf-8")
    for tag in ("[ACTION:BUILD]", "[ACTION:BROWSE]", "[ACTION:OPEN_TERMINAL]",
                "[ACTION:SCREEN]", "[ACTION:RESEARCH]", "[ACTION:PROMPT_PROJECT]"):
        assert tag not in text, f"{tag} has no parser left to reach"


def test_the_context_refresh_thread_is_gone():
    """It wrote `_ctx_cache` that nothing ever read, and paid for it by running
    an AppleScript every 30s that enumerated every window of every app, opened
    Calendar.app on the user's screen, and tripped a Notes error. It belonged
    to the retired system-prompt path; the brain gets its context from tools.

    The user noticed Calendar launching and asked why. That is the bug.
    """
    import server
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    assert "_ctx_cache" not in source
    assert "_refresh_context_sync" not in source
    assert "Context refresh thread" not in source


def test_restart_does_not_hardcode_host_and_port():
    """It used to re-exec with --host 0.0.0.0 --port 8340 regardless of how the
    server was actually started, so a restart moved the UI to a different
    origin — and Chrome scopes microphone permission per origin INCLUDING the
    port, so the user silently lost their mic."""
    import server
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    import inspect
    body = inspect.getsource(server.api_restart) + inspect.getsource(server._restart_arguments)
    assert "os.execv(sys.executable, _restart_arguments())" in body
    assert "sys.argv" in body, "restart must re-exec with the real arguments"
    assert '"0.0.0.0"' not in body and '"8340"' not in body
