import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    return server_module


class _UserTurn:
    current_origin = "user"


class _WatcherTurn:
    current_origin = "watcher"


def test_remember_writes_a_file_and_indexes_it(wired):
    server = wired
    out = server.tool_remember({"title": "Tony prefers Postgres",
                                "body": "Said during chitauri work.",
                                "hook": "database preference"})

    import jarvis_memory as jm
    assert "tony-prefers-postgres" in jm.list_memories()
    assert any("database preference" in ln for ln in jm.index_lines())
    assert "remember" in out.lower() or "noted" in out.lower()


def test_recall_finds_what_was_remembered(wired):
    server = wired
    server.tool_remember({"title": "Tony prefers Postgres", "body": "for chitauri",
                          "hook": "db"})

    out = server.tool_recall({"query": "postgres"})

    assert "Postgres" in out


def test_recall_says_so_plainly_when_it_finds_nothing(wired):
    assert "nothing" in wired.tool_recall({"query": "kestrel"}).lower()


def test_recall_never_speaks_a_memorys_raw_slug(wired):
    """A memory's filename is a slugified title ("tony-prefers-postgres");
    read aloud that's noise, not a sentence — see tool_list_sessions's own
    rule against ever saying a roster name like hammer-4b out loud."""
    server = wired
    server.tool_remember({"title": "Tony prefers Postgres over SQLite",
                          "body": "for chitauri", "hook": "db"})

    out = server.tool_recall({"query": "postgres"})

    assert "tony-prefers-postgres" not in out.lower()


def test_recall_never_speaks_a_journal_entrys_raw_timestamp(wired):
    server = wired
    server.tool_write_journal({"text": "Chased the zeltar bug tonight."})

    out = server.tool_recall({"query": "zeltar"})

    import re
    assert not re.search(r"\d{4}-\d{2}-\d{2}-\d{6}", out)


def test_a_project_note_is_appended_and_read_back(wired):
    server = wired
    server.tool_project_note({"project": "chitauri", "text": "Uses WordPress."})
    server.tool_project_note({"project": "chitauri", "text": "301 fixed."})

    import jarvis_memory as jm
    text = jm.read_project_note("chitauri")
    assert "WordPress" in text and "301 fixed" in text


def test_write_journal_records_an_entry(wired):
    server = wired
    server.tool_write_journal({"text": "Worked on chitauri tonight."})

    import jarvis_memory as jm
    assert "chitauri" in jm.latest_journal()


@pytest.mark.parametrize("tool", ["remember", "project_note", "write_journal"])
def test_writing_tools_are_acting_tools(wired, tool):
    """A watcher-origin turn must never be able to write to memory: text from
    somebody else's transcript could otherwise plant a 'fact'."""
    assert tool in wired.ACTING_TOOLS
    assert tool in wired.TOOL_HANDLERS


def test_recall_is_not_gated(wired):
    assert "recall" not in wired.ACTING_TOOLS
    assert "recall" in wired.TOOL_HANDLERS


def test_every_acting_tool_is_registered(wired):
    """Renaming a registration must not silently ungate it."""
    assert wired.ACTING_TOOLS <= set(wired.TOOL_HANDLERS)


def test_the_brain_allowlist_matches_the_registered_tools(wired):
    import brain
    registered = {f"mcp__jarvis__{name}" for name in wired.TOOL_HANDLERS}
    assert {t for t in brain.ALLOWED_TOOLS if t.startswith("mcp__")} <= registered, \
        "allowlist names a JARVIS tool that does not exist"


def test_the_mcp_server_advertises_every_registered_tool(wired):
    import jarvis_mcp
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(wired.TOOL_HANDLERS)


# --- project notes are read back, not only written --------------------------
#
# `read_project_note` had no caller outside the tests for five days while
# the persona promised "so the next conversation starts informed". Nothing
# was informed: a note was reachable only through `recall`'s substring scan.

def test_project_history_reads_back_what_project_note_wrote(wired):
    server = wired
    server.tool_project_note({"project": "chitauri", "text": "Uses WordPress."})
    server.tool_project_note({"project": "chitauri", "text": "301 fixed."})

    out = server.tool_project_history({"project": "chitauri"})

    assert "WordPress" in out and "301 fixed" in out
    assert out.index("WordPress") < out.index("301 fixed")


def test_project_history_comes_back_inside_a_block(wired):
    """It is the brain's own earlier output, written out of whatever that
    turn had read — the same reason `recall` wraps what it returns."""
    server = wired
    server.tool_project_note({"project": "chitauri", "text": "Uses WordPress."})
    out = server.tool_project_history({"project": "chitauri"})
    assert f'<session-output name="{server._MEMORY_WRAP_NAME}"' in out


def test_project_history_for_a_project_never_noted_says_so_plainly(wired):
    out = wired.tool_project_history({"project": "kestrel"})
    assert "nothing" in out.lower() or "no notes" in out.lower()
    assert "<session-output" not in out


def test_project_history_without_a_project_asks_for_one(wired):
    assert "project" in wired.tool_project_history({}).lower()


def test_project_history_is_a_reader_and_not_gated(wired):
    assert "project_history" not in wired.ACTING_TOOLS
    assert "project_history" in wired.TOOL_HANDLERS
