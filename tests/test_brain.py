import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import pytest

import procs

sys.path.insert(0, str(Path(__file__).parent.parent))

FAKE = Path(__file__).parent / "fixtures" / "fake_brain.py"


# The handover is spliced in as an untrusted BLOCK — it is model output, not
# JARVIS's own prose (see tests/test_handover_taint.py). These two are how the
# tests below find it; they used to look for "Where you left off" and
# "conversation):\n", which were the prose of the trusted form.
HANDOVER_OPEN = '<session-output name="handover" untrusted="true">\n'
HANDOVER_CLOSE = "\n</session-output>"


def _carried(prompt: str) -> str:
    """Exactly the handover slice: what is inside the block, and nothing else."""
    if HANDOVER_OPEN not in prompt:
        return ""
    return prompt.split(HANDOVER_OPEN, 1)[1].split(HANDOVER_CLOSE, 1)[0]


def _config(tmp_path, **kw):
    import brain
    return brain.BrainConfig(home=tmp_path / "jarvis",
                             claude_path=f"{sys.executable} {FAKE}",
                             turn_timeout=kw.pop("turn_timeout", 5.0),
                             warmup_timeout=kw.pop("warmup_timeout", 10.0),
                             **kw)


def test_command_has_exact_flags(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path, model="opus"))
    cmd = b.command()
    joined = " ".join(cmd)
    assert cmd[2:] [:6] == ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    assert "--include-partial-messages" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--name") + 1] == "jarvis"
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert '"crossSessionInbound": "accept"' in cmd[cmd.index("--settings") + 1].replace('":"', '": "')
    assert cmd[cmd.index("--tools") + 1] == (                     # an allowlist, not a denylist
        "mcp__jarvis__business_status,mcp__jarvis__business_action,mcp__jarvis__business_find,"
        "mcp__jarvis__business_propose,mcp__jarvis__business_record,mcp__jarvis__business_report,"
        "mcp__jarvis__list_sessions,mcp__jarvis__session_detail,"
        "mcp__jarvis__steer_session,mcp__jarvis__answer_dialog,"
        "mcp__jarvis__spawn_run,"
        "mcp__jarvis__start_build,mcp__jarvis__build_status,"
        "mcp__jarvis__review_document,mcp__jarvis__approve_document,"
        "mcp__jarvis__run_command,"
        "mcp__jarvis__create_project,mcp__jarvis__run_status,"
        "mcp__jarvis__cancel_run,"
        "mcp__jarvis__list_projects,"
        "mcp__jarvis__open_in_browser,mcp__jarvis__open_in_terminal,"
        "mcp__jarvis__read_page,mcp__jarvis__look_at_page,"
        "mcp__jarvis__what_is_on_screen,mcp__jarvis__look_at_screen,"
        "mcp__jarvis__github_repo,"
        "mcp__jarvis__usage_status,mcp__jarvis__connections,"
        "mcp__jarvis__enable_session_inbox,"
        "mcp__jarvis__repo_overview,mcp__jarvis__search_repo,"
        "mcp__jarvis__read_file,mcp__jarvis__open_in_editor,"
        "mcp__jarvis__remember,mcp__jarvis__recall,"
        "mcp__jarvis__project_note,mcp__jarvis__project_history,mcp__jarvis__write_journal,"
        "mcp__jarvis__message_user,"
        # The CLI's own two, and the only built-ins here: without them JARVIS
        # can read a page he was given the address of and find nothing.
        "WebSearch,WebFetch")
    assert cmd[cmd.index("--tools") + 1] == ",".join(brain.ALLOWED_TOOLS), (
        "the pin above IS ALLOWED_TOOLS — a name that appears in one and not "
        "the other means the list this test guards is no longer the list the "
        "brain is launched with")
    assert "--disallowedTools" not in cmd
    assert "--append-system-prompt" in joined
    assert "--mcp-config" not in cmd


def test_model_defaults_to_sonnet_and_env_overrides(tmp_path, monkeypatch):
    import brain
    assert brain.BrainConfig.from_env(tmp_path).model == "sonnet"
    monkeypatch.setenv("JARVIS_BRAIN_MODEL", "haiku")
    assert brain.BrainConfig.from_env(tmp_path).model == "haiku"


def test_child_env_scrubs_claude_code_vars(monkeypatch):
    import brain
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/keep/me")
    env = brain.Brain.child_env()
    assert "CLAUDE_CODE_SESSION_ID" not in env and "CLAUDECODE" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/keep/me"


# Read off a live Claude Code session on 2026-09-26: what JARVIS's backend
# carried when an agent started it, and passed to the brain's `claude -p`.
LAUNCHING_SESSION = {
    "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "the-agent's-session",
    "CLAUDE_PID": "4242", "CLAUDE_EFFORT": "xhigh",
    "AI_AGENT": "claude-code_2-1-280_agent", "CLAUDE_AGENT_SDK_VERSION": "0.3.280",
    "CLAUDE_PREVIEW_CLASSIFIER_FLOOR": "1", "MCP_CONNECTION_NONBLOCKING": "true",
    "MCP_SERVER_CONNECTION_BATCH_SIZE": "8", "API_TIMEOUT_MS": "900000",
    "DISABLE_MICROCOMPACT": "1", "USE_STAGING_OAUTH": "",
    "TRACEPARENT": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "OTEL_EXPORTER_OTLP_HEADERS": "authorization=Bearer collector-secret",
}


@pytest.mark.asyncio
async def test_the_brain_process_never_inherits_the_session_that_started_jarvis(tmp_path, monkeypatch):
    """Through the real spawn, not just the helper: the environment the
    brain's process actually received. `--effort low` is the brain's; the
    launching session's effort, MCP start-up tuning, API timeout, agent
    identity and telemetry are not."""
    import json
    import brain
    dump = tmp_path / "brain_env.json"
    monkeypatch.setenv("FAKE_BRAIN_ENV_DUMP", str(dump))
    for name, value in LAUNCHING_SESSION.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "login"))
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(tmp_path / "bash.exe"))
    b = brain.Brain(_config(tmp_path))
    try:
        assert await b.start() is True
    finally:
        await b.stop()
    child = json.loads(dump.read_text(encoding="utf-8"))
    assert sorted(set(child) & set(LAUNCHING_SESSION)) == []
    assert child["CLAUDE_CONFIG_DIR"] == str(tmp_path / "login")
    assert child["CLAUDE_CODE_GIT_BASH_PATH"] == str(tmp_path / "bash.exe")
    assert child["PATH"] == os.environ["PATH"]


@pytest.mark.asyncio
async def test_start_warms_up_and_becomes_ready(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        assert await b.start() is True
        assert b.ready and b.session_id == "fake-session-0001" and b.model_in_use == "claude-sonnet-5-fake"
        assert b.context_tokens == 10 + 9000 + 1000   # all three columns; see test_turn_streams_deltas_and_accounts
    finally:
        await b.stop()
    assert not b.running


@pytest.mark.asyncio
async def test_turn_streams_deltas_and_accounts(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        deltas = []
        r = await b.turn("hello there", on_delta=deltas.append)
        assert r.stop_reason == "result"
        assert "".join(deltas) == "Echo: hello there" == r.text
        assert r.first_delta_sec is not None and r.first_delta_sec < 2
        # The fake reports input=10, cache_read=18000, cache_creation=1000 on
        # this turn's one call. The window is that call's whole prompt: the
        # three columns are disjoint, and the 1000 written to the cache this
        # turn is prompt the model read like any other. (Leaving it out is
        # what measured a cold-cache floor as 2 tokens live; see
        # tests/test_rotation_loop.py.)
        assert r.context_tokens == 10 + 18000 + 1000 and b.context_tokens == r.context_tokens
        assert r.origin == "user"
    finally:
        # A failing assert above must still stop the brain: its child holds
        # pytest's captured stdout, and an unstopped one hangs the whole
        # suite instead of failing this test.
        await b.stop()


@pytest.mark.asyncio
async def test_current_origin_is_set_during_turn(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        seen = []
        r = await b.turn("SLOW:0.3 hi", origin="watcher", on_delta=lambda d: seen.append(b.current_origin))
        assert seen and set(seen) == {"watcher"}
        assert b.current_origin is None
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_turn_can_begin_holding_somebody_elses_words(tmp_path):
    """A message the owner FORWARDED from his phone is somebody else's text
    from its first token, so the turn is tainted before its first tool call
    — not marked afterwards by a reading tool, which is too late for the
    acting-tool gate that call has to pass. The generation keeps the mark,
    as it does after any read; the next turn is the user's own again."""
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        at_tool, at_delta = [], []
        r = await b.turn("TOOL please", untrusted="a message forwarded to you",
                         on_tool=lambda: at_tool.append(b.turn_untrusted_source),
                         on_delta=lambda d: at_delta.append(b.turn_untrusted_source))
        assert r.stop_reason == "result"
        assert at_tool == ["a message forwarded to you"], "tainted before the first tool"
        assert set(at_delta) == {"a message forwarded to you"}
        assert b.turn_untrusted_source is None, "nothing between turns"
        assert b.generation_untrusted_source == "a message forwarded to you"
        seen = []
        await b.turn("hello again", on_delta=lambda d: seen.append(b.turn_untrusted_source))
        assert seen and set(seen) == {None}
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_tool_use_is_recorded(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        r = await b.turn("TOOL please")
        assert r.tools == ["ListAgents"]
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_turns_are_serialized(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        r1, r2 = await asyncio.gather(b.turn("SLOW:0.2 one"), b.turn("two"))
        assert r1.text == "Echo: SLOW:0.2 one" and r2.text == "Echo: two"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_stuck_turn_times_out_and_restarts(tmp_path):
    import brain
    states = []
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5))
    try:
        b.on_state(lambda s, info: states.append(s))
        await b.start()
        gen = b.generation
        r = await b.turn("SLOW:3 never")
        assert r.stop_reason == "timeout"
        for _ in range(50):
            if b.ready and b.generation > gen:
                break
            await asyncio.sleep(0.1)
        assert b.generation == gen + 1 and b.ready
        assert "restarting" in states and states[-1] == "ready"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_crash_mid_turn_restarts_with_backoff(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        gen = b.generation
        r = await b.turn("DIE now")
        assert r.stop_reason == "died"
        for _ in range(60):
            if b.ready and b.generation > gen:
                break
            await asyncio.sleep(0.1)
        assert b.ready and b.generation == gen + 1
        r2 = await b.turn("alive?")
        assert r2.text == "Echo: alive?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_too_many_crashes_marks_failed(tmp_path):
    import brain
    states = []
    cfg = _config(tmp_path, max_restarts=1, restart_window=300.0)
    b = brain.Brain(cfg)
    try:
        b.on_state(lambda s, info: states.append(s))
        await b.start()
        await b.turn("DIE")            # restart 1 (allowed)
        for _ in range(60):
            if b.ready:
                break
            await asyncio.sleep(0.1)
        await b.turn("DIE")            # restart 2 (exceeds max_restarts=1)
        for _ in range(60):
            if b.failed:
                break
            await asyncio.sleep(0.1)
        assert b.failed and "failed" in states
        r = await b.turn("anyone?")
        assert r.stop_reason == "not_running"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_rate_limit_is_reported_and_blocks_turns(tmp_path):
    import brain
    states = []
    b = brain.Brain(_config(tmp_path))
    try:
        b.on_state(lambda s, info: states.append((s, info)))
        await b.start()
        r = await b.turn("RATELIMIT")
        assert r.stop_reason == "result"             # the turn that carried the event still completes
        assert b.rate_limit and b.rate_limit["status"] == "rejected"
        assert any(s == "rate_limited" and info.get("resets_at") for s, info in states)
        r2 = await b.turn("again")
        assert r2.stop_reason == "rate_limited" and r2.rate_limit["resetsAt"] > time.time()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_missing_binary_fails_cleanly(tmp_path):
    import brain
    cfg = _config(tmp_path)
    cfg.claude_path = "/definitely/not/here/claude"
    b = brain.Brain(cfg)
    assert await b.start() is False
    assert b.failed
    r = await b.turn("hi")
    assert r.stop_reason == "not_running"


# ── recovery paths the first review found untested ──────────────────────

async def _wait_until(pred, seconds=8.0):
    for _ in range(int(seconds / 0.1)):
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.mark.asyncio
async def test_restart_keeps_trying_when_the_replacement_dies_during_warmup(tmp_path, monkeypatch):
    import brain
    marker = tmp_path / "die-once"
    monkeypatch.setenv("FAKE_BRAIN_DIE_ONCE", str(marker))
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=3))
    try:
        b.on_state(lambda s, info: states.append(s))
        await b.start()
        gen = b.generation
        marker.write_text("x", encoding="utf-8")                     # the NEXT spawn exits 1 at startup
        r = await b.turn("DIE")
        assert r.stop_reason == "died"
        assert await _wait_until(lambda: b.ready and b.generation == gen + 2)
        assert not b.failed and not marker.exists()
        assert states.count("restarting") == 2 and states[-1] == "ready"
        assert (await b.turn("alive?")).text == "Echo: alive?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_turn_during_the_restart_gap_does_not_bind_to_the_dying_process(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path, turn_timeout=0.5))
    try:
        await b.start()
        gen = b.generation
        assert (await b.turn("SLOW:3 stuck")).stop_reason == "timeout"
        t0 = time.monotonic()
        r = await b.turn("anyone there?")
        assert r.stop_reason == "not_running" and time.monotonic() - t0 < 0.3
        assert await _wait_until(lambda: b.ready and b.generation == gen + 1)
        assert (await b.turn("now?")).text == "Echo: now?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_rate_limit_without_resets_at_expires_after_the_default(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path, rate_limit_default_sec=0.3))
    try:
        await b.start()
        assert (await b.turn("RATELIMIT_NORESET")).stop_reason == "result"
        r = await b.turn("blocked?")
        assert r.stop_reason == "rate_limited" and r.rate_limit["resetsAt"] > time.time()
        await asyncio.sleep(0.4)
        assert (await b.turn("free?")).text == "Echo: free?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_stop_cancels_a_pending_restart_and_leaves_nothing_running(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    await b.turn("DIE")                        # a restart is now pending (0.5 s backoff)
    await b.stop()
    await asyncio.sleep(0.8)
    assert not b.running and not b.ready and not b.failed
    assert b._restart_task is not None and b._restart_task.done()
    assert (await b.turn("hello?")).stop_reason == "not_running"


@pytest.mark.asyncio
async def test_rate_limit_spanning_a_restart_neither_fails_the_brain_nor_leaks(tmp_path):
    """The warm-up turn must bypass the rate-limit gate, or a transient limit that
    spans a crash burns the whole restart budget."""
    import brain
    b = brain.Brain(_config(tmp_path, max_restarts=2))
    try:
        await b.start()
        gen = b.generation
        assert (await b.turn("RATELIMIT")).stop_reason == "result"
        assert (await b.turn("still limited?")).stop_reason == "rate_limited"
        b._kill(b._proc)                                   # a crash while rate-limited
        assert await _wait_until(lambda: b.ready and b.generation == gen + 1)
        assert not b.failed and len(b._restart_times) == 1
        assert (await b.turn("after restart")).stop_reason == "rate_limited"   # limited, but alive
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_start_after_failed_is_a_fresh_boot(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path, max_restarts=0))
    try:
        await b.start()
        b._kill(b._proc)                                   # any crash exceeds max_restarts=0
        assert await _wait_until(lambda: b.failed)
        assert await b.start() is True
        assert b.ready and not b.failed
        assert (await b.turn("back?")).text == "Echo: back?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_launch_prompt_names_the_generation_being_started(tmp_path, monkeypatch):
    """The prompt is built before the spawn, so the generation bump must come first."""
    import brain
    seen = []
    real = asyncio.create_subprocess_exec

    async def capture(*argv, **kw):
        seen.append(list(argv))
        return await real(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        prompt = seen[0][seen[0].index("--append-system-prompt") + 1]
        assert "brain generation 1" in prompt and b.generation == 1
    finally:
        await b.stop()


def _alive(pid: int) -> bool:
    # Not `os.kill(pid, 0)`: on Windows that is a Ctrl+C, not a probe.
    return procs.pid_alive(pid)


@pytest.mark.asyncio
async def test_start_on_a_running_brain_replaces_it_without_orphans(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        first = b._proc.pid
        assert await b.start() is True
        second = b._proc.pid
        assert second != first and b.ready
        assert await _wait_until(lambda: not _alive(first), 3.0)
    finally:
        await b.stop()
    assert await _wait_until(lambda: not _alive(second), 3.0)


@pytest.mark.asyncio
async def test_start_during_a_pending_restart_yields_exactly_one_process(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        b._kill(b._proc)                                   # restart pending (0.5 s backoff)
        await asyncio.sleep(0.1)
        assert await b.start() is True                     # operator retry during the gap
        pid = b._proc.pid
        gen = b.generation
        await asyncio.sleep(0.9)                           # past where the old backoff would fire
        assert b._proc.pid == pid and b.generation == gen and b.ready
        assert b._restart_task is None or b._restart_task.done()
    finally:
        await b.stop()
    assert await _wait_until(lambda: not _alive(pid), 3.0)


@pytest.mark.asyncio
async def test_warmup_timeout_kills_the_hung_child(tmp_path, monkeypatch):
    import brain
    marker = tmp_path / "hang-once"
    monkeypatch.setenv("FAKE_BRAIN_HANG_ONCE", str(marker))
    b = brain.Brain(_config(tmp_path, warmup_timeout=1.0, max_restarts=3))
    await b.start()
    marker.write_text("x", encoding="utf-8")                             # the replacement will hang in warm-up
    hung = []
    orig = b._kill

    def spy(proc):
        hung.append(proc.pid)
        orig(proc)

    b._kill = spy
    b._proc.kill()                                     # crash the current one
    try:
        assert await _wait_until(lambda: b.ready and not marker.exists() and len(hung) >= 1, 8.0)
        for pid in hung:
            assert await _wait_until(lambda: not _alive(pid), 3.0), f"hung child {pid} still alive"
        assert not b.failed
    finally:
        await b.stop()          # a live child left behind by a failing test hangs pytest's loop teardown


# ── subscription-only auth and error results (found by the first live run) ──

def test_child_env_never_carries_api_credentials(monkeypatch):
    """The server's .env puts ANTHROPIC_API_KEY in the environment for other
    features; the brain must never inherit it or the CLI bills the key (and,
    with an identity-linked key, fails with '400 anthropic-workspace-id')."""
    import brain
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-identity-linked")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_x")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")   # would move the brain off the login
    monkeypatch.setenv("ANTHROPIC_MODEL", "something")
    monkeypatch.setenv("FISH_API_KEY", "keep")
    env = brain.Brain.child_env()
    assert not any(k.startswith("ANTHROPIC_") for k in env)
    assert env["FISH_API_KEY"] == "keep"


@pytest.mark.asyncio
async def test_an_error_result_is_an_error_not_an_empty_success(tmp_path):
    import brain
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        r = await b.turn("APIERROR please")
        assert r.stop_reason == "error" and r.text == ""
        assert "anthropic-workspace-id" in (r.error or "")
        assert b.ready                                  # one failed turn does not restart the brain
        assert (await b.turn("still here?")).text == "Echo: still here?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_warmup_that_errors_never_reports_ready(tmp_path, monkeypatch):
    """Every spawn errors (e.g. credentials that never work): the brain must end
    up failed with the CLI's message in the reason — never 'ready' with ctx=0."""
    import brain
    monkeypatch.setenv("FAKE_BRAIN_FORCE", "APIERROR")
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=1))
    b.on_state(lambda s, info: states.append((s, info)))
    try:
        assert await b.start() is False
        assert not b.ready
        assert await _wait_until(lambda: b.failed, 8.0)
        assert not b.running
        assert any(s == "failed" for s, _ in states)
    finally:
        await b.stop()


def test_spec_defaults_are_the_spec_defaults(tmp_path):
    """The suite overrides every timing knob; pin the shipped values here."""
    import brain
    cfg = brain.BrainConfig(home=tmp_path)
    assert (cfg.model, cfg.effort) == ("sonnet", "low")
    assert cfg.turn_timeout == 90.0 and cfg.warmup_timeout == 45.0
    assert cfg.max_restarts == 3 and cfg.restart_window == 300.0
    assert cfg.rate_limit_default_sec == 300.0


# ── fatal (auth) warm-up failures do not burn the restart budget ────────
# The live incident: an expired OAuth login made every warm-up fail with
# "Failed to authenticate: OAuth session expired and could not be
# refreshed", and the brain spent its whole restart budget retrying a
# condition retrying can never fix -- 3 restarts in 5 seconds, then mute.

@pytest.mark.asyncio
async def test_auth_warmup_failure_is_fatal_after_one_attempt(tmp_path, monkeypatch):
    import brain
    monkeypatch.setenv("FAKE_BRAIN_FORCE", "AUTHERROR")
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=3))
    try:
        b.on_state(lambda s, info: states.append((s, info)))
        assert await b.start() is False
        assert b.failed and b.failure_reason == "auth"
        assert b.generation == 1                        # exactly one spawn attempt, not four
        assert not any(s == "restarting" for s, _ in states)  # the restart budget was never touched
        assert any(s == "failed" and info.get("failure_reason") == "auth" for s, info in states)
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_ordinary_warmup_failure_still_retries(tmp_path, monkeypatch):
    """Contrast case: a non-auth warm-up failure is still assumed transient
    and gets the full restart budget, exactly as before this change."""
    import brain
    monkeypatch.setenv("FAKE_BRAIN_FORCE", "APIERROR")
    states = []
    b = brain.Brain(_config(tmp_path, max_restarts=2))
    try:
        b.on_state(lambda s, info: states.append((s, info)))
        assert await b.start() is False
        assert await _wait_until(lambda: b.failed, 8.0)
        assert b.failure_reason is None                 # not classified as fatal
        assert sum(1 for s, _ in states if s == "restarting") == 2   # the full budget was used
        assert b.generation == 3                         # 1 initial attempt + 2 restarts
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_auth_fatal_failure_kills_the_process_and_leaks_nothing(tmp_path, monkeypatch):
    import brain
    monkeypatch.setenv("FAKE_BRAIN_FORCE", "AUTHERROR")
    b = brain.Brain(_config(tmp_path, max_restarts=3))
    try:
        assert await b.start() is False
        assert b.failed and b.failure_reason == "auth"
        pid = b._proc.pid if b._proc else None
        assert await _wait_until(lambda: not b.running, 3.0)   # kill() is async; give it a moment to be reaped
        if pid is not None:
            assert await _wait_until(lambda: not _alive(pid), 3.0), f"child {pid} still alive"
    finally:
        await b.stop()   # must not hang or raise against an already-dead process


@pytest.mark.asyncio
async def test_a_usage_warning_is_not_a_rate_limit(tmp_path):
    """The CLI sends `allowed_warning` when you pass a utilisation threshold —
    you are still allowed. Treating it as a limit muted JARVIS completely."""
    import brain
    states = []
    b = brain.Brain(_config(tmp_path))
    try:
        b.on_state(lambda s, info: states.append(s))
        await b.start()
        r = await b.turn("RATELIMIT_WARNING please")
        assert r.stop_reason == "result" and r.text == "Echo: RATELIMIT_WARNING please"
        assert b.rate_limit is None                       # not blocked
        assert "rate_limited" not in states
        assert b.usage["status"] == "allowed_warning" and b.usage["utilization"] == 0.76
        assert (await b.turn("still working?")).text == "Echo: still working?"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_an_unrecognised_status_fails_open(tmp_path):
    """An unknown status must not mute JARVIS: we try, and a real limit comes
    back as an error result we can speak."""
    import brain
    assert "allowed_warning" not in brain.BLOCKING_RATE_LIMIT_STATUSES
    b = brain.Brain(_config(tmp_path))
    try:
        await b.start()
        b._handle({"type": "rate_limit_event",
                   "rate_limit_info": {"status": "some_future_status"}}, b._proc)
        assert b.rate_limit is None
        assert (await b.turn("hello")).stop_reason == "result"
    finally:
        await b.stop()


# ---------------------------------------------------------------------------
# The cold-start handover: a RESTART, not only an in-process rotation, has to
# pick up where the last generation left off. Before this, `_handover` was set
# only by rotate(), so restarting the server — the normal case — handed the new
# brain a blank slate and the journal it had just written was read by nobody.
# ---------------------------------------------------------------------------

@pytest.fixture
def journal(monkeypatch, tmp_path):
    """An isolated brain home, so no test can read or write the real journal."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import data_paths
    import jarvis_memory
    data_paths.ensure_memory_layout()
    return jarvis_memory


def test_a_cold_start_carries_the_last_real_handover(tmp_path, journal):
    import brain
    journal.write_journal("We shipped the runs dashboard; chitauri is next.",
                          reason="shutdown")

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()

    assert "chitauri is next" in prompt
    assert "chitauri is next" in _carried(prompt), prompt


def test_the_handover_tells_the_brain_not_to_resume_the_old_topic(tmp_path, journal):
    """The handover is continuity of knowledge, not a script to open with. A
    fresh conversation must greet and let the user set the topic, not resume
    a question the previous conversation left unfinished."""
    import brain
    journal.write_journal("We shipped the runs dashboard; chitauri is next.",
                          reason="shutdown")

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()

    assert "do not raise it yourself" in prompt
    assert "let the user set today's topic" in prompt
    # The instruction must sit BEFORE the handover block, not inside it:
    # test_the_handover_is_bounded_to_1200_characters slices the block's
    # contents as the handover and pins its length.
    assert prompt.index("let the user set today's topic") < prompt.index(HANDOVER_OPEN)


def test_a_cold_start_prefers_the_newest_real_handover(tmp_path, journal):
    import brain
    journal.write_journal("the older note", reason="shutdown")
    journal.write_journal("the newer note", reason="rotation")

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()

    assert "the newer note" in prompt
    assert "the older note" not in prompt


def test_a_placeholder_never_displaces_a_real_handover(tmp_path, journal):
    """Shutdown ALWAYS writes an entry, including a tombstone when the brain
    said nothing. Carrying that forward would mean every session after one
    silent shutdown began knowing nothing."""
    import brain
    journal.write_journal("the real handover about chitauri", reason="shutdown")
    journal.write_journal("Session ended; the brain wrote no handover.",
                          reason="shutdown-silent")

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()

    assert "the real handover about chitauri" in prompt
    assert "wrote no handover" not in prompt


def test_a_journal_of_nothing_but_placeholders_carries_nothing(tmp_path, journal):
    import brain
    journal.write_journal("Session ended; the brain wrote no handover.",
                          reason="shutdown-silent")
    journal.write_journal("No handover was written — the outgoing brain did not answer.",
                          reason="rotation-silent")

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()

    assert HANDOVER_OPEN not in prompt
    assert "wrote no handover" not in prompt


def test_an_empty_journal_is_fine(tmp_path, journal):
    import brain
    prompt = brain.Brain(_config(tmp_path)).launch_prompt()
    assert HANDOVER_OPEN not in prompt
    assert "brain generation" in prompt


def test_a_missing_journal_folder_is_fine(tmp_path, monkeypatch):
    """Nothing has ever been written: the folder itself does not exist."""
    import brain
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "never-created"))
    assert HANDOVER_OPEN not in brain.Brain(_config(tmp_path)).launch_prompt()


def test_the_handover_is_bounded_to_1200_characters(tmp_path, journal):
    import brain
    assert brain.HANDOVER_MAX_CHARS == 1200
    journal.write_journal("x" * 6000, reason="shutdown")

    prompt = brain.Brain(_config(tmp_path)).launch_prompt()

    carried = _carried(prompt)
    assert len(carried) <= brain.HANDOVER_MAX_CHARS
    assert "xxxx" in carried


def test_an_in_process_handover_beats_the_journal(tmp_path, journal):
    """rotate() hands over what the OUTGOING brain just said; the journal on
    disk is the cold-start fallback and may be days old."""
    import brain
    journal.write_journal("yesterday's note", reason="shutdown")
    b = brain.Brain(_config(tmp_path))
    b._handover = "what I was doing thirty seconds ago"

    prompt = b.launch_prompt()

    assert "thirty seconds ago" in prompt
    assert "yesterday's note" not in prompt


def test_the_launch_prompt_names_the_active_projects(tmp_path, journal):
    import brain
    b = brain.Brain(_config(tmp_path))
    b.active_projects = lambda: ["chitauri", "jarvis"]

    prompt = b.launch_prompt()

    assert "chitauri" in prompt and "jarvis" in prompt


def test_no_projects_line_when_the_watcher_has_not_polled(tmp_path, journal):
    import brain
    assert "live Claude Code sessions" not in brain.Brain(_config(tmp_path)).launch_prompt()


def test_a_broken_project_provider_cannot_stop_a_spawn(tmp_path, journal):
    import brain
    b = brain.Brain(_config(tmp_path))

    def boom():
        raise RuntimeError("the watcher exploded")

    b.active_projects = boom
    assert "brain generation" in b.launch_prompt()


def test_an_unreadable_journal_cannot_stop_a_spawn(tmp_path, journal, monkeypatch):
    import brain
    monkeypatch.setattr(journal, "latest_journal",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    assert "brain generation" in brain.Brain(_config(tmp_path)).launch_prompt()


# ── oversized stream-json lines (asyncio's 64 KiB StreamReader default) ────
#
# Same bug as run_executor.py: `claude -p --output-format stream-json`
# emits one JSON object per line, and asyncio's default 64 KiB StreamReader
# line limit is far smaller than a legitimate line can be. Uncaught, this
# killed the brain's stdout reader task outright (or silently stopped the
# stderr drain, which then deadlocks the child once its pipe fills) — "my
# language systems are down."

async def _bounded(coro, timeout, b, what):
    """Run ONE brain coroutine under a hard wall-clock bound.

    The bug under test can make the child process block forever inside a
    blocking write() to a full, unread pipe — which the brain's own timeouts
    do not reliably resolve when the *reader* is what died. Rather than let a
    broken fix hang the whole suite, this turns a hanging call into a clean,
    named test failure and kills the child that was hanging it.

    That is the whole of it, and the docstring used to claim more. It bounds
    the coroutine it is handed; it does NOT own the brain's lifetime. An
    `assert` that fails BETWEEN two calls raises straight past this helper,
    `b.stop()` is never reached, and the still-running child — which holds
    pytest's captured stdout — hangs the entire suite instead of failing this
    test. Driven, with an assert failure injected: each of the four tests
    below hung until killed. So the bound here is necessary and not
    sufficient, and every caller wraps its body in
    `try: ... finally: await _bounded(b.stop(), ...)` as well.
    """
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        proc = b._proc
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        pytest.fail(f"{what} never returned within {timeout}s — an oversized "
                   "line must not hang the brain")


@pytest.mark.asyncio
async def test_large_single_line_parsed_correctly(tmp_path):
    """A real subprocess emits one stream-json line far bigger than the old
    64 KiB default (but under the raised limit); the brain must still parse
    it and complete the turn with its content intact."""
    import brain
    b = brain.Brain(_config(tmp_path))
    await _bounded(b.start(), 20, b, "brain.start()")
    try:
        r = await _bounded(b.turn("BIGLINE:200000"), 20, b, "brain.turn(BIGLINE)")
        assert r.stop_reason == "result"
        assert r.text == "H" * 200000
    finally:
        # `_bounded` guards a hanging coroutine, not a failing assert: without
        # this the child outlives the test, keeps pytest's captured stdout,
        # and the suite hangs instead of reporting the failure.
        await _bounded(b.stop(), 20, b, "brain.stop()")


@pytest.mark.asyncio
async def test_oversized_stdout_line_is_skipped_and_brain_survives(tmp_path, monkeypatch, caplog):
    """A line bigger than even the raised limit must be skipped — not left
    to kill the reader task — and the next real line must still parse."""
    import brain
    monkeypatch.setattr(brain.claude_env, "STREAM_LINE_LIMIT", 64 * 1024)
    b = brain.Brain(_config(tmp_path))
    await _bounded(b.start(), 20, b, "brain.start()")
    try:
        with caplog.at_level(logging.WARNING, logger="jarvis.brain"):
            r = await _bounded(b.turn("SKIPSTDOUT:300000"), 20, b,
                               "brain.turn(SKIPSTDOUT)")
        assert r.stop_reason == "result"
        assert r.text.startswith("Echo: SKIPSTDOUT:300000")
        assert any("oversized" in rec.message.lower() for rec in caplog.records)
        # The reader task must still be alive to serve the NEXT turn — proof
        # the oversized line did not kill it.
        r2 = await _bounded(b.turn("hi again"), 20, b, "brain.turn(follow-up)")
        assert r2.stop_reason == "result" and r2.text == "Echo: hi again"
    finally:
        # `_bounded` guards a hanging coroutine, not a failing assert: without
        # this the child outlives the test, keeps pytest's captured stdout,
        # and the suite hangs instead of reporting the failure.
        await _bounded(b.stop(), 20, b, "brain.stop()")


@pytest.mark.asyncio
async def test_oversized_stderr_line_is_skipped_and_brain_survives(tmp_path, monkeypatch, caplog):
    """Same treatment for stderr: an oversized stderr line must not stop the
    drain loop outright (which would eventually deadlock the child once its
    stderr pipe fills) — draining must continue past it."""
    import brain
    monkeypatch.setattr(brain.claude_env, "STREAM_LINE_LIMIT", 64 * 1024)
    b = brain.Brain(_config(tmp_path))
    await _bounded(b.start(), 20, b, "brain.start()")
    try:
        with caplog.at_level(logging.DEBUG, logger="jarvis.brain"):
            r = await _bounded(b.turn("SKIPSTDERR:300000"), 20, b,
                               "brain.turn(SKIPSTDERR)")
            for _ in range(50):
                if any("STDERR-MARKER-AFTER-SKIP" in rec.message for rec in caplog.records):
                    break
                await asyncio.sleep(0.05)
        assert r.stop_reason == "result"
        assert any("oversized" in rec.message.lower() for rec in caplog.records)
        assert any("STDERR-MARKER-AFTER-SKIP" in rec.message for rec in caplog.records)
    finally:
        # `_bounded` guards a hanging coroutine, not a failing assert: without
        # this the child outlives the test, keeps pytest's captured stdout,
        # and the suite hangs instead of reporting the failure.
        await _bounded(b.stop(), 20, b, "brain.stop()")


# ---------------------------------------------------------------------------
# Usage capture. The rate-limit event is the ONLY place JARVIS ever learns how
# much of the subscription's windows is gone; it used to be read for a backoff
# and dropped on the floor. It is now persisted before anything is announced.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_usage_warning_is_persisted_for_the_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import brain
    import usage_store
    importlib.reload(usage_store)

    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        assert usage_store.snapshot()["measured"] is False   # nothing observed yet
        await b.turn("RATELIMIT_WARNING please")
    finally:
        # Always: a brain left running holds pytest's captured stdout open and
        # a failing assertion turns into a hung suite instead of a red line.
        await b.stop()

    snap = usage_store.snapshot()
    assert snap["measured"] is True and snap["status"] == "allowed_warning"
    by_key = {w["key"]: w for w in snap["windows"]}
    assert by_key["five_hour"]["utilization"] == 62.0
    assert by_key["seven_day"]["utilization"] == 76.0
    assert snap["stale"] is False


@pytest.mark.asyncio
async def test_a_rejection_is_persisted_too(tmp_path, monkeypatch):
    """A blocked turn is the most important reading there is — it is the one
    the user is asking about when JARVIS goes quiet."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import brain
    import usage_store
    importlib.reload(usage_store)

    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        await b.turn("RATELIMIT")
    finally:
        await b.stop()

    five = {w["key"]: w for w in usage_store.snapshot()["windows"]}["five_hour"]
    assert five["status"] == "rejected"
    assert five["resets_at"] is not None
    assert five["utilization"] is None            # the event carried none; none is shown


@pytest.mark.asyncio
async def test_a_failing_usage_store_still_leaves_the_limit_enforced(tmp_path, monkeypatch):
    """Recording sits upstream of the rate-limit gate. If a write error escaped
    it, the event handler would abort before setting `rate_limit` and JARVIS
    would keep firing turns straight into a limit it had already been told
    about — a disk problem turned into a wall of rejected turns."""
    import brain
    import usage_store

    def boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(usage_store, "record", boom)
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        r = await b.turn("RATELIMIT")
        assert r.stop_reason == "result"               # the carrying turn completes
        assert b.rate_limit and b.rate_limit["status"] == "rejected"
        assert (await b.turn("again")).stop_reason == "rate_limited"
    finally:
        await b.stop()


# ── one invalid UTF-8 byte used to kill the reader for good ────────────────
#
# `json.loads(raw)` was called on BYTES. json decodes them first, so an
# invalid byte raises UnicodeDecodeError — a ValueError, but NOT a
# JSONDecodeError, so `except json.JSONDecodeError: continue` did not catch
# it. It escaped `while True`, ran `_on_exit`, and the reader was gone while
# the process was still alive and still writing: 90 seconds of silence, then
# a restart burned.
#
# run_executor.py has always done `raw.decode(errors="replace")` first and is
# immune. The brain now does the same.

@pytest.mark.asyncio
async def test_invalid_utf8_line_does_not_kill_the_reader(tmp_path, caplog):
    """A bad byte, then a real `result` line. The result must still be read."""
    import brain
    b = brain.Brain(_config(tmp_path))
    await _bounded(b.start(), 20, b, "brain.start()")
    try:
        r = await _bounded(b.turn("BADBYTE please"), 20, b, "brain.turn(BADBYTE)")
        assert r.stop_reason == "result", r
        assert r.text == "Echo: BADBYTE please"
        # And the reader is still there for the next turn.
        r2 = await _bounded(b.turn("hi again"), 20, b, "brain.turn(follow-up)")
        assert r2.stop_reason == "result" and r2.text == "Echo: hi again"
    finally:
        # `_bounded` guards a hanging coroutine, not a failing assert: without
        # this the child outlives the test, keeps pytest's captured stdout,
        # and the suite hangs instead of reporting the failure.
        await _bounded(b.stop(), 20, b, "brain.stop()")


def test_a_turn_reports_how_much_of_its_context_was_served_from_cache():
    """The latency line prints it beside the context size: a first delta of
    four seconds on a window that was all cache reads is the model's time,
    and one on a window that was all fresh input is a cache that is being
    rebuilt every turn. The window counts all three columns; `cached` is
    the part of it that was read."""
    from brain import _Turn
    t = _Turn("user", None)
    t.usage = {"input_tokens": 120, "cache_read_input_tokens": 26000,
               "cache_creation_input_tokens": 500, "output_tokens": 25}
    r = t.result(None)
    assert r.context_tokens == 26620
    assert r.cached_tokens == 26000
