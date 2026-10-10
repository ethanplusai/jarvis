"""An outward call from a user's own MCP server needs a human yes.

Measured live, 2026-09-22. The user asked JARVIS to rehearse a LinkedIn
post, read it back, and wait for his yes. The brain called create_post with
confirmation already set to true, published it, and was then killed by the
turn watchdog before it could say so. The user saw nothing, re-ran the
flow, and published a second copy. LinkedIn has no delete tool, so the
recovery was manual.

Nothing in JARVIS's code was in a position to stop that. A
`mcp__linkedin__create_post` never enters his process: the CLI runs the
user's server itself. His acting-tool gate, his origin check and his
untrusted-content refusal all sit on `/internal/tool`, which that call does
not take. The only thing between a spoken instruction and a live post was
the model choosing to comply.

It turns out there IS an enforcement point. Verified against the installed
CLI (2.1.270) rather than assumed:

  - A PreToolUse hook passed through `--settings` DENIES a call even with
    `--dangerously-skip-permissions`. That flag removes the prompt, not the
    hook.
  - The matcher matches MCP names: `mcp__(?!jarvis__).*` intercepted a real
    `mcp__tiny__publish_thing`, the server never ran it, and the denial
    record carried the payload `{"text": "hello world"}`.

So JARVIS sees the text of the post BEFORE it is posted, with a
synchronous reply channel — which `on_tool()` cannot give him, firing with
no arguments after the call has already gone.

This file is the policy. The gate itself is `/internal/pretool`.
"""

import pytest

import pretool_gate


# --- which calls are outward ---------------------------------------------
#
# Reads are the common path and must stay free: gating them would put a
# round trip and an approval in front of "what's in my inbox".

@pytest.mark.parametrize("tool", [
    "mcp__linkedin__get_feed",
    "mcp__linkedin__get_my_profile",
    "mcp__linkedin__search_posts",
    "mcp__linkedin__get_post_comments",
    "mcp__paperclip__paperclipListIssues",
    "mcp__paperclip__paperclipGetIssue",
    "mcp__outlook__read_message",
])
def test_reads_are_not_gated(tool):
    assert pretool_gate.classify(tool) == "read", tool


@pytest.mark.parametrize("tool", [
    "mcp__linkedin__create_post",
    "mcp__linkedin__send_message",
    "mcp__linkedin__comment_on_post",
    "mcp__linkedin__reply_to_comment",
    "mcp__linkedin__connect_with_person",
    "mcp__paperclip__paperclipCreateIssue",
    "mcp__paperclip__paperclipUpdateIssue",
    "mcp__paperclip__paperclipAddComment",
])
def test_the_ones_that_reach_other_people_are_outward(tool):
    assert pretool_gate.classify(tool) == "outward", tool


def test_a_verb_nobody_has_seen_before_is_outward():
    """The safe direction. A server the user adds next year gets its unknown
    verbs held, not waved through — the cost of being wrong is one approval
    click, against a public post that cannot be recalled."""
    assert pretool_gate.classify("mcp__whatever__frobnicate") == "outward"
    assert pretool_gate.classify("mcp__whatever__") == "outward"


def test_jarvis_own_tools_are_never_this_gates_business():
    """They are already gated at /internal/tool, and double-gating deadlocks:
    the gate's own bookkeeping would have to call through the gate."""
    for tool in ("mcp__jarvis__spawn_run", "mcp__jarvis__read_file"):
        assert pretool_gate.classify(tool) == "read", tool


def test_a_builtin_is_not_an_mcp_call():
    for tool in ("Bash", "WebFetch", "WebSearch"):
        assert pretool_gate.classify(tool) == "read", tool


# --- the digest binds approval to the exact payload ----------------------

def test_the_digest_covers_the_tool_and_its_arguments():
    a = pretool_gate.digest_for("mcp__linkedin__create_post", {"text": "hello"})
    b = pretool_gate.digest_for("mcp__linkedin__create_post", {"text": "hello."})
    c = pretool_gate.digest_for("mcp__linkedin__send_message", {"text": "hello"})
    assert a != b, "approving one wording must not approve another"
    assert a != c, "approving a post must not approve a message"


def test_the_digest_ignores_key_order_but_nothing_else():
    """The brain re-emits the same call to retry it; the JSON key order is
    not part of what the user approved."""
    a = pretool_gate.digest_for("mcp__x__send", {"to": "a", "text": "hi"})
    b = pretool_gate.digest_for("mcp__x__send", {"text": "hi", "to": "a"})
    assert a == b
    assert a != pretool_gate.digest_for("mcp__x__send", {"to": "b", "text": "hi"})


def test_the_digest_survives_a_payload_it_cannot_serialise():
    """A hook must never fail open because an argument was odd."""
    assert pretool_gate.digest_for("mcp__x__send", {"when": object()})


# --- a read verb at the front does not make a tool a read ----------------
#
# Found by review, reproduced against the real module: classify() answered
# "read" for every one of these, so each would have reached the user's
# server with no approval card and nothing said.

@pytest.mark.parametrize("tool", [
    "mcp__x__report_issue",        # files an issue somebody else will read
    "mcp__x__log_expense",         # writes to the user's books
    "mcp__x__check_out",           # takes the thing
    "mcp__x__export_contacts",     # creates an export, often shareable
    "mcp__x__download_invoice",    # writes a file onto the user's disk
    "mcp__x__search_and_reply",    # the reply is the point
    "mcp__x__get_or_create_page",  # creates
    "mcp__x__list_and_archive",    # archives
    "mcp__x__read_and_delete",     # deletes
])
def test_a_compound_name_is_not_a_read(tool):
    assert pretool_gate.classify(tool) == "outward", tool


@pytest.mark.parametrize("tool", [
    "mcp__x__get_inbox", "mcp__x__list_issues", "mcp__x__search_people",
    "mcp__x__read_message", "mcp__x__fetch_page", "mcp__x__describe_table",
    "mcp__x__query_rows", "mcp__x__show_status", "mcp__x__preview_invoice",
    "mcp__x__history", "mcp__x__diff_branches", "mcp__x__count_rows",
])
def test_the_ordinary_reads_still_pass(tool):
    """The common path has to stay free, or every "what's in my inbox" waits
    on a human."""
    assert pretool_gate.classify(tool) == "read", tool


def test_a_conjunction_anywhere_makes_it_outward():
    """"and" or "or" in a tool's name means it does a second thing, and the
    second thing is what gets you."""
    for tool in ("mcp__x__find_and_replace", "mcp__x__get_or_create",
                 "mcp__x__list_and_notify"):
        assert pretool_gate.classify(tool) == "outward", tool


def test_a_noun_that_looks_like_a_verb_does_not_gate_a_read():
    """`get_post_comments` reads comments ON a post. A blanket scan for write
    words cannot tell that from `create_post`, which is why POSITION decides:
    a word is a verb when it leads the name or follows a conjunction, and
    nowhere else."""
    for tool in ("mcp__x__get_post_comments", "mcp__x__list_share_links",
                 "mcp__x__get_order_status", "mcp__x__search_book_titles"):
        assert pretool_gate.classify(tool) == "read", tool


def test_a_trailing_conjunction_is_held():
    """Malformed, so it takes the safe direction like every other unknown."""
    assert pretool_gate.classify("mcp__x__get_and") == "outward"
