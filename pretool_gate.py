"""What a user's own MCP server is allowed to do without asking.

JARVIS's own tools are gated at `/internal/tool`: the origin check, the
acting-tool gate and the untrusted-content refusal all live there. A tool
from a server the USER declared never goes near it — the CLI starts that
server itself and calls it directly, so none of those gates are on the
path. Measured live: the brain published a LinkedIn post during a turn
whose instructions were to rehearse it and wait, and nothing could have
stopped it.

The enforcement point is a PreToolUse hook. Verified against the installed
CLI (2.1.270), not assumed: a hook supplied through `--settings` denies a
call even under `--dangerously-skip-permissions` (that flag removes the
prompt, not the hook), the matcher matches MCP names, the user's server
never runs the tool, and the hook is handed the arguments. So the decision
below is made with the text of the post in hand, before it is posted.

This module is only the POLICY. It does no I/O, so it is cheap to call on
every tool the brain reaches for and cheap to test.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

# JARVIS's own server. Never this gate's business: already gated at
# `/internal/tool`, and gating it here would deadlock — the gate's own
# bookkeeping goes through tools of his.
OWN_PREFIX = "mcp__jarvis__"

# Verbs that only look. The list is deliberately of VERBS rather than of
# tool names: a user can declare any server tomorrow, and a policy written
# against the eighteen tools one of them happens to expose today would be
# silently wrong the moment they add a nineteenth.
#
# Matched against the tool's own name with the `mcp__<server>__` prefix
# stripped, lowercased, and with word boundaries taken at `_` and at
# camelCase humps — `paperclipListIssues` has to read as "list".
READ_VERBS = frozenset({
    "get", "list", "search", "read", "fetch", "find", "show", "view",
    "describe", "inspect", "query", "lookup", "count", "status",
    "browse", "scan", "diff", "history", "summary", "summarise",
    "summarize", "preview",
})

# A read verb at the FRONT is necessary and not sufficient, but a blanket
# scan for write words cannot tell a verb from a noun: `get_post_comments`
# reads comments ON a post, and "post" there is not something the tool does.
#
# So POSITION decides. A word is a verb when it leads the name or follows a
# conjunction, and a verb that is not a read verb makes the whole thing
# outward. Measured against the real module, every one of these used to
# answer "read" and would have reached the user's server with no card:
#
#   report_issue      files something somebody else will read
#   log_expense       writes to the user's books
#   check_out         takes the thing
#   export_contacts   creates an export, often shareable
#   download_invoice  writes a file onto the user's disk
#   search_and_reply  the reply is the point
#   read_and_delete   deletes
#
# The first five are caught by `report`, `log`, `check`, `export` and
# `download` leaving READ_VERBS — each is a write at least as often as it is
# a read, and the safe direction costs one approval click. The last two are
# caught by the conjunction.
CONJUNCTIONS = frozenset({"and", "or", "then", "plus"})


def _words(name: str) -> list[str]:
    """The tool's own name as lowercase words, split on `_` and camel humps."""
    out: list[str] = []
    for chunk in name.split("_"):
        word = ""
        for ch in chunk:
            if ch.isupper() and word:
                out.append(word.lower())
                word = ch
            else:
                word += ch
        if word:
            out.append(word.lower())
    return out


def classify(tool_name: str) -> str:
    """'read' (let it through) or 'outward' (a human says yes first).

    An unrecognised verb is OUTWARD. That is the safe direction and it is
    not a close call: being wrong about a read costs one approval click,
    and being wrong about a write costs a published post that the server
    in question has no tool to delete.
    """
    name = str(tool_name or "")
    if not name.startswith("mcp__"):
        return "read"          # not this gate's business
    rest = name[len("mcp__"):]
    # `mcp__<server>__<tool>`; anything shorter is malformed, so hold it.
    # So is a tool part that begins with `_`: `mcp__jarvis___post` splits
    # as JARVIS's own `_post` or as server `jarvis_`'s `post`, and a name
    # that means two things is held rather than guessed at.
    server, sep, tool = rest.partition("__")
    if not sep or not tool or tool.startswith("_"):
        return "outward"
    if name.startswith(OWN_PREFIX):
        return "read"          # JARVIS's own: gated at /internal/tool
    words = _words(tool)
    # A server may namespace its own tools with its own name —
    # `paperclipListIssues`, `paperclipGetIssue`. That prefix is not the verb.
    if words and server and words[0] == server.lower():
        words = words[1:]
    # Every position a VERB can occupy: the front, and after a conjunction.
    # Everything else in the name is a noun and says nothing about what the
    # tool does — `get_post_comments` reads comments on a post.
    verbs = [words[0]] if words else []
    for index, word in enumerate(words):
        if word in CONJUNCTIONS:
            if index + 1 >= len(words):
                return "outward"      # trailing conjunction: malformed, hold it
            verbs.append(words[index + 1])
    if not verbs:
        return "outward"
    return "read" if all(verb in READ_VERBS for verb in verbs) else "outward"


# A tool's OWN name, before the Claude CLI spells it (`claude_env.
# mcp_tool_name`: every symbol becomes `_`). A symbol that joins two verbs —
# `get&delete`, `search+reply` — is read as the conjunction it stands for;
# any other — `.`, `/`, `:`, a space — separates words, as the CLI's
# spelling does: `issues.list` is one verb, as it always was.
_JOINING_SYMBOLS = re.compile(r"\s*[&+|,;]+\s*")
_SEPARATING_SYMBOLS = re.compile(r"[^A-Za-z0-9_-]+")


def own_spelling(tool: str) -> str:
    """A tool's own name as words `classify` reads, conjunctions kept."""
    return _SEPARATING_SYMBOLS.sub("_", _JOINING_SYMBOLS.sub("_and_", tool))


def classify_call(cli_name: str, server: str = "", own_tool: Optional[str] = None) -> str:
    """`classify` of the CLI's spelling, and — when the tool's own name is
    known (the fallback's gateway sends it) — of that too: outward when
    either is. `server` is the name's server part as the CLI wrote it."""
    kinds = {classify(cli_name)}
    if isinstance(own_tool, str) and server:
        kinds.add(classify(f"mcp__{server}__{own_spelling(own_tool)}"))
    return "read" if kinds == {"read"} else "outward"


def needs_own_spelling(cli_name: str) -> bool:
    """Whether the CLI's spelling may have replaced a symbol in a user
    server's tool name: a `_` in the tool's part, which might have been
    `_`, `&`, `+`, `.` or a space. Without one the spelling IS the tool's
    own. JARVIS's own tools, and anything not `mcp__`, are not this gate's
    reading."""
    name = str(cli_name or "")
    if not name.startswith("mcp__") or name.startswith(OWN_PREFIX):
        return False
    _server, sep, tool = name[len("mcp__"):].partition("__")
    return bool(sep) and "_" in tool


def classify_hook_call(cli_name: str, server: str, own_names, read_only: bool = False) -> str:
    """The Claude path's verdict. Its hook sends only the CLI's spelling;
    `own_names` is every name the connectors said they call a tool the CLI
    spells `cli_name` (the brain asks the CLI, `Brain.own_tool_names`), or
    None when no process has said. Each is read as the gateway's own name
    is (`classify_call`), and one that acts holds the call: the hook cannot
    say which of two tools spelled alike was called. A spelling that may
    have lost a symbol, with no own name to read, is held.

    `read_only` is the connector's own word, from the same `mcp_status`
    answer as its names: every live process reported the tool with
    `readOnlyHint` (`Brain.own_tools_read_only`). See `frees_read_only`."""
    if own_names:
        kinds = {classify_call(cli_name, server, own) for own in own_names}
        if kinds == {"read"}:
            return "read"
        if read_only and frees_read_only(cli_name, server, own_names):
            return "read"
        return "outward"
    if needs_own_spelling(cli_name):
        return "outward"
    return classify(cli_name)


# Verbs that reach other people or change something, whatever a server says
# about the tool. `readOnlyHint` never frees a tool whose name LEADS with one
# of these, so a server that mislabels `send_message` still asks.
ACT_VERBS = frozenset({
    "send", "post", "create", "publish", "delete", "remove", "update", "edit",
    "write", "comment", "reply", "connect", "invite", "share", "repost", "like",
    "react", "follow", "unfollow", "message", "submit", "pay", "transfer", "buy",
    "order", "book", "schedule", "cancel", "approve", "reject", "merge", "push",
    "deploy", "upload", "add", "set", "put", "move", "archive", "close", "assign",
    "accept", "decline", "forward", "email", "mail", "call", "text", "tweet",
    "rename", "save", "store", "insert", "modify", "patch", "run", "execute",
    "trigger", "start", "stop", "kill", "restart", "install", "grant", "revoke",
})

# The few that are verbs wherever they stand. `post` and `comment` are not
# among them: `resolve_post_url` finds a post, and `get_post_comments` reads
# them, as `classify` already knows.
ALWAYS_ACT = frozenset({
    "send", "publish", "delete", "remove", "pay", "transfer", "buy", "purchase",
    "invite", "repost", "unfollow", "tweet", "forward", "deploy", "merge",
    "execute", "grant", "revoke", "drop", "wipe", "erase", "destroy",
})


def frees_read_only(cli_name: str, server: str, own_names) -> bool:
    """Whether the connector's own `readOnlyHint` may let a call through that
    its name alone would hold — `resolve_post_url`: `resolve` is not a read
    verb, and the connector declares the tool read-only.

    The gate guards against the MODEL, which can be talked into anything; the
    hint comes from the server the user installed, through the CLI, and the
    model cannot write it. A server that lied about its own tool could act
    without asking whatever this gate decided. What stays held is a NAME
    that says it acts, in the CLI's spelling or any own name: one that leads
    with an acting verb, has an always-acting word anywhere, or joins two
    verbs with a conjunction."""
    if not own_names:
        return False
    spellings = [cli_name] + [f"mcp__{server}__{own_spelling(own)}" for own in own_names]
    for spelling in spellings:
        _server, sep, tool = spelling[len("mcp__"):].partition("__")
        if not sep or not tool or tool.startswith("_"):
            return False
        words = _words(tool)
        if words and server and words[0] == server.lower():
            words = words[1:]
        if not words or words[0] in ACT_VERBS:
            return False
        if any(word in CONJUNCTIONS or word in ALWAYS_ACT for word in words):
            return False
    return True


def digest_for(tool_name: str, tool_input) -> str:
    """What the user is approving: this tool, with these arguments.

    Key order is not part of it — the brain re-emits the same call to retry
    and a dict is not ordered in any way the user chose. Everything else
    is: approving one wording must not approve another, and approving a
    post must not approve a message.

    Never raises. A hook that failed on an odd argument would fail OPEN,
    which is the one outcome this whole module exists to prevent.
    """
    try:
        encoded = json.dumps(tool_input, sort_keys=True, default=repr,
                             ensure_ascii=False, separators=(",", ":"))
    except Exception:
        encoded = repr(tool_input)
    return hashlib.sha256(f"{tool_name}\n{encoded}".encode("utf-8", "replace")).hexdigest()
