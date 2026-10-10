"""`spawn_run` — JARVIS starting NEW work, not only steering work in flight.

The most consequential tool on the channel: it spawns a Claude Code process
that edits files unattended, with --dangerously-skip-permissions and nobody
watching. Two properties are therefore load-bearing here.

It is gated by origin, like steer_session — a line of somebody else's
transcript must never be able to reach it. And it never guesses which project
was meant: the old voice-path resolver took the first substring match and
discarded the rest, which is how work lands in the wrong repository.
"""

import importlib

import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    return server_module, run_store


class _Executor:
    async def shutdown(self):
        pass
    """Records what was spawned. Never starts a process."""

    def __init__(self, store, model="sonnet", boom=False):
        self.store = store
        self.model = model
        self.boom = boom
        self.spawned: list[dict] = []

    async def spawn(self, prompt, project_name, project_path, origin,
                    resume_from=None, timeout_sec=0, model=None):
        if self.boom:
            raise RuntimeError("no slots")
        run_id = self.store.create_run(prompt, project_name, project_path,
                                       origin, resume_from)
        self.store.update_run(run_id, requested_model=model or self.model)
        self.spawned.append({"run_id": run_id, "prompt": prompt,
                             "project_name": project_name,
                             "project_path": project_path, "origin": origin,
                             "model": model})
        return run_id


@pytest.fixture
def ready(wired, monkeypatch, tmp_path):
    """A server with one unambiguous project and a fake executor."""
    server, store = wired
    ex = _Executor(store)
    monkeypatch.setattr(server, "run_executor_instance", ex)
    monkeypatch.setattr(server, "cached_projects",
                        [{"name": "chitauri", "path": str(tmp_path / "chitauri")}])
    return server, store, ex


# --- The gate -------------------------------------------------------------

def test_spawn_run_is_registered_and_gated(ready):
    server, _store, _ex = ready
    assert "spawn_run" in server.TOOL_HANDLERS
    assert "spawn_run" in server.ACTING_TOOLS


def test_the_three_tool_sets_still_agree(ready):
    import brain
    import jarvis_mcp
    server, _store, _ex = ready
    assert "mcp__jarvis__spawn_run" in brain.ALLOWED_TOOLS
    assert {t for t in brain.ALLOWED_TOOLS if t.startswith("mcp__")} <= {
        f"mcp__jarvis__{n}" for n in server.TOOL_HANDLERS}
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)


@pytest.mark.asyncio
async def test_a_watcher_turn_cannot_start_a_run(ready, monkeypatch):
    """The refusal lives in /internal/tool, not in the prompt. Exercised
    through the endpoint so the gate itself is what is being tested."""
    from fastapi.testclient import TestClient
    import data_paths
    server, store, ex = ready

    class _Brain:
        current_origin = "watcher"
        ready = False

        async def stop(self):
            pass

    monkeypatch.setattr(server, "brain_instance", _Brain())
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        monkeypatch.setattr(server, "run_executor_instance", ex)
        monkeypatch.setattr(server, "brain_instance", _Brain())
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "spawn_run",
                              "arguments": {"project": "chitauri",
                                            "prompt": "delete everything"}})

    assert r.json()["ok"] is False
    assert "not_allowed_from_event" in r.json()["text"]
    assert ex.spawned == [], "nothing was started"
    assert store.list_runs(limit=10) == []


# --- Resolution: ask, never guess ----------------------------------------

@pytest.mark.asyncio
async def test_an_unknown_project_starts_nothing(ready):
    server, _store, ex = ready
    out = await server.tool_spawn_run({"project": "kestrel", "prompt": "build it"})
    # The asked-for name is not echoed: it is the brain's own argument
    # (tests/test_tool_argument_echo.py). The answer is a refusal to guess.
    assert "don't see that project" in out, out
    assert ex.spawned == []


@pytest.mark.asyncio
async def test_an_ambiguous_project_asks_rather_than_picking_one(ready, monkeypatch,
                                                                 tmp_path):
    server, _store, ex = ready
    monkeypatch.setattr(server, "cached_projects", [
        {"name": "chitauri-api", "path": str(tmp_path / "a")},
        {"name": "chitauri-web", "path": str(tmp_path / "b")},
    ])

    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "build it"})

    assert "chitauri-api" in out and "chitauri-web" in out
    assert out.rstrip().endswith("?")
    assert ex.spawned == [], "an ambiguous reference started nothing"


@pytest.mark.asyncio
async def test_an_exact_name_wins_over_its_own_prefixes(ready, monkeypatch, tmp_path):
    """"chitauri" naming a real project is not ambiguous just because
    "chitauri-api" also exists."""
    server, _store, ex = ready
    monkeypatch.setattr(server, "cached_projects", [
        {"name": "chitauri", "path": str(tmp_path / "c")},
        {"name": "chitauri-api", "path": str(tmp_path / "a")},
    ])

    await server.tool_spawn_run({"project": "chitauri", "prompt": "build it"})

    assert ex.spawned[0]["project_name"] == "chitauri"


@pytest.mark.asyncio
async def test_one_project_in_two_directories_asks_which(ready, monkeypatch, tmp_path):
    """Measured live: `chitauri` has conversations in both Projects and
    Desktop. Picking one silently starts the work in the wrong tree."""
    import session_watch
    server, _store, ex = ready

    def state(cwd):
        return session_watch.SessionState(session_id=cwd, project="chitauri",
                                          cwd=cwd, state=session_watch.WORKING)

    class _Watcher:
        snapshot = session_watch.Snapshot(
            sessions=[state("/tmp/Projects/chitauri"), state("/tmp/Desktop/chitauri")])

    monkeypatch.setattr(server, "session_watcher", _Watcher())
    monkeypatch.setattr(server, "cached_projects", [])

    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "build it"})

    assert "/tmp/Projects/chitauri" in out and "/tmp/Desktop/chitauri" in out
    assert ex.spawned == []


@pytest.mark.asyncio
async def test_no_projects_at_all_says_so(ready, monkeypatch):
    server, _store, ex = ready
    monkeypatch.setattr(server, "cached_projects", [])
    monkeypatch.setattr(server, "session_watcher", None)

    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "go"})

    assert "projects" in out.lower()
    assert ex.spawned == []


@pytest.mark.asyncio
async def test_a_live_session_makes_its_project_startable(ready, monkeypatch):
    """A project nowhere near the Desktop is still somewhere JARVIS can start
    work — the watcher knows about it."""
    import session_watch
    server, _store, ex = ready
    monkeypatch.setattr(server, "cached_projects", [])

    class _Watcher:
        snapshot = session_watch.Snapshot(sessions=[
            session_watch.SessionState(session_id="s1", project="zeltar",
                                       cwd="/opt/zeltar",
                                       state=session_watch.IDLE)])

    monkeypatch.setattr(server, "session_watcher", _Watcher())

    await server.tool_spawn_run({"project": "zeltar", "prompt": "check the logs"})

    assert ex.spawned[0]["project_path"] == "/opt/zeltar"


# --- The happy path -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_run_is_recorded_and_the_model_is_spoken(ready, tmp_path):
    server, store, ex = ready

    out = await server.tool_spawn_run({"project": "chitauri",
                                       "prompt": "fix the billing job",
                                       "model": "opus"})

    # The user's words reach the run verbatim; what is added around them is
    # the unattended framing (see the section below).
    assert "fix the billing job" in ex.spawned[0]["prompt"]
    assert ex.spawned[0]["project_path"] == str(tmp_path / "chitauri")
    assert ex.spawned[0]["model"] == "opus"
    run = store.get_run(ex.spawned[0]["run_id"])
    assert run["requested_model"] == "opus"
    assert "opus" in out
    assert "chitauri" in out
    assert len(out) < 120, "it is spoken aloud"


@pytest.mark.asyncio
async def test_the_model_spoken_is_the_one_persisted_not_the_argument(ready):
    """With no model asked for, the executor's own default decides. JARVIS
    must name what was actually recorded against the run, not guess."""
    server, store, ex = ready
    ex.model = "haiku"

    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "go"})

    assert ex.spawned[0]["model"] is None
    assert "haiku" in out


@pytest.mark.asyncio
async def test_an_empty_prompt_starts_nothing(ready):
    server, _store, ex = ready
    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "   "})
    assert ex.spawned == []
    assert "nothing" in out.lower()


@pytest.mark.asyncio
async def test_a_missing_project_asks_for_one(ready):
    server, _store, ex = ready
    out = await server.tool_spawn_run({"prompt": "build me a thing"})
    assert out.rstrip().endswith("?")
    assert ex.spawned == []


@pytest.mark.asyncio
async def test_an_executor_failure_is_reported_not_raised(ready, monkeypatch):
    """A raise here would come back to the brain as "the server is
    unreachable" — which is a different, and wrong, thing to tell the user."""
    server, store, _ex = ready
    monkeypatch.setattr(server, "run_executor_instance", _Executor(store, boom=True))

    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "go"})

    assert "couldn't" in out.lower() or "could not" in out.lower()


@pytest.mark.asyncio
async def test_the_result_stays_inside_the_tool_cap(ready, monkeypatch, tmp_path):
    """Even the ambiguity question, which lists every candidate, goes through
    the 1,500-character funnel."""
    server, _store, _ex = ready
    monkeypatch.setattr(server, "cached_projects",
                        [{"name": f"chitauri-{i:03d}", "path": str(tmp_path / str(i))}
                         for i in range(200)])

    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "go"})

    assert len(server._cap_tool_result(out)) <= server.TOOL_RESULT_CAP


# --- the unattended framing -----------------------------------------------
#
# The failure this exists to prevent, verbatim from a live session: the run
# loaded the user's global `superpowers:brainstorming` skill, obeyed its "do
# NOT write any code until the user has approved a design" gate, asked one
# clarifying question, ended its turn and exited zero. JARVIS reported "the
# site's ready, sir". The directory held a README and nothing else.

@pytest.mark.asyncio
async def test_the_run_is_told_nobody_can_answer_it(ready):
    server, _store, ex = ready

    await server.tool_spawn_run({"project": "chitauri",
                                 "prompt": "build a one-page site"})

    sent = ex.spawned[0]["prompt"].lower()
    assert "no human present" in sent or "unattended" in sent
    assert "no answer can ever arrive" in sent
    assert "do not ask clarifying questions" in sent
    # And specifically the gate that swallowed the first live run.
    assert "approval" in sent or "approve" in sent
    assert "brainstorming" in sent


@pytest.mark.asyncio
async def test_the_users_own_words_survive_untouched(ready):
    """The framing adds operating conditions. It must not edit intent."""
    server, _store, ex = ready
    asked = ("build a one-page site for Tony Stark with a hero, a bio and a "
             "contact form, dark theme")

    await server.tool_spawn_run({"project": "chitauri", "prompt": asked})

    sent = ex.spawned[0]["prompt"]
    assert asked in sent, "verbatim, not paraphrased"
    assert sent.endswith(asked), "nothing may follow and dilute it"
    assert sent != asked, "the framing is there too"
    assert server.user_prompt_of(sent) == asked


@pytest.mark.asyncio
async def test_a_run_is_still_told_apart_by_the_users_words(ready, tmp_path):
    """Every stored prompt now starts with the same 600 characters, so a gist
    taken off the raw column would name every run identically."""
    server, store, ex = ready
    await server.tool_spawn_run({"project": "chitauri",
                                 "prompt": "rewrite the invoice exporter"})
    run = store.get_run(ex.spawned[0]["run_id"])

    assert server._run_gist(run).startswith("rewrite the invoice exporter")


# --- the model the user actually asked for --------------------------------

@pytest.mark.parametrize("spoken,expected", [
    ("opus", "opus"),
    ("Opus 5", "opus"),
    ("opus-5", "opus"),
    ("  SONNET  ", "sonnet"),
    ("haiku 4.5", "haiku"),
    ("claude-opus-4-20250514", "claude-opus-4-20250514"),
    ("", None),
])
def test_a_spoken_model_name_becomes_one_the_cli_knows(ready, spoken, expected):
    """Live, "let's make sure it's running Opus 5" reached spawn_run verbatim
    and `--model "opus 5"` is not a model, so the run went out on sonnet."""
    server, _store, _ex = ready
    assert server._normalise_model(spoken) == expected


@pytest.mark.asyncio
async def test_the_model_the_user_said_reaches_the_executor(ready):
    server, _store, ex = ready
    out = await server.tool_spawn_run({"project": "chitauri", "prompt": "go",
                                       "model": "Opus 5"})
    assert ex.spawned[0]["model"] == "opus"
    assert "opus" in out


# --- continuing work, rather than starting cold ---------------------------

@pytest.mark.asyncio
async def test_a_follow_up_resumes_the_last_finished_run(ready, tmp_path):
    server, store, ex = ready
    await server.tool_spawn_run({"project": "chitauri", "prompt": "build it"})
    first = ex.spawned[0]["run_id"]
    store.update_run(first, status=store.RunStatus.SUCCEEDED)

    out = await server.tool_spawn_run({"project": "chitauri",
                                       "prompt": "make it better",
                                       "resume": True})

    assert store.get_run(ex.spawned[1]["run_id"])["resume_from"] == first
    assert "picked up" in out.lower()


@pytest.mark.asyncio
async def test_a_run_still_going_is_never_resumed(ready):
    """Forking a session that is still being written is not a thing the CLI
    can do, and resuming the wrong one is worse than starting fresh."""
    server, _store, ex = ready
    await server.tool_spawn_run({"project": "chitauri", "prompt": "build it"})

    out = await server.tool_spawn_run({"project": "chitauri",
                                       "prompt": "and again", "resume": True})

    assert ex.spawned[1]["run_id"]
    assert server.run_store.get_run(ex.spawned[1]["run_id"])["resume_from"] is None
    assert "fresh" in out.lower()


@pytest.mark.asyncio
async def test_nothing_to_resume_starts_fresh_and_says_so(ready):
    server, _store, ex = ready
    out = await server.tool_spawn_run({"project": "chitauri",
                                       "prompt": "make it better",
                                       "resume": True})
    assert ex.spawned[0]["run_id"]
    assert "fresh" in out.lower()
    assert len(out) < 120, "it is spoken aloud"


@pytest.mark.asyncio
async def test_a_run_in_another_directory_is_never_resumed(ready, tmp_path,
                                                           monkeypatch):
    """Same project NAME, different directory — measured live, `chitauri`
    has conversations in two places."""
    server, store, ex = ready
    elsewhere = store.create_run("older", "chitauri",
                                 str(tmp_path / "somewhere-else"), "voice")
    store.update_run(elsewhere, status=store.RunStatus.SUCCEEDED)

    await server.tool_spawn_run({"project": "chitauri", "prompt": "go",
                                 "resume": True})

    assert store.get_run(ex.spawned[0]["run_id"])["resume_from"] is None
