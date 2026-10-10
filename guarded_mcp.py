"""The door between the fallback brain and a user's own MCP server.

When Claude's usage limit is hit, JARVIS's brain falls back to the OpenAI
Codex CLI (`chatgpt_fallback.py`). Codex is given no tools of its own, only
MCP servers, and for each server the user declared in `connections.json` it
is handed THIS instead of the server itself:

    python guarded_mcp.py --config <mcp.json> --server <name>
        --gate-url <JARVIS origin> --token-file <path> --nonce <str>

Codex starts it as a stdio MCP server. It starts (or dials) the user's real
server and relays JSON-RPC between the two.

Why a gateway at all: on the Claude path the PreToolUse hook is the one
place JARVIS can stop a user's server acting (CLAUDE.md, "The gate on a
user's own MCP servers"). Codex has no such hook, so the gate has to sit ON
THE WIRE. Every `tools/call` is put to the same `/internal/pretool` route
the hook uses, and only an explicit "allow" lets it through. Everything here
fails closed, for the reason `pretool_hook.py` does: a gate that waves calls
through when JARVIS is down gives exactly the wrong answer at exactly the
wrong moment.

A relay that gated `tools/call` and passed everything else would still have
doors in it. Each rule below closes one that a security review found:

  * Six methods pass from Codex to the server, no more. `resources/read`,
    `prompts/get` and the rest reach the user's service without a tool call
    and so without the gate, and a server can do anything in a handler.
  * The server may not ask Codex anything. `sampling/createMessage` would
    let the user's server put words to the brain; `elicitation/create` and
    `roots/list` are the same door. The gateway answers them itself, and the
    capabilities that invite them are struck from both halves of the
    handshake.
  * What is forwarded is re-serialised from the object that was checked,
    never the line that arrived. `{"params": A, "params": B}` parses as B
    here; a server whose parser keeps the FIRST would run A, which nobody
    approved.
  * A call Codex cancelled while its gate was pending is dropped, not sent
    late: Codex has stopped listening, and the brain believes nothing ran.
  * The connector gets `claude_env.child_env()` plus its own `env`, never
    this process's raw environment, and its stderr is read and thrown away:
    a server's log can hold the server's own credentials.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import claude_env

# How long one gated call may wait for JARVIS's answer. Longer than the
# server's own wait for the user (`server.GATE_APPROVAL_WAIT_SEC`, 120s), so
# the innermost budget gives up first and a hang is blamed on what hung; the
# same nesting `pretool_hook.TIMEOUT_SEC` keeps on the Claude path.
GATE_TIMEOUT_SEC = 155.0

# After Codex closes our stdin, how long calls that were ALREADY forwarded
# may take to answer. Short: Codex is shutting down and is not going to wait
# long either, and a gate still pending is abandoned rather than waited on.
EOF_GRACE_SEC = 5.0

# A stdio connector is asked to leave by closing its stdin (what the MCP spec
# says a client does), then terminated, then killed.
CONNECTOR_EXIT_SEC = 2.0
CONNECTOR_KILL_SEC = 3.0

# A remote connector's own budget. A real LinkedIn call was measured at up
# to 180s by that server's own reckoning; the connect half is short because
# a server that cannot be reached in ten seconds is not there.
HTTP_TIMEOUT_SEC = 300.0
HTTP_CONNECT_SEC = 10.0

# The gate's answer is a few hundred bytes. Anything a thousand times that is
# not the gate, and is not read.
GATE_ANSWER_LIMIT = 1 << 20
REASON_LIMIT = 2000

# `jarvis` is JARVIS's own server, already gated at `/internal/tool`;
# gating it here would deadlock. The name rule is the one
# `server.declared_connections` applies, and `__` is refused for the same
# reason there: `mcp__<server>__<tool>` must split back into the server that
# was asked about, or the gate classifies a verb out of the server's name.
# `fullmatch`, not `match`: `$` matches before a trailing newline.
RESERVED_SERVER_NAME = "jarvis"
_SERVER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# The bearer token goes to the gate on every call, over a TLS connection
# whose certificate is not checked. Anywhere but this machine would hand the
# token to whoever answered.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

CLIENT_METHODS = frozenset({
    "initialize", "notifications/initialized", "notifications/cancelled",
    "ping", "tools/list", "tools/call",
})
CLIENT_CAPABILITIES_STRUCK = ("sampling", "elicitation", "roots")
# An allowlist, and stricter than a list of what to strike: `resources`,
# `prompts`, `completions` and `logging` all go, and so does anything a
# future protocol adds, until somebody decides it is safe.
SERVER_CAPABILITIES_KEPT = ("tools",)
SERVER_NOTIFICATIONS_RELAYED = frozenset({"notifications/tools/list_changed"})

STDIO_TYPES = frozenset({"stdio"})
HTTP_TYPES = frozenset({"http", "streamable-http"})

INVALID_REQUEST = -32600
NOT_AVAILABLE = -32601
CONNECTOR_FAILED = -32000
NOT_AVAILABLE_TEXT = "Not available through JARVIS"
BATCH_TEXT = "Batches are not accepted through JARVIS"
BAD_ID_TEXT = "A request id must be a string or an integer"

# What the brain is told. JARVIS's own words, never a connector's or an
# exception's: an exception's text can quote the arguments it choked on.
GATE_SILENT = "My gate did not answer, sir, so I have not let that run."
GATE_REFUSED = "My gate did not approve that, sir, so I have not let it run."
MALFORMED_CALL = "That tool call was malformed, sir, so I have not sent it."
CONNECTOR_GONE = "The connected service has stopped, sir; that was not sent."
CONNECTOR_UNREACHABLE = "The connected service could not be reached, sir; that was not sent."
CONNECTOR_SILENT = ("The connected service did not answer, sir, so I cannot say "
                    "whether that went through.")
CONNECTOR_REFUSED = ("The connected service answered with an error, sir, so I cannot "
                     "say whether that went through.")
CONNECTOR_GARBLED = "The connected service answered with something unreadable, sir."

_ABSENT = object()        # a key that was not there, as opposed to a JSON null
_UNREADABLE = object()    # a line that was not JSON


class Refused(Exception):
    """A configuration the gateway will not serve. Exit 2, before anything
    starts: a server that is half-served is worse than one that is absent,
    because it looks like it works."""


class ConnectorFailure(Exception):
    """The connector could not take or answer a message. `text` is one of the
    sentences above, never the underlying exception's own words."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def _say(text: str) -> None:
    """A line for whoever reads Codex's MCP log. Never stdout: that is the
    protocol, and one stray line there is a corrupt message."""
    try:
        print(text, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


# --- the wire ------------------------------------------------------------

def _no_constant(name):
    """`NaN` and `Infinity` are not JSON. Python accepts them by default;
    accepting them here would hand the connector a value the gate's own
    canonical form (`allow_nan=False`) cannot express."""
    raise ValueError(name)


def _finite(text: str) -> float:
    """A JSON number as a float, refusing one too big to be finite: `1e400`
    is not `NaN`'s spelling, but it parses to the same unrepresentable
    value."""
    value = float(text)
    if math.isinf(value) or math.isnan(value):
        raise ValueError(text)
    return value


def _parse(raw):
    """One message, or `_UNREADABLE`. Strict UTF-8 and strict JSON: a line
    that is neither is never guessed at."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text, parse_constant=_no_constant, parse_float=_finite)
    except (ValueError, TypeError, RecursionError):
        return _UNREADABLE


# The id of a message whose line would not parse — a number too long for
# Python's integer limit, a float too big to be finite, bytes that are not
# UTF-8. Only ever used to ANSWER it: Codex waits on an id until its own
# timeout, and a request nobody answers holds the whole turn open that long.
# A string id or a plain integer; anything cleverer is left unanswered.
_SALVAGED_ID_VALUE_RE = re.compile(r'\s*("(?:[^"\\\x00-\x1f]){1,128}"|-?\d{1,18})\s*[,}]')


def _salvaged_id(raw, *, answer: bool = False):
    """The `id` of the OUTERMOST object on an unreadable line, or `_ABSENT`.

    Only a key at the top level: an `"id"` nested in a result (the MCP
    TypeScript SDK writes `result` before `id`) belongs to something else,
    and settling it would fail another call and leave this one hanging.
    `answer`: the line is the server's, read as an answer — one with a
    top-level `"method"` is its own request or notification instead, whose
    id is the server's, not Codex's. Two top-level ids is no id at all. The
    line is decoded leniently: only the id is taken from it, never its
    content."""
    if isinstance(raw, (bytes, bytearray)):
        text = bytes(raw).decode("utf-8", "replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return _ABSENT
    text = text.lstrip()
    if not text.startswith("{"):
        return _ABSENT
    found, depth, key_next, i = _ABSENT, 0, False, 0
    while i < len(text):
        c = text[i]
        if c == '"':
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                break                               # the line ends inside a string
            if depth == 1 and key_next:
                key, colon = text[i + 1:end], end + 1
                while colon < len(text) and text[colon] in " \t\r\n":
                    colon += 1
                if colon >= len(text) or text[colon] != ":":
                    return _ABSENT
                if key == "method" and answer:
                    return _ABSENT
                if key == "id":
                    match = _SALVAGED_ID_VALUE_RE.match(text, colon + 1)
                    if found is not _ABSENT or match is None:
                        return _ABSENT
                    try:
                        found = json.loads(match.group(1))
                    except ValueError:
                        return _ABSENT
                key_next, i = False, colon + 1
                continue
            i = end + 1
            continue
        if c in "{[":
            depth += 1
            key_next = c == "{" and depth == 1
        elif c in "}]":
            depth -= 1
        elif c == "," and depth == 1:
            key_next = True
        i += 1
    return found


def _encode(message) -> bytes:
    """Exactly one JSON object and one newline. ASCII-only, so no separator
    `str.splitlines()` knows can appear raw and a lone surrogate out of a
    connector's reply still encodes."""
    return (json.dumps(message, ensure_ascii=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("ascii")


def _id_key(value):
    """A request id as a dictionary key, or None if it is not a usable id.

    Typed, because `True == 1` in Python and JSON `true` is not an id: a
    cancellation of request `1` must not cancel a request someone numbered
    `true`, and the other way round."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return ("n", value)
    if isinstance(value, str):
        return ("s", value)
    return None


def _outgoing(method, rid=_ABSENT, params=_ABSENT) -> dict:
    """A message built from parsed values, so nothing that arrived alongside
    them — a second `method`, an extra key — travels on."""
    message = {"jsonrpc": "2.0"}
    if rid is not _ABSENT:
        message["id"] = rid
    message["method"] = method
    if params is not _ABSENT:
        message["params"] = params
    return message


def _rpc_error(rid, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}}


def _tool_error(rid, text: str) -> dict:
    """A refused call, in the shape the brain reads as a failed tool rather
    than a broken server: it can say so and carry on."""
    return {"jsonrpc": "2.0", "id": rid, "result": {
        "content": [{"type": "text", "text": text}], "isError": True}}


def _checked_call(params):
    """(name, arguments, _meta) for a well-formed `tools/call`, else None.

    `arguments` absent means {}. Present and anything but an object — a
    list, a string, null — is refused rather than coerced: `/internal/pretool`
    turns a non-object into {}, and a gate that approved {} must not release
    a call whose arguments are something else."""
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return None
    meta = params.get("_meta", _ABSENT)
    if meta is not _ABSENT and not isinstance(meta, dict):
        return None
    return name, arguments, meta


def _client_capabilities_struck(params: dict) -> dict:
    """Codex's `initialize` without the capabilities that invite the server
    to ask the client for something. Those requests are refused anyway; not
    advertising them means a well-behaved server never tries."""
    params = dict(params)
    capabilities = params.get("capabilities")
    if isinstance(capabilities, dict):
        params["capabilities"] = {k: v for k, v in capabilities.items()
                                  if k not in CLIENT_CAPABILITIES_STRUCK}
    return params


def _server_capabilities_kept(result):
    """The server's `initialize` result with only the capabilities Codex may
    use through JARVIS. Advertising `resources` or `prompts` would have Codex
    offer the brain methods the gateway then refuses."""
    if not isinstance(result, dict):
        return result
    result = dict(result)
    capabilities = result.get("capabilities")
    result["capabilities"] = {k: capabilities[k] for k in SERVER_CAPABILITIES_KEPT
                              if isinstance(capabilities, dict) and k in capabilities}
    return result


def _header_safe(value: str) -> bool:
    """Visible ASCII only, as the MCP spec requires of a session id: a value
    a server chose must not be able to add a header of its own."""
    return 0 < len(value) <= 256 and all(0x21 <= ord(c) <= 0x7E for c in value)


# --- configuration ---------------------------------------------------------

def gate_url_problem(url: str) -> str | None:
    """Why `url` is not a loopback origin of JARVIS's, or None if it is.

    Checked before the token file is opened. The token is sent to this URL
    on every call, over TLS that is not verified (JARVIS's certificate is
    self-signed), so a URL anywhere else would hand the token to whoever
    answered it."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port                     # raises on a port that is not one
    except (ValueError, TypeError):
        return "guarded_mcp: --gate-url is not a URL."
    if parts.scheme not in ("http", "https"):
        return "guarded_mcp: --gate-url must be http or https."
    if host not in LOOPBACK_HOSTS:
        return "guarded_mcp: --gate-url must be on this machine (127.0.0.1, ::1 or localhost)."
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        return "guarded_mcp: --gate-url must be a plain origin."
    return None


def _usable_server_name(name) -> bool:
    """`server.declared_connections`' rule, judged on the name as the CLI
    writes it (`claude_env.server_name_problem`): `jarvis_` or `jarvis.`
    would make every call read as one of JARVIS's own tools."""
    return (isinstance(name, str) and bool(_SERVER_NAME_RE.fullmatch(name))
            and name != RESERVED_SERVER_NAME
            and claude_env.server_name_problem(name) is None)


def _strings(value, *, allow_empty_values: bool = True) -> bool:
    """A mapping of names to strings, none of which can end a header or an
    environment entry early."""
    if not isinstance(value, dict):
        return False
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k:
            return False
        if any(c in k for c in "=\0\r\n") or any(c in v for c in "\0\r\n"):
            return False
        if not allow_empty_values and not v:
            return False
    return True


def _stdio_spec(entry: dict) -> dict:
    command = entry.get("command")
    if not isinstance(command, str) or not command or "\0" in command:
        raise Refused("a stdio server needs a \"command\".")
    args = entry.get("args")
    args = [] if args is None else args
    if not isinstance(args, list) or not all(isinstance(a, str) and "\0" not in a for a in args):
        raise Refused("\"args\" must be a list of strings.")
    env = entry.get("env")
    env = {} if env is None else env
    if not _strings(env):
        raise Refused("\"env\" must map names to strings.")
    cwd = entry.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd or "\0" in cwd):
        raise Refused("\"cwd\" must be a directory path.")
    return {"kind": "stdio", "command": command, "args": list(args),
            "env": dict(env), "cwd": cwd}


def _http_spec(entry: dict) -> dict:
    url = entry.get("url")
    try:
        parts = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        usable = bool(parts and parts.scheme in ("http", "https") and parts.hostname)
    except ValueError:
        usable = False
    if not usable:
        raise Refused("an HTTP server needs an http or https \"url\".")
    headers = entry.get("headers")
    headers = {} if headers is None else headers
    if not _strings(headers):
        raise Refused("\"headers\" must map names to strings.")
    return {"kind": "http", "url": url, "headers": dict(headers)}


def load_entry(config: Path, server: str) -> dict:
    """The one server this gateway serves, validated, or `Refused`.

    stdio (`command`, `args`, `env`, `cwd`) or streamable HTTP (`url`,
    `headers`). `type` may say which; absent, the keys do. SSE and anything
    else is refused outright rather than served partly: the legacy SSE
    transport would need a second, long-lived channel this gateway does not
    gate, and an unknown type is a transport nobody has looked at."""
    if not _usable_server_name(server):
        raise Refused("that server name is not one I serve.")
    try:
        body = json.loads(Path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused("the MCP config could not be read.") from None
    servers = body.get("mcpServers") if isinstance(body, dict) else None
    if not isinstance(servers, dict) or server not in servers:
        raise Refused("the MCP config does not declare that server.")
    entry = servers[server]
    if not isinstance(entry, dict):
        raise Refused("that server's entry is not an object.")
    kind = entry.get("type", _ABSENT)
    if kind is _ABSENT:
        has_command, has_url = "command" in entry, "url" in entry
        if has_command == has_url:
            raise Refused("that server needs exactly one of \"command\" or \"url\".")
        kind = "stdio" if has_command else "http"
    if not isinstance(kind, str):
        raise Refused("that server's \"type\" is not stdio or streamable HTTP, so I do not relay it.")
    if kind in STDIO_TYPES:
        return _stdio_spec(entry)
    if kind in HTTP_TYPES:
        return _http_spec(entry)
    if kind == "sse":
        raise Refused("that server uses the SSE transport, which I do not relay; "
                      "only stdio and streamable HTTP are served.")
    raise Refused("that server's \"type\" is not stdio or streamable HTTP, so I do not relay it.")


def connector_env(own: dict) -> dict:
    """What a stdio connector runs with.

    `claude_env.child_env` first: this process was started by Codex, which
    was started by JARVIS, and whatever JARVIS's launcher handed down (an
    `ANTHROPIC_*` key, a Claude Code session's `CLAUDE_*`, JARVIS's phone
    tokens) is no business of a user's server. Then the entry's own `env`,
    which is how CLAUDE.md tells a user to give a server a variable."""
    env = claude_env.child_env(dict(os.environ))
    env.update(own)
    return env


# --- the gate --------------------------------------------------------------

def _gate_tls() -> ssl.SSLContext:
    """Unverified, exactly as `pretool_hook.py`: the certificate is JARVIS's
    own self-signed one on loopback, and the bearer token is what
    authenticates the call. Used for this one call only; a remote connector
    is verified."""
    return ssl._create_unverified_context()


def gated_tool_name(server: str, name: str) -> str:
    """The name the Claude CLI gives the same tool (`claude_env.
    mcp_tool_name`), so one policy (`pretool_gate.classify`) and one
    approval queue cover both brains. The connector is still sent the raw
    name; only the gate sees this one."""
    return claude_env.mcp_tool_name(server, name)


class Gate:
    """JARVIS's approval gate, asked over loopback the way the hook asks it.

    Callable: `(name, arguments) -> (allowed, reason)`. Never raises. Only
    an answer that says `allow` in so many words allows; a deny, an `ask`, a
    status that is not 200 (a redirect included), a body that is not the
    hook's shape, a timeout, a missing token, anything at all that goes
    wrong, is a refusal.

    `http.client` rather than urllib, for what it does NOT do. urllib sends a
    loopback request through HTTP_PROXY when NO_PROXY does not name it, and
    follows a redirect with the Authorization header still on; either would
    hand the bearer token to somebody else. `http.client` dials the address
    it is given and reports what came back."""

    def __init__(self, base_url: str, token_file: str, server: str, nonce: str,
                 timeout: float = GATE_TIMEOUT_SEC):
        endpoint = urllib.parse.urlsplit(base_url.rstrip("/") + "/internal/pretool")
        self._https = endpoint.scheme == "https"
        self._host = endpoint.hostname
        self._port = endpoint.port
        self._path = endpoint.path
        self.token_file = token_file
        self.server = server
        self.nonce = nonce
        self.timeout = timeout

    def __call__(self, name: str, arguments: dict) -> tuple[bool, str]:
        try:
            return self._ask(name, arguments)
        except Exception:
            return False, GATE_SILENT

    def _ask(self, name: str, arguments: dict) -> tuple[bool, str]:
        # Read now, not at start: JARVIS may have rotated it since.
        token = Path(self.token_file).read_text(encoding="utf-8").strip()
        if not token:
            return False, GATE_SILENT
        body = json.dumps({
            "tool_name": gated_tool_name(self.server, name),
            "tool_input": arguments,
            # Fresh per call: the Claude CLI's ids are unique per tool use,
            # and the gate's record keys on it.
            "tool_use_id": uuid.uuid4().hex,
            "fallback_nonce": self.nonce,
            # The tool's own name too, before the CLI's spelling turned a
            # `&` or a `/` into `_`: the gate reads `get&delete` as two verbs.
            "raw_tool_name": name,
        }, allow_nan=False).encode("utf-8")
        if self._https:
            connection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self.timeout, context=_gate_tls())
        else:
            connection = http.client.HTTPConnection(self._host, self._port,
                                                    timeout=self.timeout)
        try:
            connection.request("POST", self._path, body=body, headers={
                "Authorization": "Bearer " + token, "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                return False, GATE_SILENT
            raw = response.read(GATE_ANSWER_LIMIT + 1)
        finally:
            connection.close()
        if len(raw) > GATE_ANSWER_LIMIT:
            return False, GATE_SILENT
        answer = _parse(raw)
        verdict = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
        if not isinstance(verdict, dict):
            return False, GATE_SILENT
        decision = verdict.get("permissionDecision")
        if decision == "allow":
            return True, ""
        reason = verdict.get("permissionDecisionReason")
        if decision == "deny" and isinstance(reason, str) and reason.strip():
            # The route's own sentence (it walls what it interpolates), so
            # the brain hears why: declined, already used, waiting.
            return False, reason[:REASON_LIMIT]
        return False, GATE_REFUSED


# --- the gateway -----------------------------------------------------------

class _Pending:
    """A request forwarded to the connector and not yet answered.

    `sent` is False for a gated call between the gate's yes and the write
    to the connector; a cancellation in that gap stops the write
    (`cancelled`) instead of racing it to the server."""
    __slots__ = ("rid", "key", "method", "sent", "cancelled")

    def __init__(self, rid, key, method: str, *, sent: bool = True):
        self.rid = rid
        self.key = key
        self.method = method
        self.sent = sent
        self.cancelled = False


class _GatedCall:
    """A `tools/call` waiting on the gate. `cancelled` is set, under the
    gateway's lock, by a `notifications/cancelled` naming it."""
    __slots__ = ("rid", "key", "name", "arguments", "meta", "cancelled")

    def __init__(self, rid, key, name: str, arguments: dict, meta):
        self.rid = rid
        self.key = key
        self.name = name
        self.arguments = arguments
        self.meta = meta
        self.cancelled = False


class Gateway:
    """Both directions of the relay, and every rule the module docstring
    lists. Transport-agnostic: a connector has `send`, `close`, `start`,
    `initialized` and says whether `send` blocks until the answer is in."""

    def __init__(self, *, gate, out, eof_grace: float = EOF_GRACE_SEC):
        self.gate = gate
        self.connector = None
        self._out = out
        self._out_lock = threading.Lock()
        self._state = threading.Condition()
        self._outstanding: dict = {}
        self._gating: dict = {}
        self._closing = False
        self._client_gone = False
        self._stop = threading.Event()
        self._eof_grace = eof_grace

    # -- to Codex --

    def emit(self, message: dict) -> bool:
        """One message to Codex, whole, under a lock: two threads answering
        at once must not interleave half-lines."""
        try:
            data = _encode(message)
        except (TypeError, ValueError, RecursionError):
            if "id" not in message:
                return False
            data = _encode(_rpc_error(message["id"], CONNECTOR_FAILED, CONNECTOR_GARBLED))
        with self._out_lock:
            if self._client_gone:
                return False
            try:
                self._out.write(data)
                self._out.flush()
                return True
            except (OSError, ValueError):
                self._client_gone = True
        # Codex has stopped reading: nothing more is forwarded (`_admit`),
        # and nothing waits for answers nobody will receive.
        with self._state:
            self._state.notify_all()
        self._stop.set()
        return False

    # -- from Codex --

    def from_client(self, raw) -> None:
        """One line from Codex: refused, gated, or forwarded as rebuilt."""
        if self._client_gone:
            return          # nobody to answer; do not even stage a card
        message = _parse(raw)
        if message is _UNREADABLE:
            # Answered when it can be, so Codex does not wait out its own
            # timeout on a request nothing will ever answer.
            rid = _salvaged_id(raw)
            if rid is not _ABSENT:
                self.emit(_rpc_error(rid, INVALID_REQUEST, MALFORMED_CALL))
            return
        if isinstance(message, list):
            # A batch would carry a `tools/call` past a check written for
            # one message at a time. MCP no longer has them; refuse whole.
            self.emit(_rpc_error(None, INVALID_REQUEST, BATCH_TEXT))
            return
        if not isinstance(message, dict) or "method" not in message:
            return          # not JSON, or an answer to a request never relayed
        method = message["method"]
        has_id = "id" in message
        key = _id_key(message["id"]) if has_id else None
        if not isinstance(method, str) or method not in CLIENT_METHODS:
            if has_id:
                self.emit(_rpc_error(message["id"] if key else None, NOT_AVAILABLE,
                                     NOT_AVAILABLE_TEXT))
            return
        if has_id and key is None:
            self.emit(_rpc_error(None, INVALID_REQUEST, BAD_ID_TEXT))
            return
        rid = message["id"] if has_id else _ABSENT
        params = message.get("params", _ABSENT)
        if method == "tools/call":
            self._gate_then_forward(rid, key, params)
        elif method == "notifications/cancelled":
            self._cancel(params)
        else:
            if method == "initialize" and isinstance(params, dict):
                params = _client_capabilities_struck(params)
            pending = _Pending(rid, key, method) if key is not None else None
            self._forward_soon(_outgoing(method, rid, params), pending)

    def _gate_then_forward(self, rid, key, params) -> None:
        checked = _checked_call(params)
        if checked is None:
            if key is not None:
                self.emit(_tool_error(rid, MALFORMED_CALL))
            return
        name, arguments, meta = checked
        call = _GatedCall(rid, key, name, arguments, meta)
        if key is not None:
            with self._state:
                self._gating[key] = call
        # Its own thread: the gate may hold this call for two minutes while
        # the user decides, and the stdin loop must keep reading meanwhile —
        # a cancellation of this very call arrives on it.
        threading.Thread(target=self._decide, args=(call,), daemon=True).start()

    def _decide(self, call: _GatedCall) -> None:
        try:
            allowed, reason = self.gate(call.name, call.arguments)
        except Exception:
            allowed, reason = False, GATE_SILENT
        pending = (_Pending(call.rid, call.key, "tools/call", sent=False)
                   if call.key is not None else None)
        with self._state:
            if call.key is not None and self._gating.get(call.key) is call:
                del self._gating[call.key]
            # Cancelled, or Codex gone: answer nothing and send nothing.
            # Codex gave up on it; a late send would act on a call the
            # brain has already been told did not happen.
            if call.cancelled or self._closing or self._client_gone:
                return
            if allowed and pending is not None:
                self._outstanding[call.key] = pending
        if not allowed:
            if pending is not None:
                self.emit(_tool_error(call.rid, reason))
            return          # a refused notification is answered with silence
        # Rebuilt from the very objects the gate was shown. Never the line
        # that arrived: see the module docstring on duplicate keys.
        params = {"name": call.name, "arguments": call.arguments}
        if call.meta is not _ABSENT:
            params["_meta"] = call.meta
        self._send(_outgoing("tools/call", call.rid, params), pending,
                   before=self._still_wanted(pending))

    def _still_wanted(self, pending):
        """Asked by the connector as it is about to write an allowed call
        (under its write lock, for stdio). No: it was cancelled meanwhile,
        and is not sent. Yes: from now on a cancellation of it is forwarded,
        and cannot overtake it on the same pipe."""
        if pending is None:
            return None

        def check() -> bool:
            with self._state:
                if pending.cancelled or self._outstanding.get(pending.key) is not pending:
                    return False
                pending.sent = True
                return True
        return check

    def _cancel(self, params) -> None:
        """Codex has given up on a request. If it is still at the gate it
        will never be forwarded; if the connector has it, the connector is
        told, and its late answer is dropped."""
        if not isinstance(params, dict):
            return
        key = _id_key(params.get("requestId"))
        if key is None:
            return
        with self._state:
            call = self._gating.pop(key, None)
            if call is not None:
                call.cancelled = True
            pending = self._outstanding.pop(key, None)
            if pending is not None and not pending.sent:
                # Allowed but not yet written: it will not be. There is
                # nothing at the server to cancel.
                pending.cancelled = True
                pending = None
            self._state.notify_all()
        if pending is not None:
            notice = {"requestId": pending.rid}
            if isinstance(params.get("reason"), str):
                notice["reason"] = params["reason"]
            self._send(_outgoing("notifications/cancelled", params=notice), None)

    def _admit(self, pending) -> bool:
        with self._state:
            if self._closing or self._client_gone:
                return False
            if pending is not None:
                self._outstanding[pending.key] = pending
            return True

    def _forward_soon(self, message: dict, pending) -> None:
        """Forward from the stdin loop. A request to a connector whose `send`
        waits for the answer (HTTP) goes on its own thread, so one slow
        `tools/list` cannot stop a cancellation being read. Notifications go
        at once and in order: `notifications/initialized` must not overtake
        the `tools/list` that follows it."""
        if not self._admit(pending):
            return
        if pending is not None and self.connector.blocking:
            threading.Thread(target=self._send, args=(message, pending), daemon=True).start()
        else:
            self._send(message, pending)

    def _send(self, message: dict, pending, before=None) -> None:
        try:
            self.connector.send(message, done=(lambda: not self._is_outstanding(pending))
                                if pending is not None else None, before=before)
        except ConnectorFailure as failure:
            if pending is not None:
                self._fail(pending, failure.text)
            return
        except Exception:
            if pending is not None:
                self._fail(pending, CONNECTOR_SILENT)
            return
        if pending is not None and self.connector.blocking:
            # The exchange is over. An answer would have settled it.
            self._fail(pending, CONNECTOR_SILENT)

    def _is_outstanding(self, pending: _Pending) -> bool:
        with self._state:
            return self._outstanding.get(pending.key) is pending

    def _fail(self, pending: _Pending, text: str) -> None:
        """Answer a forwarded request with an error, unless it was answered."""
        with self._state:
            if self._outstanding.get(pending.key) is not pending:
                return
            del self._outstanding[pending.key]
            self._state.notify_all()
        self.emit(_rpc_error(pending.rid, CONNECTOR_FAILED, text))

    # -- from the connector --

    def from_server(self, message) -> None:
        if isinstance(message, list):
            for item in message:
                if isinstance(item, dict):
                    self._one_from_server(item)
        elif isinstance(message, dict):
            self._one_from_server(message)

    def unreadable_answer(self, raw) -> None:
        """An answer the gateway cannot read, for a request it forwarded:
        settle that request as garbled rather than let Codex wait on it."""
        key = _id_key(_salvaged_id(raw, answer=True))
        if key is None:
            return
        with self._state:
            pending = self._outstanding.get(key)
        if pending is not None:
            self._fail(pending, CONNECTOR_GARBLED)

    def _one_from_server(self, message: dict) -> None:
        """Relay answers to what was asked, and one notification; answer the
        server's own requests here; drop everything else.

        An answer is relayed only for a request that was forwarded and is
        still open: a server cannot answer a call on the gate's behalf, or
        answer the same call twice."""
        if "method" in message:
            if "id" in message:
                # The server asking Codex for something: a completion, the
                # user's input, the filesystem roots. Never Codex's to see.
                try:
                    self.connector.send(_rpc_error(message["id"], NOT_AVAILABLE,
                                                   NOT_AVAILABLE_TEXT), deliver=False)
                except Exception:
                    pass
            elif isinstance(message["method"], str) and \
                    message["method"] in SERVER_NOTIFICATIONS_RELAYED:
                self.emit(_outgoing(message["method"]))
            return
        if "id" not in message:
            return
        pending = self._settle(message["id"])
        if pending is None:
            return
        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict):
                error = {"code": CONNECTOR_FAILED, "message": CONNECTOR_GARBLED}
            self.emit({"jsonrpc": "2.0", "id": pending.rid, "error": error})
        elif "result" in message:
            result = message["result"]
            if pending.method == "initialize":
                result = _server_capabilities_kept(result)
                self.connector.initialized(result)
            self.emit({"jsonrpc": "2.0", "id": pending.rid, "result": result})
        else:
            self.emit(_rpc_error(pending.rid, CONNECTOR_FAILED, CONNECTOR_GARBLED))

    def _settle(self, server_id):
        key = _id_key(server_id)
        if key is None:
            return None
        with self._state:
            pending = self._outstanding.pop(key, None)
            self._state.notify_all()
        return pending

    def connector_stopped(self) -> None:
        """The connector exited. Everything it held gets an answer now rather
        than when Codex's own timeout expires."""
        with self._state:
            pending = list(self._outstanding.values())
            self._outstanding.clear()
            self._state.notify_all()
        for each in pending:
            self.emit(_rpc_error(each.rid, CONNECTOR_FAILED, CONNECTOR_GONE))

    # -- lifetime --

    def _read_client(self, stdin) -> None:
        try:
            for raw in iter(stdin.readline, b""):
                try:
                    self.from_client(raw)
                except Exception as e:           # pragma: no cover - defensive
                    _say(f"guarded_mcp: dropped a message ({type(e).__name__}).")
        except (OSError, ValueError):
            pass
        finally:
            self._stop.set()

    def run(self, stdin) -> int:
        """Relay until Codex closes our stdin (or stops reading our stdout),
        then give what was already forwarded a short grace to answer, and
        take the connector down. A call still at the gate is abandoned."""
        threading.Thread(target=self._read_client, args=(stdin,), daemon=True).start()
        self._stop.wait()
        deadline = time.monotonic() + self._eof_grace
        with self._state:
            self._closing = True
            while self._outstanding and not self._client_gone:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._state.wait(left)
        self.connector.close()
        return 0


# --- connectors ------------------------------------------------------------

def _resolve_command(command: str, env: dict) -> str:
    """A bare name found on the CONNECTOR's PATH, with PATHEXT on Windows,
    where `npx` is `npx.cmd` and CreateProcess will not find it by itself.
    A name with a directory in it is used as written."""
    if os.path.dirname(command):
        return command
    path = env.get("PATH") or env.get("Path")
    return shutil.which(command, path=path) or command


class StdioConnector:
    """The user's server as a child process speaking line-delimited JSON-RPC.

    Started in a process tree JARVIS owns (a kill-on-close job on Windows, a
    session on POSIX, as `process_tree` does for every other child): `npx`
    starts `node`, and killing the first alone leaves the second running."""

    blocking = False

    def __init__(self, spec: dict, gateway: Gateway):
        self._spec = spec
        self._gateway = gateway
        self._proc = None
        self._job = None
        self._write_lock = threading.Lock()
        self._dead = False

    def start(self) -> None:
        env = connector_env(self._spec["env"])
        argv = [_resolve_command(self._spec["command"], env), *self._spec["args"]]
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                  "stderr": subprocess.PIPE, "env": env, "cwd": self._spec["cwd"]}
        if os.name == "nt":
            import process_tree
            try:
                self._job = process_tree.Job()
            except OSError:
                raise ConnectorFailure(CONNECTOR_UNREACHABLE) from None
            kwargs["creationflags"] = 0x4          # CREATE_SUSPENDED until it is in the job
        else:
            kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except (OSError, ValueError):
            if self._job is not None:
                self._job.close()
            raise ConnectorFailure(CONNECTOR_UNREACHABLE) from None
        if self._job is not None:
            try:
                self._job.attach_and_resume(self._proc.pid)
            except OSError:
                self._proc.kill()
                self._job.close()
                raise ConnectorFailure(CONNECTOR_UNREACHABLE) from None
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def send(self, message: dict, done=None, deliver: bool = True, before=None) -> None:
        data = _encode(message)
        with self._write_lock:
            if self._dead:
                raise ConnectorFailure(CONNECTOR_GONE)
            # Under the write lock, so whatever is written after this —
            # a cancellation of this very call — is written after it too.
            if before is not None and not before():
                return
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (OSError, ValueError):
                self._dead = True
                raise ConnectorFailure(CONNECTOR_GONE) from None

    def initialized(self, result) -> None:
        pass

    def _pump_stdout(self) -> None:
        try:
            for raw in iter(self._proc.stdout.readline, b""):
                message = _parse(raw)
                if message is _UNREADABLE:
                    # A server logging to stdout is not a message — unless
                    # it answers a request with something unreadable (a
                    # 1e400, a NaN, bytes that are not UTF-8): then that
                    # request is answered as garbled, not left to time out.
                    self._gateway.unreadable_answer(raw)
                    continue
                try:
                    self._gateway.from_server(message)
                except Exception:              # pragma: no cover - defensive
                    pass
        except (OSError, ValueError):
            pass
        finally:
            self._dead = True
            self._gateway.connector_stopped()

    def _drain_stderr(self) -> None:
        """Read and discard. Unread, a chatty server blocks on a full pipe;
        relayed, a server's log (tokens, request dumps) reaches Codex's."""
        try:
            while self._proc.stderr.read(65536):
                pass
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        proc = self._proc
        self._dead = True
        # Not forever: a writer stuck on a full pipe holds this lock.
        if self._write_lock.acquire(timeout=0.5):
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            finally:
                self._write_lock.release()
        try:
            proc.wait(timeout=CONNECTOR_EXIT_SEC)
        except subprocess.TimeoutExpired:
            self._signal(force=False)
            try:
                proc.wait(timeout=CONNECTOR_KILL_SEC)
            except subprocess.TimeoutExpired:
                self._signal(force=True)
                try:
                    proc.wait(timeout=CONNECTOR_KILL_SEC)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            self._release()

    def _signal(self, *, force: bool) -> None:
        if os.name == "nt":
            try:
                if self._job is not None:
                    self._job.kill()           # the whole tree; Windows has no SIGTERM
                    return
            except OSError:
                pass
            try:
                self._proc.kill()
            except OSError:
                pass
        else:
            import signal
            try:
                os.killpg(self._proc.pid, signal.SIGKILL if force else signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def _release(self) -> None:
        """Whatever the server left behind goes with it."""
        if os.name == "nt":
            if self._job is not None:
                self._job.close()              # kill-on-close
                self._job = None
        else:
            import signal
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class HttpConnector:
    """The user's server over MCP streamable HTTP: one POST per message, the
    answer as JSON or as a server-sent event stream.

    TLS is verified and redirects are not followed: this is the user's own
    service, often across the internet, and the headers carry his
    credentials. Only the loopback gate call skips verification."""

    blocking = True
    # Ours to set; an entry's copy is dropped whatever its case.
    _TRANSPORT_HEADERS = frozenset({
        "accept", "content-type", "content-length", "host", "connection",
        "transfer-encoding", "mcp-session-id", "mcp-protocol-version"})

    def __init__(self, spec: dict, gateway: Gateway):
        import httpx
        self._httpx = httpx
        self._gateway = gateway
        self._url = spec["url"]
        self._headers = {k: v for k, v in spec["headers"].items()
                         if k.lower() not in self._TRANSPORT_HEADERS}
        self._session = None
        self._version = None
        self._client = httpx.Client(
            timeout=httpx.Timeout(HTTP_TIMEOUT_SEC, connect=HTTP_CONNECT_SEC),
            follow_redirects=False, verify=True)

    def start(self) -> None:
        pass

    def initialized(self, result) -> None:
        """Every later request says which protocol version was agreed, as
        the 2025-06-18 transport requires."""
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if isinstance(version, str) and _header_safe(version):
            self._version = version

    def _request_headers(self) -> dict:
        headers = dict(self._headers)
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self._version:
            headers["MCP-Protocol-Version"] = self._version
        return headers

    def send(self, message: dict, done=None, deliver: bool = True, before=None) -> None:
        # Separate requests carry no order over HTTP, so this can only stop
        # a call cancelled before it was posted; a server that sees the
        # cancellation first ignores it, as MCP says it may.
        if before is not None and not before():
            return
        httpx = self._httpx
        try:
            with self._client.stream("POST", self._url, content=_encode(message),
                                     headers=self._request_headers()) as response:
                session = response.headers.get("mcp-session-id")
                if session and _header_safe(session):
                    self._session = session
                if response.status_code == 202 or not deliver:
                    return
                if not 200 <= response.status_code < 300:
                    raise ConnectorFailure(CONNECTOR_REFUSED)
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind == "text/event-stream":
                    self._read_events(response, done)
                elif kind == "application/json":
                    self._deliver(response.read())
                else:
                    raise ConnectorFailure(CONNECTOR_GARBLED)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ConnectorFailure(CONNECTOR_UNREACHABLE) from None
        except httpx.HTTPError:
            raise ConnectorFailure(CONNECTOR_SILENT) from None

    def _read_events(self, response, done) -> None:
        """`data:` lines, joined per event, one message per event. Stops as
        soon as the request it was opened for has its answer: a server that
        keeps the stream open afterwards is not holding this thread."""
        data: list[str] = []
        for line in response.iter_lines():
            if line == "":
                if data:
                    self._deliver("\n".join(data))
                    data = []
                    if done is not None and done():
                        return
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "data":
                data.append(value)
        if data:
            self._deliver("\n".join(data))

    def _deliver(self, raw) -> None:
        """One message from the server, or — unreadable — the request it
        answers settled as garbled, as the stdio connector does, rather than
        left until the stream closes."""
        message = _parse(raw)
        if message is _UNREADABLE:
            self._gateway.unreadable_answer(raw)
        else:
            self._gateway.from_server(message)

    def close(self) -> None:
        """End the session politely (the spec's DELETE), then let go."""
        if self._session:
            try:
                self._client.delete(self._url, headers=self._request_headers(), timeout=2.0)
            except Exception:
                pass
        self._client.close()


# --- entry point -----------------------------------------------------------

def _arguments(argv):
    parser = argparse.ArgumentParser(
        prog="guarded_mcp.py",
        description="Relay one user-declared MCP server to Codex, every tool "
                    "call approved by JARVIS first.")
    parser.add_argument("--config", required=True, help="the brain's mcp.json")
    parser.add_argument("--server", required=True, help="which server in it")
    parser.add_argument("--gate-url", required=True,
                        help="JARVIS's loopback origin; /internal/pretool is appended")
    parser.add_argument("--token-file", required=True, help="the loopback bearer token")
    parser.add_argument("--nonce", required=True,
                        help="this fallback session's nonce, sent with every gate request")
    # For the tests: a budget measured in minutes makes a suite that takes
    # minutes.
    parser.add_argument("--gate-timeout", type=float, default=GATE_TIMEOUT_SEC,
                        help=argparse.SUPPRESS)
    parser.add_argument("--eof-grace", type=float, default=EOF_GRACE_SEC,
                        help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _arguments(argv)
    problem = gate_url_problem(args.gate_url)
    if problem is not None:
        _say(problem)
        return 2
    if not args.nonce.strip():
        _say("guarded_mcp: --nonce is empty.")
        return 2
    try:
        spec = load_entry(Path(args.config), args.server)
    except Refused as refusal:
        _say(f"guarded_mcp: refused: {refusal}")
        return 2
    gate = Gate(args.gate_url, args.token_file, args.server, args.nonce,
                timeout=args.gate_timeout)
    gateway = Gateway(gate=gate, out=sys.stdout.buffer, eof_grace=args.eof_grace)
    connector = (StdioConnector(spec, gateway) if spec["kind"] == "stdio"
                 else HttpConnector(spec, gateway))
    gateway.connector = connector            # before start: it may speak at once
    try:
        connector.start()
    except ConnectorFailure:
        _say("guarded_mcp: the connected server could not be started.")
        return 1
    return gateway.run(sys.stdin.buffer)


if __name__ == "__main__":
    code = main()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    # Not a normal exit: daemon threads may still be blocked reading a pipe,
    # and interpreter shutdown can stall on a stream one of them holds.
    os._exit(code)
