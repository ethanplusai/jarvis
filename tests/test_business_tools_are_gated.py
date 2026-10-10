"""Every business tool that writes or reaches outward needs the user talking.

`business_report` was not in the set. It is not a local read: it calls
`providers.perform(..., read=True)`, which makes live HTTPS requests with
the user's own credentials to `googleads.googleapis.com`,
`graph.facebook.com`, `api.twilio.com` and `api.ads.openai.com`
(business_providers.py:183, 193, 208, 222). So an event-driven turn — the
journal request, or the approval resume — could reach four third parties on
the user's accounts with nobody in the room.

The gate is `ACTING_TOOLS`, whose own comment says what it really means:
"Tools that may only run while the user is the one talking." A tool outside
it runs on any turn, including ones nobody asked for.

This file pins the DECISION for every business tool rather than a list of
names. A fifth tool added next year fails here until somebody says which
side it is on, which is the same shape as
tests/test_header_lines.py's "the list IS the source file".
"""

import pytest


# Why each business tool is or is not gated. Adding a tool without adding it
# here fails `test_every_business_tool_has_been_decided`.
DECIDED = {
    "business_status": (False,
                        "pure local reads — connections(), briefing(), actions(), "
                        "records() all hit the local store and nothing else"),
    "business_action": (False,
                        "a pure local read of one approval card in the local "
                        "store; it sends nothing and changes nothing"),
    "business_find": (False,
                      "a pure local read of the records table: it matches the "
                      "words it is given here and returns handles; it sends "
                      "nothing and changes nothing"),
    "business_propose": (True, "stages an action the user will be asked to approve"),
    "business_record": (True, "writes a durable record to the user's own books"),
    "business_report": (True,
                        "live outward HTTPS to Google, Meta, Twilio and OpenAI "
                        "with the user's credentials"),
}


@pytest.fixture
def server():
    import importlib
    import server as server_module
    importlib.reload(server_module)
    return server_module


def test_every_business_tool_has_been_decided(server):
    """Wherever it is registered: `business_action` renders its untrusted
    block in server.py, so business_api's table alone would miss it."""
    import business_api
    business = set(business_api.TOOL_HANDLERS) | {
        name for name in server.TOOL_HANDLERS if name.startswith("business_")}
    undecided = sorted(business - set(DECIDED))
    assert not undecided, (
        f"these business tools run on turns nobody asked for and nobody has "
        f"decided whether that is safe: {undecided}")


@pytest.mark.parametrize("tool,gated,why", [(t, g, w) for t, (g, w) in DECIDED.items()])
def test_the_decision_is_what_the_code_does(server, tool, gated, why):
    assert (tool in server.ACTING_TOOLS) is gated, f"{tool}: {why}"


def test_the_report_tool_really_does_reach_third_parties(server):
    """The reason business_report is gated, held against the code rather
    than against this file's own say-so."""
    import inspect
    import business_providers
    source = inspect.getsource(business_providers)
    for host in ("googleads.googleapis.com", "graph.facebook.com",
                 "api.twilio.com", "api.ads.openai.com"):
        assert host in source, host
    assert "business_report" in server.ACTING_TOOLS


def test_two_provider_reports_in_one_turn(server):
    """Reading Google must not make reading Meta an untrusted action.

    `business_report` taints the turn, because what four ad platforms say
    about your campaigns is somebody else's text. Gating it on ORIGIN is
    right; refusing it a second time is not, and it broke a working flow the
    moment the tool was gated: "show me Google, then Meta" became two turns,
    and "how's business?" — which taints as well — disabled reports for the
    rest of the turn with a refusal about untrusted content."""
    assert server._untrusted_content_refusal(
        "business_report", True, source="provider reports") is None


def test_the_opener_does_not_disable_the_follow_up(server):
    """`business_status` taints too, and it is how any such conversation
    starts."""
    assert "business_status" in server.TAINTING_TOOLS
    assert server._untrusted_content_refusal(
        "business_report", True, source="your own business records") is None


def test_it_is_still_gated_on_origin(server):
    """Exempt from the SECOND gate, never the first: an event-driven turn
    still may not reach four third parties on the user's accounts."""
    assert "business_report" in server.ACTING_TOOLS
