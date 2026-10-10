"""The two gates membership causes, said as two sets.

`ACTING_TOOLS` is one name over two different consequences
(server.py's `/internal/tool`): an ORIGIN gate, which refuses the call on a
turn nobody drove, and a FOREIGN-TEXT gate, which refuses it on a turn that
has read somebody else's words. Six of its members are readers, and all six
then have to be exempted from the second gate by a separate set — so the
code kept `UNTRUSTED_READING_TOOLS` and `TAINT_EXEMPT_ACTING` purely to
undo half of the first set's effect for some of its members.

Two sets wearing one name is not a style complaint. It produced a live
regression the day `business_report` was gated: it went into the first set,
nothing put it in either undo-set, and "show me Google, then Meta" stopped
working in one turn. There was no cheap correct move, because the correct
move was invisible.

So the second gate is now a set of its own, derived from the first minus
the two documented exemptions. One source of truth, readable in both
directions: the exemption sets still say WHY each tool is exempt, and
`FOREIGN_TEXT_REFUSED` says what the gate actually consults.
"""

import importlib

import pytest


@pytest.fixture
def server():
    import server as server_module
    importlib.reload(server_module)
    return server_module


def test_the_second_gate_is_a_set_and_not_two_subtractions(server):
    assert hasattr(server, "FOREIGN_TEXT_REFUSED"), \
        "the taint gate is still expressed as ACTING_TOOLS minus two undo-sets"


def test_it_is_exactly_the_acting_tools_that_are_not_exempt(server):
    assert server.FOREIGN_TEXT_REFUSED == (
        server.ACTING_TOOLS - server.UNTRUSTED_READING_TOOLS - server.TAINT_EXEMPT_ACTING)


def test_every_reader_is_an_acting_tool(server):
    """A name in an exemption set that is not gated in the first place
    exempts nothing and is a typo."""
    assert server.UNTRUSTED_READING_TOOLS <= server.ACTING_TOOLS
    assert server.TAINT_EXEMPT_ACTING <= server.ACTING_TOOLS


def test_the_two_gates_do_not_overlap_by_accident(server):
    assert not (server.FOREIGN_TEXT_REFUSED & server.UNTRUSTED_READING_TOOLS)
    assert not (server.FOREIGN_TEXT_REFUSED & server.TAINT_EXEMPT_ACTING)


def test_the_refusal_consults_the_positive_set(server):
    import inspect
    source = inspect.getsource(server._untrusted_content_refusal)
    assert "FOREIGN_TEXT_REFUSED" in source, \
        "the refusal still reasons by subtraction at the call site"


@pytest.mark.parametrize("tool", ["spawn_run", "run_command", "steer_session",
                                  "start_build", "remember", "write_journal"])
def test_the_dangerous_ones_are_still_refused_after_a_read(server, tool):
    assert tool in server.FOREIGN_TEXT_REFUSED
    assert server._untrusted_content_refusal(tool, True, source="a web page")


@pytest.mark.parametrize("tool", ["read_page", "look_at_page", "github_repo",
                                  "look_at_screen", "what_is_on_screen",
                                  "business_report", "answer_dialog"])
def test_the_exempt_ones_still_survive_a_read(server, tool):
    assert tool not in server.FOREIGN_TEXT_REFUSED
    assert server._untrusted_content_refusal(tool, True, source="a web page") is None


def test_a_tool_outside_the_origin_gate_is_never_refused_for_taint(server):
    """`connections`, `usage_status`, `list_projects` and the rest answer on
    any turn; the taint gate has no opinion about them."""
    for tool in ("connections", "usage_status", "list_projects", "read_file"):
        assert tool not in server.ACTING_TOOLS
        assert server._untrusted_content_refusal(tool, True, source="a web page") is None
