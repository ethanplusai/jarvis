"""Connecting the user's own MCP servers — the doorway.

JARVIS ships with no calendar, no mail and no notes. What he has instead is
one file the user writes their own servers into, and the machinery here is
everything that makes writing that file enough: the file survives an upgrade,
the servers in it reach the brain, the brain's allowlist admits their tools,
anything that did not start is named out loud rather than silently missing,
their tool schemas do not eat the conversation, and what they return is
treated as a stranger's words.
"""

import hashlib
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    return data_paths


def _template_text() -> str:
    return (Path(__file__).parent.parent / "jarvis_home" / "connections.json").read_text()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- 1. one obvious place, and it survives an upgrade ----------------------

def test_the_file_lives_beside_the_brains_own_config(monkeypatch, tmp_path):
    """Next to CLAUDE.md and mcp.json, in the brain's home — not in the repo,
    which an upgrade overwrites."""
    dp = _fresh(monkeypatch, tmp_path)
    assert dp.connections_path() == dp.brain_home() / "connections.json"
    assert dp.connections_path().parent == dp.persona_path().parent


def test_the_template_is_seeded_on_a_fresh_install(monkeypatch, tmp_path):
    dp = _fresh(monkeypatch, tmp_path)
    assert dp.sync_connections() == "seeded"
    assert dp.connections_path().read_text() == _template_text()
    record = json.loads(dp.connections_seed_path().read_text())
    assert record["sha256"] == _sha(_template_text())


def test_the_shipped_template_is_valid_json_with_an_empty_server_block():
    """It is handed to a user to edit. A template that does not parse makes
    every server they add invisible, and they would blame their own entry."""
    body = json.loads(_template_text())
    assert body["mcpServers"] == {}, "we ship nothing connected"
    assert any(k.startswith("//") for k in body), "it has to explain itself"


def test_a_user_who_declared_a_server_keeps_it_through_an_upgrade(
        monkeypatch, tmp_path, caplog):
    """The whole promise. Their configuration is theirs, not ours."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_connections()
    mine = json.dumps({"mcpServers": {"notion": {"command": "npx"}}}, indent=2)
    dp.connections_path().write_text(mine)

    with caplog.at_level("WARNING"):
        assert dp.sync_connections() == "kept"
    assert dp.connections_path().read_text() == mine
    assert str(dp.connections_path()) in caplog.text


def test_an_untouched_template_is_brought_up_to_date(monkeypatch, tmp_path):
    """A user who never connected anything still gets today's wording."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_connections()
    old = json.dumps({"mcpServers": {}})
    dp.connections_path().write_text(old)
    dp.connections_seed_path().write_text(json.dumps({"sha256": _sha(old)}))

    assert dp.sync_connections() == "updated"
    assert dp.connections_path().read_text() == _template_text()


def test_first_run_after_this_ships_keeps_a_file_it_cannot_recognise(
        monkeypatch, tmp_path):
    dp = _fresh(monkeypatch, tmp_path)
    dp.brain_home().mkdir(parents=True, exist_ok=True)
    mine = json.dumps({"mcpServers": {"notion": {"command": "npx"}}})
    dp.connections_path().write_text(mine)
    assert dp.sync_connections() == "kept"
    assert dp.connections_path().read_text() == mine


def test_ensure_brain_home_seeds_the_connections_file_too(monkeypatch, tmp_path):
    """Startup is what matters; a file nobody creates cannot be edited."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.ensure_brain_home()
    assert dp.connections_path().exists()


def test_the_call_the_server_actually_makes_on_boot_seeds_it(monkeypatch, tmp_path):
    """`start_brain_and_speech` calls `jarvis_memory.ensure_layout()`, not
    `ensure_brain_home` — so that is the path that has to seed the file. A
    user who is told to edit a file that does not exist writes a new one in
    the wrong place."""
    dp = _fresh(monkeypatch, tmp_path)
    import jarvis_memory
    importlib.reload(jarvis_memory)
    jarvis_memory.ensure_layout()
    assert dp.connections_path().exists()


def test_every_connections_template_this_project_has_shipped_is_listed(
        monkeypatch, tmp_path):
    """Same chore, same enforcement, as the persona's history list: a
    template change that lands without its hash marks that release's users
    "edited" and they never get another improvement."""
    dp = _fresh(monkeypatch, tmp_path)
    repo = Path(__file__).resolve().parents[1]
    rel = "jarvis_home/connections.json"
    try:
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H", "--", rel],
            capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError):      # pragma: no cover
        pytest.skip("no usable git checkout here")

    missing = {}
    for commit in commits:
        blob = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{rel}"],
                              capture_output=True, timeout=60).stdout
        digest = dp._sha256(blob)
        if digest not in dp.KNOWN_CONNECTIONS_HASHES:
            missing[digest] = commit[:8]
    here = dp._sha256(dp.connections_template_path().read_bytes())
    if here not in dp.KNOWN_CONNECTIONS_HASHES:
        missing[here] = "the working tree"
    assert not missing, (
        "add these to data_paths.KNOWN_CONNECTIONS_HASHES: "
        + ", ".join(f"{d} ({c})" for d, c in missing.items()))


def test_the_persona_and_the_connections_file_share_one_mechanism():
    """Two copies of "is this the user's file or ours" is two chances to get
    the destructive half wrong."""
    import data_paths
    source = Path(data_paths.__file__).read_text()
    assert source.count("KNOWN_TEMPLATE_HASHES") >= 1
    assert "_sync_template(" in source, \
        "sync_persona and sync_connections must go through one function"


# --- 2. the declaration reaches the brain ---------------------------------

@pytest.fixture
def srv(monkeypatch, tmp_path):
    """A reloaded server module with its own data dir."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import server as server_module
    importlib.reload(server_module)
    return server_module


def _declare(dp, servers: dict) -> None:
    dp.brain_home().mkdir(parents=True, exist_ok=True)
    dp.connections_path().write_text(json.dumps({"mcpServers": servers}, indent=2))


def test_a_declared_server_is_written_into_the_config_the_brain_is_given(
        srv, tmp_path):
    """The brain runs with --strict-mcp-config, so the generated mcp.json is
    the ONLY thing it can see. A server that does not land in this file does
    not exist as far as JARVIS is concerned."""
    import data_paths
    _declare(data_paths, {"notion": {"command": "npx",
                                     "args": ["-y", "@notionhq/notion-mcp-server"],
                                     "env": {"NOTION_TOKEN": "secret"}}})
    home = tmp_path / "home"
    home.mkdir()
    written = json.loads(srv._write_mcp_config(home).read_text())

    assert written["mcpServers"]["notion"]["command"] == "npx"
    assert written["mcpServers"]["notion"]["env"] == {"NOTION_TOKEN": "secret"}
    assert "jarvis" in written["mcpServers"], "JARVIS's own tools are still there"


def test_an_http_server_is_carried_through_unchanged(srv, tmp_path):
    """Plenty of real servers are a URL, not a command."""
    import data_paths
    _declare(data_paths, {"linear": {"type": "http", "url": "https://mcp.linear.app/mcp"}})
    home = tmp_path / "home"
    home.mkdir()
    written = json.loads(srv._write_mcp_config(home).read_text())
    assert written["mcpServers"]["linear"] == {"type": "http",
                                               "url": "https://mcp.linear.app/mcp"}


def test_no_connections_file_changes_nothing(srv, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    written = json.loads(srv._write_mcp_config(home).read_text())
    assert list(written["mcpServers"]) == ["jarvis"]
    assert srv.declared_connections().problems == []


# --- 3. a refusal that explains itself ------------------------------------

def test_a_file_that_does_not_parse_is_named_not_swallowed(srv):
    """The single most likely mistake — a trailing comma — and the one that
    would otherwise make every server they added vanish in silence."""
    import data_paths
    data_paths.brain_home().mkdir(parents=True, exist_ok=True)
    data_paths.connections_path().write_text('{"mcpServers": {"notion": {},}}')

    report = srv.declared_connections()
    assert report.servers == {}
    assert report.problems, "a broken file must never be silent"
    said = " ".join(report.problems)
    assert str(data_paths.connections_path()) in said, "say WHICH file"
    assert "json" in said.lower(), "say WHAT is wrong with it"


def test_servers_written_outside_the_mcpServers_block_are_named(srv):
    """Pasting the inner half of a README's snippet is the second most likely
    mistake, and it looks exactly like nothing happening."""
    import data_paths
    data_paths.brain_home().mkdir(parents=True, exist_ok=True)
    data_paths.connections_path().write_text(
        json.dumps({"notion": {"command": "npx"}}))

    report = srv.declared_connections()
    assert report.servers == {}
    assert any("mcpServers" in p for p in report.problems), report.problems


def test_an_entry_with_no_command_or_url_is_refused_by_name(srv):
    import data_paths
    _declare(data_paths, {"notion": {"args": ["-y", "something"]}})
    report = srv.declared_connections()
    assert report.servers == {}
    assert any("notion" in p and ("command" in p or "url" in p)
               for p in report.problems), report.problems


def test_one_bad_entry_does_not_take_the_good_ones_with_it(srv):
    import data_paths
    _declare(data_paths, {"broken": {"nonsense": True},
                          "weather": {"command": "/usr/bin/weather"}})
    report = srv.declared_connections()
    assert list(report.servers) == ["weather"]
    assert any("broken" in p for p in report.problems)


def test_a_user_cannot_replace_jarvis_himself(srv, tmp_path):
    """`mcp__jarvis__*` is how the brain reaches steer_session, spawn_run and
    run_command. A server that took that name would inherit the whole acting
    surface."""
    import data_paths
    _declare(data_paths, {"jarvis": {"command": "/tmp/evil"}})
    home = tmp_path / "home"
    home.mkdir()
    written = json.loads(srv._write_mcp_config(home).read_text())

    assert written["mcpServers"]["jarvis"]["command"] != "/tmp/evil"
    assert str(Path(srv.__file__).parent / "jarvis_mcp.py") in \
        written["mcpServers"]["jarvis"]["args"]
    assert any("jarvis" in p for p in srv.declared_connections().problems)


@pytest.mark.parametrize("name", ["two words", "my__server", "no/slashes", ""])
def test_a_name_that_would_break_tool_namespacing_is_refused(srv, name):
    """Tools arrive as `mcp__<server>__<tool>`. A name with a space, a slash
    or its own double underscore makes that unparseable — and the failure
    would show up as tools that simply never appear."""
    import data_paths
    _declare(data_paths, {name: {"command": "/usr/bin/true"}})
    report = srv.declared_connections()
    assert report.servers == {}
    assert report.problems


def test_the_problems_are_sentences_a_butler_could_say(srv):
    """They are read out by `connections`, not printed to a terminal."""
    import data_paths
    data_paths.brain_home().mkdir(parents=True, exist_ok=True)
    data_paths.connections_path().write_text("not json at all")
    problems = srv.declared_connections().problems
    assert problems
    for problem in problems:
        assert problem == problem.strip() and problem.endswith(".")
        assert "Traceback" not in problem


# --- 4. the allowlist admits their tools without becoming a denylist ------

def test_the_allowlist_grants_exactly_the_servers_the_user_declared(tmp_path, monkeypatch):
    """One `mcp__<server>` grant per server named in their own file, and
    nothing else. A server they did not declare is still refused, and so is
    every built-in a future CLI invents."""
    from tests.test_brain import _config
    import brain
    monkeypatch.setattr(brain, "_WINDOWS", False)   # the macOS flag, pinned
    b = brain.Brain(_config(tmp_path, connections=["notion", "linear"]))
    granted = b.command()[b.command().index("--tools") + 1].split(",")

    assert granted[:len(brain.ALLOWED_TOOLS)] == brain.ALLOWED_TOOLS, \
        "the baseline is untouched — this adds, it never subtracts"
    assert granted[len(brain.ALLOWED_TOOLS):] == ["mcp__notion", "mcp__linear"]
    assert "mcp__github" not in granted, "not declared, not granted"


def test_no_declared_servers_leaves_the_flag_byte_identical(tmp_path, monkeypatch):
    """The overwhelmingly common install connects nothing. It must be the
    exact command it was before any of this existed."""
    from tests.test_brain import _config
    import brain
    monkeypatch.setattr(brain, "_WINDOWS", False)   # the macOS flag, pinned
    b = brain.Brain(_config(tmp_path))
    cmd = b.command()
    assert cmd[cmd.index("--tools") + 1] == ",".join(brain.ALLOWED_TOOLS)


def test_on_windows_the_flag_is_exactly_the_macos_flag(tmp_path, monkeypatch):
    """Since answer_dialog was ported (a session's own console), nothing is
    left out on Windows, and the brain is no longer told it cannot press."""
    from tests.test_brain import _config
    import brain
    monkeypatch.setattr(brain, "_WINDOWS", True)
    b = brain.Brain(_config(tmp_path, connections=["notion"]))
    granted = b.command()[b.command().index("--tools") + 1].split(",")
    assert granted == brain.ALLOWED_TOOLS + ["mcp__notion"]
    assert "cannot press a key" not in b.launch_prompt()


def test_a_tool_unavailable_on_windows_is_still_left_out_there(tmp_path, monkeypatch):
    """The mechanism stays for the next tool that cannot be ported."""
    import brain
    monkeypatch.setattr(brain, "_WINDOWS", True)
    monkeypatch.setattr(brain, "_UNAVAILABLE_ON_WINDOWS", {"answer_dialog"})
    assert "mcp__jarvis__answer_dialog" not in brain.granted_tools([])


def test_the_static_allowlist_still_names_only_jarvis_and_the_two_web_tools():
    """ALLOWED_TOOLS is the pinned baseline and stays pinned. The user's
    grants are computed per-launch from their file — they are never added
    here, where a coding tool could drift in beside them."""
    import brain
    assert {t for t in brain.ALLOWED_TOOLS if not t.startswith("mcp__")} == \
        {"WebSearch", "WebFetch"}
    assert all(t.startswith("mcp__jarvis__") for t in brain.ALLOWED_TOOLS
               if t.startswith("mcp__"))


def test_the_grant_is_a_whole_server_not_a_guess_at_its_tools(tmp_path):
    """Verified against `claude` 2.1.259: `--tools mcp__weather` admits
    `mcp__weather__forecast` and `mcp__weather__tide`. JARVIS cannot know a
    server's tool names before it starts, so naming the server is the only
    grant it can make honestly."""
    from tests.test_brain import _config
    import brain
    b = brain.Brain(_config(tmp_path, connections=["weather"]))
    granted = b.command()[b.command().index("--tools") + 1].split(",")
    assert "mcp__weather" in granted
    assert not any(g.startswith("mcp__weather__") for g in granted)


def test_the_server_hands_the_brain_the_names_it_accepted(srv, tmp_path):
    """The two halves have to agree: a server merged into mcp.json but left
    off the grant would be present and unusable, which is precisely the
    silent failure this feature exists to prevent."""
    import data_paths
    import brain
    _declare(data_paths, {"notion": {"command": "npx"},
                          "nope": {"args": ["x"]}})
    home = tmp_path / "home"
    home.mkdir()
    written = json.loads(srv._write_mcp_config(home).read_text())
    config = brain.BrainConfig.from_env(home)
    config.connections = sorted(srv.LAST_CONNECTIONS.servers)

    merged = set(written["mcpServers"]) - {"jarvis"}
    assert merged == set(config.connections), \
        "everything merged is granted, and nothing else is"


# --- 5. JARVIS knows what actually started --------------------------------

@pytest.mark.asyncio
async def test_the_brain_records_what_connected_and_what_did_not(tmp_path):
    """The CLI says so in its init event and JARVIS used to throw it away —
    which is exactly how a server that failed to start became silence."""
    from tests.test_brain import _config
    import brain
    home = tmp_path / "jarvis"
    home.mkdir(parents=True)
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {
        "weather": {"command": "/usr/bin/true"},
        "broken-notion": {"command": "/nowhere/at/all"},
    }}))
    b = brain.Brain(_config(tmp_path, mcp_config=home / "mcp.json"))
    await b.start()
    try:
        assert b.connected_servers == ["weather"]
        assert b.failed_servers == ["broken-notion"]
        assert "mcp__weather__probe" in b.live_tools
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_inventory_is_rebuilt_for_each_generation(tmp_path):
    """A rotation spawns a new process; a stale inventory would have JARVIS
    describing servers that are no longer running."""
    from tests.test_brain import _config
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        assert b.connected_servers == []
        assert b.live_tools == ["ListAgents"]
    finally:
        await b.stop()


# --- 6. every added tool costs context ------------------------------------

@pytest.mark.asyncio
async def test_the_resident_floor_is_measured_not_guessed(tmp_path):
    """The warm-up turn carries the system prompt, CLAUDE.md and every tool
    schema, and nothing else. Its context IS the price of being connected."""
    from tests.test_brain import _config
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        assert b.baseline_tokens == 10 + 9000      # the prompt as sent; cache_creation is it being cached, not more of it
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_the_budget_governs_the_conversation_not_the_tool_schemas(tmp_path):
    """Measured against `claude` 2.1.259: a twelve-tool server costs about
    3,300 resident tokens, on every single turn. Charged against the rotation
    budget, five of those would cut what JARVIS remembers of the conversation
    by a quarter — for adding servers, silently. So the floor is measured and
    excluded, and the budget means what it says."""
    from tests.test_brain import _config
    import brain
    floor = 10 + 9000 + 1000                  # what the stand-in reports
    conversation = (10 + 18000 + 1000) - floor
    b = brain.Brain(_config(tmp_path, context_budget=conversation + 1))
    await b.start()
    try:
        await b.turn("hello")
        assert b.rotation_pending is False, \
            "the tool schemas are not the conversation"
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_a_conversation_that_outgrows_the_budget_still_rotates(tmp_path):
    from tests.test_brain import _config
    import brain
    floor = 10 + 9000 + 1000
    conversation = (10 + 18000 + 1000) - floor
    b = brain.Brain(_config(tmp_path, context_budget=conversation - 1))
    await b.start()
    try:
        await b.turn("hello")
        assert b.rotation_pending is True
    finally:
        await b.stop()


# --- 7. "what are you connected to?" --------------------------------------

class _Brain:
    """A brain that has already reported an init event."""
    def __init__(self, servers=(), tools=()):
        self.mcp_servers = list(servers)
        self.live_tools = list(tools)
        self.baseline_tokens = 21000
        self.current_origin = "user"

    def _with(self, status):
        return [str(s["name"]) for s in self.mcp_servers if s["status"] == status]

    @property
    def connected_servers(self):
        return self._with("connected")

    @property
    def failed_servers(self):
        return self._with("failed")

    def tools_from(self, server):
        prefix = f"mcp__{server}__"
        return [t[len(prefix):] for t in self.live_tools if t.startswith(prefix)]


def _wire(srv, brain_obj, connections=()):
    srv.brain_instance = brain_obj
    srv.LAST_CONNECTIONS = srv.ConnectionsReport(
        servers={n: {"command": "x"} for n in connections})
    return srv.TOOL_HANDLERS["connections"]


def test_connections_is_read_only(srv):
    """"What are you connected to" must not depend on who is talking — a
    watcher turn answering it changes nothing."""
    assert "connections" in srv.TOOL_HANDLERS
    assert "connections" not in srv.ACTING_TOOLS


def test_a_bare_install_says_so_and_says_where_to_add_one(srv):
    """This is also the answer to "why can't you see my calendar" — it has to
    point somewhere, not just say no."""
    import data_paths
    handler = _wire(srv, _Brain(servers=[{"name": "jarvis", "status": "connected"}]))
    said = handler({})
    assert str(data_paths.connections_path()) in said
    assert "notion" not in said.lower(), "we invent nothing"


def test_it_names_the_servers_that_are_actually_running(srv):
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"},
                 {"name": "notion", "status": "connected"}],
        tools=["mcp__notion__search_pages", "mcp__notion__fetch_page"]),
        connections=["notion"])
    said = handler({})
    assert "notion" in said
    assert "search_pages" in said and "fetch_page" in said, \
        "'what can you do with my notion' is answered from the live tool list"


def test_jarvis_does_not_list_himself_as_a_connection(srv):
    """He is not something the user connected, and reading his thirty tools
    out would bury the one server they asked about."""
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"}],
        tools=["mcp__jarvis__spawn_run", "mcp__jarvis__steer_session"]))
    assert "spawn_run" not in handler({})


def test_a_server_that_would_not_start_is_named_out_loud(srv):
    """The silent failure this whole feature turns on. The CLI reports it and
    nothing else on the machine will ever mention it."""
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"},
                 {"name": "notion", "status": "failed"}]),
        connections=["notion"])
    said = handler({})
    assert "notion" in said
    lower = said.lower()
    assert "start" in lower or "running" in lower, said


def test_a_server_refused_before_it_was_ever_started_is_named_too(srv):
    """A malformed entry never reaches the CLI, so it appears in no init
    event. Without this it would be the one failure with no witness."""
    import data_paths
    handler = _wire(srv, _Brain(servers=[{"name": "jarvis", "status": "connected"}]))
    srv.LAST_CONNECTIONS = srv.ConnectionsReport(
        problems=['"notion" has neither a "command" nor a "url", so there is '
                  'nothing for me to start — I left it out.'])
    said = handler({})
    assert "notion" in said
    assert "command" in said


def test_a_tool_that_is_present_but_not_permitted_says_why(srv):
    """If the CLI ever enforces `--tools` over MCP names again, a server the
    user did not declare would connect and be unusable. Silence there is the
    one outcome that teaches a user the feature is broken."""
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"},
                 {"name": "stowaway", "status": "connected"}],
        tools=["mcp__stowaway__do_it"]),
        connections=[])                       # declared nothing
    said = handler({})
    assert "stowaway" in said
    assert "connections" in said.lower() or "declare" in said.lower(), \
        "say what would make it permitted, not just that it is not"


def test_the_price_of_being_connected_is_said_in_tokens(srv):
    """Measured against `claude` 2.1.259: ~250 tokens per tool, resident on
    every turn. A user who adds five servers is entitled to know."""
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"},
                 {"name": "notion", "status": "connected"}],
        tools=[f"mcp__notion__t{i}" for i in range(12)]),
        connections=["notion"])
    said = handler({})
    assert "token" in said.lower()
    assert "3,000" in said or "3000" in said or "3,100" in said, said


def test_it_answers_about_one_service_when_asked(srv):
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"},
                 {"name": "notion", "status": "connected"},
                 {"name": "linear", "status": "connected"}],
        tools=["mcp__notion__search_pages", "mcp__linear__list_issues"]),
        connections=["notion", "linear"])
    said = handler({"service": "notion"})
    assert "search_pages" in said
    assert "list_issues" not in said


def test_an_unknown_service_is_refused_by_name_not_invented(srv):
    handler = _wire(srv, _Brain(
        servers=[{"name": "jarvis", "status": "connected"}]))
    said = handler({"service": "calendar"})
    assert "calendar" in said


def test_it_survives_a_brain_that_has_not_started(srv):
    """A tool that raises here reaches the user as "that tool failed"."""
    srv.brain_instance = None
    srv.LAST_CONNECTIONS = srv.ConnectionsReport(servers={"notion": {"command": "x"}})
    said = srv.TOOL_HANDLERS["connections"]({})
    assert "notion" in said


def test_the_answer_fits_in_a_tool_result(srv):
    """Twenty servers with ten tools each still has to come back inside the
    1,500-character cap the brain's context budget depends on."""
    servers = [{"name": "jarvis", "status": "connected"}]
    tools = []
    for i in range(20):
        servers.append({"name": f"service{i}", "status": "connected"})
        tools += [f"mcp__service{i}__tool_number_{j}" for j in range(10)]
    handler = _wire(srv, _Brain(servers=servers, tools=tools),
                    connections=[f"service{i}" for i in range(20)])
    assert len(handler({})) <= srv.TOOL_RESULT_CAP


def test_the_three_tool_sets_agree_about_connections(srv):
    import brain
    import jarvis_mcp
    assert "mcp__jarvis__connections" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(srv.TOOL_HANDLERS)


# --- 8. a stranger's server is a stranger's words -------------------------
#
# The user vouches for the SERVER — they chose the code and gave it their
# token. They do not vouch for what it returns: a Notion page someone shared
# with them, a GitHub issue a stranger opened, a Slack message. That is
# exactly the WebFetch situation, so it goes through exactly the WebFetch
# mechanism rather than a second one.

def test_a_connected_services_result_taints_the_turn(tmp_path):
    import brain
    assert brain.untrusted_tool_source("mcp__notion__fetch_page") == "notion"
    assert brain.untrusted_tool_source("WebFetch") == "a web page"
    assert brain.untrusted_tool_source("WebSearch") == "a web page"


def test_jarvis_own_tools_do_not_taint_a_turn():
    """His own results are already wrapped in `<session-output>` where they
    carry someone else's words, and gating them would shut the assistant."""
    import brain
    assert brain.untrusted_tool_source("mcp__jarvis__list_sessions") is None
    assert brain.untrusted_tool_source("mcp__jarvis__spawn_run") is None


@pytest.mark.asyncio
async def test_the_turn_knows_which_service_tainted_it(tmp_path):
    """The flag has to be true WHILE the turn runs — that is the only moment
    an acting tool could be called on the strength of what came back."""
    from tests.test_brain import _config
    import brain
    b = brain.Brain(_config(tmp_path))
    await b.start()
    try:
        seen = []
        r = await b.turn("MCPTOOL:notion__fetch_page please",
                         on_delta=lambda d: seen.append(b.turn_untrusted_source))
        assert r.tools == ["mcp__notion__fetch_page"], r.tools
        assert seen and all(s == "notion" for s in seen), seen
    finally:
        await b.stop()
    assert b.turn_untrusted_source is None, "no turn in flight, nothing to taint"


def test_an_unsupervised_action_after_a_connected_service_is_refused(srv):
    """`spawn_run` starts a process that edits files with nobody watching. A
    page — or a Notion page, or a GitHub issue — that asks for one is a
    stranger using JARVIS, and the user hears nothing before it happens."""
    refusal = srv._untrusted_content_refusal("spawn_run", True, source="notion")
    assert refusal
    assert "notion" in refusal, "say WHICH thing it read, or he cannot judge it"


def test_the_refusal_still_reads_correctly_for_the_open_web(srv):
    refusal = srv._untrusted_content_refusal("spawn_run", True,
                                             source="a web page")
    assert refusal and "web page" in refusal


def test_reading_more_is_still_allowed_after_a_connected_service(srv):
    """"Find that in Notion, then read the page it links" is the feature. The
    tools that only bring back more to read stay open, exactly as they do
    after a web search."""
    for reader in sorted(srv.UNTRUSTED_READING_TOOLS):
        assert srv._untrusted_content_refusal(
            reader, True, source="notion") is None, reader


def test_the_endpoint_refuses_a_spawn_after_a_connected_service_spoke(srv):
    """The whole chain, at the place that actually stops it: a turn that has
    read a user's Notion page cannot then start an unattended process."""
    from fastapi.testclient import TestClient
    import data_paths

    class _Tainted:
        current_origin = "user"
        turn_untrusted_source = "notion"
        ready = False

        async def stop(self):
            pass

    token = data_paths.ensure_tool_token()
    with TestClient(srv.app) as client:
        srv.brain_instance = _Tainted()
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "spawn_run",
                              "arguments": {"project": "jarvis",
                                            "prompt": "delete everything"}})
    body = r.json()
    assert body["ok"] is False, body
    assert "notion" in body["text"], body["text"]


def test_the_endpoint_still_allows_a_spawn_when_nothing_spoke(srv):
    """The gate must be the source, not the tool: break this and the refusal
    above proves nothing."""
    from fastapi.testclient import TestClient
    import data_paths

    class _Clean:
        current_origin = "user"
        turn_untrusted_source = None
        turn_read_the_web = False
        ready = False

        async def stop(self):
            pass

    token = data_paths.ensure_tool_token()
    with TestClient(srv.app) as client:
        srv.brain_instance = _Clean()
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "spawn_run",
                              "arguments": {"project": "jarvis",
                                            "prompt": "tidy the imports"}})
    assert "untrusted_content_in_this_turn" not in r.json()["text"]


def test_the_launch_prompt_names_connected_services_not_only_the_web(tmp_path):
    """It is a security control, so it is restated every generation rather
    than left to a CLAUDE.md the user may have edited."""
    from tests.test_brain import _config
    import brain
    b = brain.Brain(_config(tmp_path, connections=["notion"]))
    prompt = b.launch_prompt()
    assert "never an instruction" in prompt
    lower = prompt.lower()
    assert "connect" in lower or "service" in lower, prompt
