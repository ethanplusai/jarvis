"""JARVIS's second brain: Codex, on the owner's ChatGPT subscription, while
Claude's own usage limit holds.

The user asked for it in so many words: "change from claude to chatgpt when
claude limit reached then revert back when limit resets". `brain.py` routes a
user's turn here while Claude reports a blocking limit, and goes back to
Claude the first turn after the limit's reset time. This module is the Codex
half: finding the CLI, the home it runs in, the one command line it runs with,
whether it is ready, and one turn of it.

**The same gates, not new ones.** The fallback has no tool of its own. It
reaches the world through JARVIS's MCP server (`jarvis_mcp.py` ->
`/internal/tool`: origin, taint and generation gates) and, for the user's own
connections, through `guarded_mcp.py` -> `/internal/pretool` (the approval
card). Everything Codex would otherwise bring — a shell, a web search, image
generation, sub-agents, ChatGPT's own connectors, computer and browser use —
is switched off, and `readiness()` refuses to serve if a Codex update turns on
anything not vetted here (`KEEP_FEATURES`). Measured on codex-cli
0.155.0-alpha.9.2 against a fake Responses server: with these flags the model
is offered the three MCP resource readers (they only reach configured servers,
and the gateway refuses `resources/*`) and the `mcp__jarvis` namespace, and
nothing else. And what Codex actually does is watched, not only what it was
told: a turn that reports any item but an answer, reasoning or an MCP call,
or an MCP call answered by a server JARVIS did not give it, is killed, and
the fallback stays off until JARVIS restarts (`CodexSession.run`).

**Its own home.** `CODEX_HOME` is `<data_dir>/codex-home`, private to this
account and inside the directory `repo_read` refuses. The user signs Codex in
there once (`python scripts/chatgpt_setup.py login`); JARVIS never sees a
credential. Not the user's own `~/.codex`: that one injects its `AGENTS.md`,
skills and plugins into every prompt, and would keep JARVIS's threads in Codex
Desktop's history.

**Opt-in.** `JARVIS_CHATGPT_FALLBACK=1`. Off, nothing here runs: turning it on
sends the conversation, memory and whatever the tools return to OpenAI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import claude_env
import data_paths
import pretool_gate
import process_tree

log = logging.getLogger("jarvis.chatgpt_fallback")

ENABLE_ENV = "JARVIS_CHATGPT_FALLBACK"
MODEL_ENV = "JARVIS_CHATGPT_MODEL"
CODEX_PATH_ENV = "JARVIS_CODEX_PATH"
# Not a "code mode" model: those hand the model a JavaScript `exec` tool with
# the shell nested inside it. Measured: every model in the catalog but this
# one was code-mode on 2026-09-27.
DEFAULT_MODEL = "gpt-5.5"

# How long Codex lets one MCP call run. Longer than any turn: Codex giving up
# on a call is the one outcome that must never happen first. It covers the
# gate's wait for the owner (120 s, inside the gateway's 155 s) AND the
# service's own run after the yes — a LinkedIn call was measured at 180 s —
# and a call Codex abandons can still complete at the service while the
# model is told it failed, and says so, and the owner approves it again. The
# turn's own ceiling decides instead, and a turn it ends reports what it had
# reached for (`server._what_the_turn_had_done`), as on the Claude path.
TOOL_TIMEOUT_SEC = 3600
STARTUP_TIMEOUT_SEC = 60
READINESS_TTL_SEC = 600.0
INSTRUCTIONS_NAME = "jarvis-instructions.md"

# Every feature this Codex has on by default that is a tool, reaches a
# service, or acts on the machine. `--disable` of a name this Codex does not
# know is an error, so a rename fails loudly instead of quietly staying on.
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "view_image",
    "apps", "plugins", "remote_plugin", "plugin_sharing", "skill_search",
    "skill_mcp_dependency_install", "tool_suggest", "tool_call_mcp_elicitation",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use",
    "in_app_browser", "in_app_chat", "in_app_dictation", "in_app_local_automation",
    "in_app_updates", "image_generation", "multi_agent", "multi_agent_v2", "goals",
    "hooks", "memories", "sleep_tool", "workspace_dependencies", "worktrees",
    "guardian_approval", "realtime_conversation", "code_mode_host",
)
# On in this build whatever is passed — measured: `--disable unified_exec`
# changes nothing in `features list` — and harmless while the feature named
# beside it is off: `unified_exec` only picks how the shell tool is built,
# and `--disable shell_tool` removes the tool itself (`exec_command`,
# `write_stdin`), measured by ablation against a fake Responses server. So it
# may be on only while that one is off.
INERT_WHILE_OFF = {"unified_exec": "shell_tool"}
# What may stay on: plumbing, not tools. `readiness()` refuses to serve when
# anything else is on — a Codex update adds features switched on.
KEEP_FEATURES = frozenset({
    "auth_elicitation", "compaction_image_budget", "content_item_kinds",
    "enable_request_compression", "fast_mode", "mentions_v2", "secret_auth_storage",
    "unbounded_connection_retries",
})

# Configuration every run carries, after `exec` (overrides given before it
# are replaced by those given after it — measured). Each key was checked
# against this Codex with `--strict-config`, which refuses an unknown one.
_SETTINGS = (
    'approval_policy="never"', 'sandbox_mode="read-only"', 'web_search="disabled"',
    "tools.experimental_request_user_input={enabled=false}",
    "skills.include_instructions=false", "project_doc_max_bytes=0",
    "include_permissions_instructions=false", "include_environment_context=false",
    "analytics.enabled=false", 'forced_login_method="chatgpt"',
    # Where the conversation goes and what steers it, pinned to Codex's own
    # defaults (the provider's id; the base URL as the binary spells it) so
    # no configuration layer can move them: an override beats every layer,
    # and `developer_instructions=""` sends no developer message, exactly as
    # unset does (both measured against a fake Responses server).
    # `openai_base_url` has no value that means "unset", so it cannot be
    # pinned: that is why any machine-wide layer at all refuses
    # (`system_config_problem`), asked again right before every run.
    'model_provider="openai"', 'chatgpt_base_url="https://chatgpt.com/backend-api/"',
    'developer_instructions=""',
)

# The items a turn may report. Anything else — `command_execution`,
# `web_search`, `file_change`, whatever a Codex update names next — is a tool
# of Codex's own that the flags did not switch off.
ALLOWED_ITEMS = frozenset({"agent_message", "reasoning", "mcp_tool_call", "error"})

# Codex's own MCP resource readers, which no flag removes. They report as
# `mcp_tool_call` items, but not with a server from the table (measured on
# 0.155.0-alpha.9.2): with no `server` argument the item says "codex" and the
# call lists every configured server's resources; with one, the item says
# whatever name the model passed, and a name Codex does not know comes back
# `status: "failed"`, "unknown MCP server". None of them is a JARVIS tool,
# and none reaches anything but configured servers — JARVIS's own, which has
# no resources, and the gateways, which refuse `resources/*`. Only a reader
# ANSWERED by a server outside the table is a breach — and only an item of
# exactly that shape is a reader: a tool of the same name on some other
# server is that server's tool (`reader_call`).
RESOURCE_READERS = frozenset({"list_mcp_resources", "list_mcp_resource_templates",
                              "read_mcp_resource"})
# The server name a reader called without one reports (measured).
ALL_SERVERS = "codex"
# The breach kind for a machine-wide configuration found only after a run.
CONFIG_APPEARED = "system_config_appeared"


# The two readers that, given no server, list every configured one.
_LISTING_READERS = frozenset({"list_mcp_resources", "list_mcp_resource_templates"})


def reader_call(server: str, tool: str, arguments) -> Optional[str]:
    """For an `mcp_tool_call` item that is one of Codex's own resource
    readers, the server it named ("" for all of them); None for anything
    else. A reader reports `ALL_SERVERS` when it named none, and exactly
    the name it was given otherwise (measured) — trimmed, as Codex trims
    it. Only the two listings name none — whatever else they carry, a
    cursor, say: what they answer is checked instead (`listed_servers`);
    `read_mcp_resource` always names its server."""
    if tool not in RESOURCE_READERS or not isinstance(arguments, dict):
        return None
    named = arguments.get("server")
    named = named.strip() if isinstance(named, str) else named
    if not named:
        return "" if tool in _LISTING_READERS and server == ALL_SERVERS else None
    return str(named) if server == str(named) else None


def listed_servers(result) -> Optional[set]:
    """The servers an all-server listing's answer names, or None when it
    cannot be read. Codex answers `{"resources": [...]}` or
    `{"resourceTemplates": [...]}` as text (measured, empty); an entry names
    the server it came from."""
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return None
    servers: set = set()
    read_any = False
    for part in content:
        if not (isinstance(part, dict) and isinstance(part.get("text"), str)):
            return None
        try:
            body = json.loads(part["text"])
        except ValueError:
            return None
        # Exactly a listing's keys: anything else is not a listing.
        if not isinstance(body, dict) or set(body) - {"resources", "resourceTemplates",
                                                      "nextCursor"}:
            return None
        read_any = True
        for key in ("resources", "resourceTemplates"):
            entries = body.get(key, [])
            if not isinstance(entries, list):
                return None
            for entry in entries:
                servers.add(str(entry.get("server")) if isinstance(entry, dict) else "")
    return servers if read_any else None


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or DEFAULT_MODEL


_RESTRICTED: set[str] = set()


def codex_home() -> Path:
    """Where Codex keeps JARVIS's login and threads — never the user's own
    `~/.codex`. Private to this account: restricted once per process (with
    everything under it that does not inherit), not on every call — it is
    asked for several times a turn, and the walk grows with every thread
    Codex keeps there."""
    home = data_paths.data_dir() / "codex-home"
    if str(home) not in _RESTRICTED or not home.is_dir():
        home.mkdir(parents=True, exist_ok=True)
        data_paths.restrict_to_owner(home)
        _RESTRICTED.add(str(home))
    return home


# What makes a folder somebody's project to Codex: a repository. It takes the
# nearest `.git` above its cwd for the project root, and a project's
# AGENTS.md, `.agents/` skills and `.codex/` layers are looked for between
# that root and the cwd; with no repository above it, only the cwd itself —
# empty — is looked in. `~/.codex` and `~/.agents` are the user's own, not a
# project's, and are no marker.
_PROJECT_MARKERS = (".git",)


def _in_a_project(path: Path) -> Optional[Path]:
    for folder in (path, *path.parents):
        for marker in _PROJECT_MARKERS:
            if (folder / marker).exists():
                return folder / marker
    return None


def workdir() -> Path:
    """The folder a turn runs in: empty, private, and inside nobody's project.

    Inside the data directory when that is not itself inside a repository.
    It usually is — the default is `<repo>/data` — and then Codex, walking
    up for a `.git`, would take JARVIS's own checkout for the project, with
    its AGENTS.md and `.agents/` beside it. Then a folder of its own under
    the account's local application data instead."""
    preferred = codex_home() / "cwd"
    if _in_a_project(preferred.parent) is None:
        return preferred
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or str(Path.home() / ".cache")
    return Path(base) / "JARVIS" / "codex-cwd"


def workdir_problem() -> Optional[str]:
    found = _in_a_project(workdir())
    return f"{found} is above the folder Codex would run in" if found else None


def codex_candidates() -> list[str]:
    """Every Codex CLI this machine might have, best first:
    `JARVIS_CODEX_PATH`; the copies Codex Desktop keeps under
    `%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<hash>`, newest first (the hash
    changes with every update, and the packaged copy under WindowsApps
    refuses to run); `codex` on PATH; the native binary inside npm's
    `@openai/codex` package.

    Executables only — on Windows, `.exe` and nothing else. npm's own
    `codex.cmd` shim is never used: a batch file is re-parsed by cmd.exe,
    and a JARVIS command line carries quoted JSON — cmd would read its `&`,
    `|` and `%` as its own syntax."""
    found: list[str] = []
    configured = os.environ.get(CODEX_PATH_ENV, "").strip()
    if configured:
        found.append(configured)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found += [str(p) for p in sorted(Path(local, "OpenAI", "Codex", "bin").glob("*/codex.exe"),
                                         key=_mtime, reverse=True)]
    on_path = shutil.which("codex")
    if on_path:
        found.append(on_path)
    appdata = os.environ.get("APPDATA")
    if appdata:
        vendor = Path(appdata, "npm", "node_modules", "@openai", "codex", "vendor")
        found += [str(p) for p in sorted(vendor.glob("*/codex/codex.exe"), key=_mtime, reverse=True)]
    unique: list[str] = []
    for path in found:
        if path in unique:
            continue
        if os.name == "nt" and not path.lower().endswith(".exe"):
            continue
        if path.lower().endswith((".cmd", ".bat")):
            continue
        unique.append(path)
    return unique


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def child_env(base: Optional[dict] = None, *, home: Optional[Path] = None) -> dict:
    """Codex's environment: what a Claude Code child gets, minus every
    `OPENAI_*` and `CODEX_*` — `CODEX_API_KEY` bills an API key instead of
    the subscription, `CODEX_ACCESS_TOKEN` and the base-URL overrides
    redirect it (measured) — plus JARVIS's own `CODEX_HOME`. Case-blind,
    because Windows is."""
    env = {k: v for k, v in claude_env.child_env(base).items()
           if not k.upper().startswith(("OPENAI_", "CODEX_", "AZURE_OPENAI_"))}
    env["CODEX_HOME"] = str(home or codex_home())
    return env


def toml(value: Any) -> str:
    """The small closed set of TOML values a `-c` override needs. Strings are
    JSON-quoted, which TOML reads the same way, and which escapes a Windows
    path's backslashes (`\\U` breaks a TOML basic string)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)} = {toml(v)}"
                               for k, v in value.items()) + "}"
    raise TypeError(f"no TOML for {type(value).__name__}")


def gate_reachable(tool_url: str) -> bool:
    """Whether the gateway can ask the gate at `tool_url`. It sends the
    bearer token over TLS it does not verify, so it dials loopback only; a
    JARVIS bound to one LAN address does not listen there."""
    import guarded_mcp
    return guarded_mcp.gate_url_problem(tool_url) is None


def mcp_table(*, mcp_config: Path, tool_url: str, connections: list[str],
              jarvis_tools: list[str], nonce: str, env_names: list[str],
              turn_ceiling: float = 0.0) -> dict:
    """Every MCP server Codex gets, as ONE table — one `-c mcp_servers=…`
    override, so a connection named with a dot is still one server (a
    dotted key per server splits it; measured).

    `jarvis` is JARVIS's own server exactly as the Claude brain has it,
    limited to the tools the brain is granted, plus this turn's `nonce`:
    `/internal/tool` takes a call during a fallback turn as the owner's only
    when it carries it, so the idle Claude process beside it cannot borrow
    the turn. Each declared connection is `guarded_mcp.py` in front of the
    real server: every `tools/call` goes to JARVIS's approval gate first,
    with the same nonce. A connection also gets `env_names` passed through,
    so the service starts with the environment the Claude path gives it —
    Codex otherwise hands an MCP server only a short fixed list. None at all
    when the gate is not on loopback (`gate_reachable`): the gateway would
    refuse to start, and they are left out rather than left failing."""
    servers = json.loads(Path(mcp_config).read_text(encoding="utf-8"))["mcpServers"]
    jarvis = servers["jarvis"]
    token_file = jarvis.get("env", {}).get("JARVIS_TOOL_TOKEN_FILE", "")
    # Never shorter than the turn: see TOOL_TIMEOUT_SEC.
    timeout = max(TOOL_TIMEOUT_SEC, int(turn_ceiling) + 60)
    common = {"default_tools_approval_mode": "approve", "tool_timeout_sec": timeout,
              "startup_timeout_sec": STARTUP_TIMEOUT_SEC}
    table: dict = {"jarvis": {"command": jarvis["command"], "args": list(jarvis.get("args") or []),
                              "env": {**dict(jarvis.get("env") or {}), "JARVIS_TOOL_NONCE": nonce},
                              "required": True, "enabled_tools": list(jarvis_tools), **common}}
    wanted = [n for n in connections if n != "jarvis" and n in servers]
    if wanted and not gate_reachable(tool_url):
        log.warning("chatgpt fallback: JARVIS is not listening on loopback (%s), so the "
                    "gateway cannot reach the gate; %s left out", tool_url, ", ".join(wanted))
        return table
    gateway = str(Path(__file__).with_name("guarded_mcp.py"))
    for name in wanted:
        table[name] = {"command": sys.executable,
                       "args": [gateway, "--config", str(mcp_config), "--server", name,
                                "--gate-url", tool_url, "--token-file", token_file,
                                "--nonce", nonce],
                       "env_vars": list(env_names), "required": False, **common}
    return table


class CommandTooLong(ValueError):
    """The command line would not fit in what Windows lets one process be
    given (32,767 characters) — too many connections for one `-c`."""


# Below CreateProcess's 32,767, with room for the quoting it adds.
MAX_COMMAND_CHARS = 30000


def _setting_flags() -> list[str]:
    flags: list[str] = []
    for setting in _SETTINGS:
        flags += ["-c", setting]
    return flags


def _disable_flags() -> list[str]:
    return [arg for feature in DISABLED_FEATURES for arg in ("--disable", feature)]


def exec_argv(command: list[str], *, model_name: str, instructions_file: Path, mcp: dict,
              resume: Optional[str] = None) -> list[str]:
    """The one command line a fallback turn runs. `command` is how to start
    Codex (its path; a test's fake is an interpreter and a script).
    Everything after `exec`; a resumed thread gets the whole set again (the
    tools follow the flags of the call, not of the thread — measured)."""
    flags = ["--json", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
             "--strict-config", "-m", model_name, *_setting_flags(),
             "-c", f"model_instructions_file={toml(str(instructions_file))}",
             "-c", f"mcp_servers={toml(mcp)}", *_disable_flags()]
    argv = ([*command, "exec", "resume", *flags, resume, "-"] if resume
            else [*command, "exec", *flags, "-"])
    length = len(subprocess.list2cmdline(argv))
    if length > MAX_COMMAND_CHARS:
        raise CommandTooLong(f"the Codex command line is {length} characters; "
                             f"Windows allows {MAX_COMMAND_CHARS}")
    return argv


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------

@dataclass
class Readiness:
    ok: bool
    # Why not, as a clause JARVIS can say aloud after "ChatGPT can't stand
    # in:" — and what to do about it, for the log, preflight and the setup
    # script. Never a credential; nothing here reads one.
    reason: str = ""
    remedy: str = ""
    command: list = field(default_factory=list)   # how to start Codex
    version: str = ""
    checked_at: float = field(default_factory=time.monotonic)
    # The binary as it was vetted: (size, mtime). A Codex updated in place
    # is asked again before its next turn (`is_current`).
    stamp: Optional[tuple] = None
    # A refusal that ran no Codex — switched off, a configuration file, the
    # folder — costs a few `stat`s, and is asked again every time rather
    # than remembered for minutes after the file is gone.
    static: bool = False


_readiness: Optional[Readiness] = None
# Set once Codex has been seen doing what it was told it could not — the
# verdict said from then on — and kept until JARVIS restarts: no re-check
# can say why the flags failed.
_breach: Optional[Readiness] = None
BREACH_REASON = "Codex used a tool I don't allow, so I've stopped using it"
_SETUP = "run `python scripts/chatgpt_setup.py login` in JARVIS's folder"
_UPDATE = "update JARVIS"


def _run(argv: list[str], env: dict, timeout: float = 30.0,
         cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          cwd=str(cwd) if cwd is not None else None,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _stamp(command: list) -> Optional[tuple]:
    try:
        st = Path(command[0]).stat()
        return (st.st_size, st.st_mtime)
    except (OSError, IndexError, TypeError):
        return None


def _resolved(command: list[str]) -> list[str]:
    """`command` with a bare name — `JARVIS_CODEX_PATH=codex.exe`, or a
    test's — resolved on PATH, before anything runs it: the binary asked is
    the binary stamped, and compared later (`is_current`)."""
    if command and not Path(command[0]).is_file():
        found = shutil.which(command[0])
        if found:
            return [found, *command[1:]]
    return command


def find_command(env: dict, cwd: Optional[Path] = None) -> tuple[Optional[list[str]], str]:
    """The first candidate that answers `--version`, and its version."""
    for path in codex_candidates():
        command = _resolved([path])
        try:
            done = _run([*command, "--version"], env, timeout=15.0, cwd=cwd)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return command, done.stdout.strip()
    return None, ""


# One line of `codex features list`, as measured on 0.155.0-alpha.9.2: the
# name (dotted for a sub-feature — `guardianv2.thread_context`), its stage
# (one word or two — "under development"), and `true` or `false`. No header.
_FEATURE_LINE_RE = re.compile(r"([a-z0-9_.]+)\s+([a-z]+(?: [a-z]+)?)\s+(true|false)")


def vet_features(listing: str) -> Optional[tuple[str, str]]:
    """(reason, remedy) if `codex features list`, run with JARVIS's flags,
    shows anything JARVIS has not vetted — or anything it cannot read. None
    when every line was understood, every switched-off feature is seen off,
    and nothing on is outside `KEEP_FEATURES`. Strict on purpose: a Codex
    that changes this table's format has changed something, and a check
    that passes whatever it cannot parse is not a check."""
    rows: dict[str, tuple[str, bool]] = {}
    for line in listing.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _FEATURE_LINE_RE.fullmatch(line)
        if match is None:
            return ("this Codex's feature list is not one I can read",
                    f"{_UPDATE}: `codex features list` printed {line[:120]!r}")
        rows[match.group(1)] = (match.group(2), match.group(3) == "true")
    def excused(name: str) -> bool:
        guard = INERT_WHILE_OFF.get(name)
        return guard is not None and rows.get(guard, ("", True))[1] is False
    still_on = [name for name in DISABLED_FEATURES
                if rows.get(name, ("", True))[1] and not (name in rows and excused(name))]
    if still_on:
        return ("this Codex did not switch off what I switch off",
                f"{_UPDATE}: still on or not listed: {', '.join(sorted(still_on))}")
    unvetted = sorted(name for name, (stage, on) in rows.items()
                      if on and stage != "removed" and name not in KEEP_FEATURES
                      and not excused(name))
    if unvetted:
        return ("this Codex has switched on something I haven't vetted",
                f"{_UPDATE}: newly enabled {', '.join(unvetted)}")
    return None


def home_config_problem(home: Path) -> Optional[str]:
    """Why JARVIS's own Codex home would make `features list` see a
    different Codex than `exec` runs, or None. `exec` ignores
    `<home>/config.toml` (`--ignore-user-config`), which `features list`
    cannot be told to; a `[features]` table there could show a new tool as
    off to the check while `exec` has it on."""
    path = home / "config.toml"
    if not path.exists():
        return None
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{path} cannot be read"
    for key in ("features", "profile", "profiles"):
        if key in config:
            # A profile carries a features table of its own.
            return f"{path} sets {key}"
    return None


# FOLDERID_ProgramData, which is how Codex finds the machine-wide layer
# ("SHGetKnownFolderPath(FOLDERID_ProgramData)" in the binary) — not the
# environment, which a process can be started without.
_FOLDERID_PROGRAM_DATA = "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}"
SYSTEM_CONFIG_FILES = ("config.toml", "managed_config.toml", "requirements.toml")


def _known_program_data() -> Optional[str]:
    """The ProgramData known folder as Windows reports it, or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        raw = uuid.UUID(_FOLDERID_PROGRAM_DATA).bytes_le
        guid = _GUID.from_buffer_copy(raw)
        path = ctypes.c_wchar_p()
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD,
                                                 wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_wchar_p)]
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return None
        try:
            return path.value or None
        finally:
            ole32.CoTaskMemFree(path)
    except Exception:
        return None


def system_config_folders() -> list[Path]:
    """Every folder Codex may read a machine-wide layer from. On Windows the
    known folder, the environment's idea of it, and Codex's own fallback
    when the known folder cannot be resolved (`C:\\ProgramData`) — all of
    them, since a check that looks somewhere else than Codex fails open."""
    if os.name != "nt":
        return [Path("/etc/codex")]
    bases = [_known_program_data(), os.environ.get("ProgramData"),
             os.environ.get("PROGRAMDATA"),
             (os.environ.get("SystemDrive") or "C:") + "\\ProgramData", "C:\\ProgramData"]
    folders: list[Path] = []
    for base in bases:
        if base and (Path(base) / "OpenAI" / "Codex") not in folders:
            folders.append(Path(base) / "OpenAI" / "Codex")
    return folders


def system_config_problem() -> Optional[str]:
    """The machine-wide Codex configuration that rules the fallback out, or
    None. `--ignore-user-config` drops only `$CODEX_HOME/config.toml`
    (Codex's own help); the system layer is still loaded, and merged table
    by table — an `[mcp_servers.x]` there would sit beside JARVIS's table
    with no gateway in front of it, and an `openai_base_url` would send the
    conversation elsewhere. Any file there refuses: none is created by Codex
    Desktop or npm, and a standard user can create that folder. Three
    `stat`s: cheap enough to ask right before every run."""
    for folder in system_config_folders():
        for name in SYSTEM_CONFIG_FILES:
            if (folder / name).exists():
                return str(folder / name)
    return None


def config_fingerprint() -> tuple:
    """The machine-wide configuration folders as they are: whether each —
    and the `OpenAI` folder above it — exists, and when its entries last
    changed. A file there only while a run starts, gone again after, still
    changes this."""
    marks = []
    for folder in system_config_folders():
        # On Windows the vendor folder above it too (`OpenAI`, whose entries
        # are only Codex's); elsewhere not — `/etc` changes all the time.
        for path in ((folder.parent, folder) if os.name == "nt" else (folder,)):
            try:
                st = path.stat()
                marks.append((str(path), True, st.st_mtime_ns))
            except OSError:
                marks.append((str(path), False, 0))
    return tuple(marks)


def config_problem(home: Path) -> Optional[tuple[str, str]]:
    """(reason, remedy) when a configuration JARVIS did not write would
    shape what Codex does — in its own home, or machine-wide — or None.
    Reads files only; asked before anything runs Codex, and again right
    before every turn."""
    own = home_config_problem(home)
    if own:
        return ("something has configured JARVIS's Codex behind my back",
                f"remove it: {own}")
    system = system_config_problem()
    if system:
        return ("this computer's Codex configuration rules ChatGPT out",
                f"{system} configures every Codex on this machine; if you put it there, "
                f"remove it, otherwise ask whoever manages this PC, or leave the "
                f"fallback off")
    return None


def _catalog_models(text: str) -> Optional[list]:
    try:
        catalog = json.loads(text)
    except (ValueError, TypeError):
        return None
    models = catalog.get("models") if isinstance(catalog, dict) else None
    return models if isinstance(models, list) else None


def model_problem(home: Path, model_name: str, live: Optional[list] = None,
                  version: str = "") -> Optional[str]:
    """Why `model_name` may not run, or None. A "code mode" model is handed
    a JavaScript `exec` tool with the shell nested inside it, which none of
    the `--disable` flags reach; Codex marks them `tool_mode:
    "code_mode_only"` in its catalog.

    Two catalogs, and either one saying code-mode refuses: `live`, what
    `codex debug models` renders now, and the one Codex cached in its home
    the last time it ran — which is what the NEXT run starts from, until it
    refreshes. With neither, only the default (measured) is allowed. A
    cache another version of Codex wrote is not read: Codex itself ignores
    it, and only a run could replace it — which a refusal here prevents.
    Compared exactly, on the version without its pre-release part, which is
    how Codex stamps it (measured: codex-cli 0.155.0-alpha.9.2 wrote
    `"client_version": "0.155.0"`): `0.155.10` did not write a cache
    stamped `0.155.1`."""
    cached = None
    whole = re.match(r"\d+\.\d+\.\d+", version.split()[-1]) if version.split() else None
    running = whole.group(0) if whole else ""
    try:
        raw = (home / "models_cache.json").read_text(encoding="utf-8")
        body = json.loads(raw)
        written_by = str(body.get("client_version") or "") if isinstance(body, dict) else ""
        if not (running and written_by and written_by != running):
            cached = _catalog_models(raw)
    except (OSError, ValueError):
        pass
    catalogs = [c for c in (live, cached) if c is not None]
    if not catalogs:
        return None if model_name == DEFAULT_MODEL else (
            f"Codex has not listed its models yet, so {model_name} can't be checked")
    for catalog in catalogs:
        entry = next((e for e in catalog if isinstance(e, dict) and e.get("slug") == model_name),
                     None)
        if entry is None:
            return f"Codex doesn't offer {model_name}"
        if "code_mode" in str(entry.get("tool_mode") or ""):
            return f"{model_name} only works with a code tool JARVIS can't gate"
    return None


def check_readiness(command: Optional[list[str]] = None) -> Readiness:
    """Whether a fallback turn can run, and if not, why — asked of Codex
    itself, never guessed. No model is called: `--version`, `login status`,
    `features list` and `debug models` are local (or read a catalog). And
    not after a breach (`note_breach`), which only a restart clears.
    `command` is for a test's fake Codex."""
    if not enabled():
        return Readiness(False, "it's switched off", f"set {ENABLE_ENV}=1 in .env", static=True)
    if _breach:
        return _breach
    home = codex_home()
    # What needs no Codex first: a policy file no update of JARVIS can
    # get past is named as that, not as a feature Codex did not switch off.
    config = config_problem(home)
    if config:
        return Readiness(False, *config, static=True)
    place = workdir_problem()
    if place:
        return Readiness(False, "the folder Codex would run in is inside a project",
                         f"move JARVIS's data directory out of it: {place}", static=True)
    cwd = workdir()
    env = child_env(home=home)
    version = ""
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        if command is None:
            command, version = find_command(env, cwd)
        else:
            command = _resolved(command)
            version = _run([*command, "--version"], env, cwd=cwd).stdout.strip()
        if not command:
            return Readiness(False, "the Codex app isn't installed",
                             "install Codex Desktop, or `npm install -g @openai/codex`")
        # Stamped before it is vetted: a binary replaced during the checks
        # below is found out by the next `is_current`, not taken as vetted.
        stamp = _stamp(command)
        # In the folder `exec` runs in, so every layer Codex finds from its
        # working directory is the one the run will find.
        login = _run([*command, "login", "status"], env, cwd=cwd)
        # The same `-c` settings `exec` runs with: the table is then the
        # one those settings produce, not the defaults.
        features = _run([*command, *_setting_flags(), *_disable_flags(), "features", "list"],
                        env, cwd=cwd)
        catalog = _run([*command, "debug", "models"], env, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as e:
        return Readiness(False, "Codex wouldn't run", f"{type(e).__name__}: {e}", command or [])
    said = (login.stdout + login.stderr).lower()
    if "logged in using chatgpt" not in said:
        if "api key" in said:
            return Readiness(False, "Codex is signed in with an API key, not a ChatGPT account",
                             _SETUP, command, version)
        return Readiness(False, "Codex isn't signed in for me yet", _SETUP, command, version)
    if features.returncode != 0:
        return Readiness(False, "this Codex has changed a switch I rely on",
                         f"{_UPDATE}: `codex features list` refused a feature it disables "
                         f"({(features.stderr or features.stdout).strip()[:200]})",
                         command, version)
    problem = vet_features(features.stdout)
    if problem:
        return Readiness(False, problem[0], problem[1], command, version)
    live = _catalog_models(catalog.stdout) if catalog.returncode == 0 else None
    problem = model_problem(home, model(), live, version)
    if problem:
        return Readiness(False, problem, f"set {MODEL_ENV} to {DEFAULT_MODEL}", command, version)
    return Readiness(True, "", "", command, version, stamp=stamp)



# Bumped by every verdict a run reaches (`note_breach`, `mark_unready`,
# `forget_readiness`): a check that started before one must not store its
# answer over it. Checks run in threads — preflight's, a turn's refresh.
_epoch = 0
# What a run found that no check can see — a setting this Codex refuses, a
# model its own run marked code-mode — held for the rest of the limit, until
# the next limit's refresh (`new_limit`), not only until the cache's TTL.
_held: Optional[Readiness] = None


def readiness(*, refresh: bool = False, new_limit: bool = False) -> Readiness:
    """`check_readiness`, remembered for ten minutes (it runs processes).
    `refresh` asks again; `new_limit` — the start of a limit episode — also
    lets go of what the last limit's runs found. A breach holds regardless,
    until restart. A binary updated in place since it was vetted is vetted
    again, whoever asks (`is_current`). Blocking: call it off the event
    loop."""
    global _readiness, _held
    if new_limit:
        _held = None
    if _breach:
        return _breach
    if _held is not None:
        return _held
    current = _readiness
    if (refresh or current is None
            or time.monotonic() - current.checked_at > READINESS_TTL_SEC
            or (current.ok and not is_current(current))
            or (not current.ok and current.static)):
        asked_at = _epoch
        answer = check_readiness()
        if asked_at == _epoch and not _breach and _held is None:
            _readiness = answer
        else:
            return readiness()      # a run spoke meanwhile: its verdict stands
        current = answer
    return current


def cached_readiness() -> Optional[Readiness]:
    """The last answer, without asking again; None if nothing has asked.
    Never runs a check — it is read on the event loop — so every global is
    read once: a check in another thread may change them meanwhile."""
    breach, held, current = _breach, _held, _readiness
    if breach:
        return breach
    return held if held is not None else current


def forget_readiness() -> None:
    """A run failed: whatever the last answer was, ask again next time."""
    global _readiness, _epoch
    _epoch += 1
    _readiness = None


def mark_unready(reason: str, remedy: str) -> None:
    """A run found what readiness could not — a setting this Codex refuses,
    a model that turned code-mode: held for the rest of the limit, so no
    turn until the next limit tries again."""
    global _readiness, _held, _epoch
    _epoch += 1
    _held = _readiness = Readiness(False, reason, remedy)


def note_breach(what: str, *, reason: Optional[str] = None,
                remedy: Optional[str] = None) -> None:
    """Codex did something its flags forbid. Not ready again until JARVIS
    restarts: re-running the same checks would pass the same Codex. Said
    from then on as `reason` — what it was — with `remedy`."""
    global _breach, _readiness, _epoch
    _epoch += 1
    _breach = _readiness = Readiness(
        False, reason or BREACH_REASON,
        remedy or f"restart JARVIS once Codex is fixed ({what})")


def breached() -> bool:
    return _breach is not None


def is_current(ready: Readiness) -> bool:
    """Whether the binary `ready` vetted is still the one on disk. One that
    could not be stamped cannot be shown to be: asked again."""
    return ready.stamp is not None and _stamp(ready.command) == ready.stamp


def bind_problem() -> Optional[str]:
    """Why the fallback's gateway could not reach the gate, from the bind
    address the server records (as `server._tool_url_base` reads it), or
    None. For preflight, which does not import the server."""
    host = os.getenv("JARVIS_BIND_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::", "127.0.0.1", "::1", "localhost"):
        return None
    return (f"JARVIS listens on {host}, not on loopback, so on ChatGPT your "
            f"connected services are left out")


# ---------------------------------------------------------------------------
# ChatGPT's own limit
# ---------------------------------------------------------------------------

# "You've hit your usage limit. … try again at Sep 28th, 2026 12:13 AM." —
# a date when the reset is another day, only the time when it is today, and
# "Try" capitalised in some plans' wording. The apostrophe is U+2019.
_USAGE_LIMIT = re.compile(r"hit your usage limit", re.IGNORECASE)
_TRY_AGAIN_AT = re.compile(
    r"try again at (?:(?P<date>[A-Z][a-z]{2} \d{1,2}(?:st|nd|rd|th)?, \d{4}) )?"
    r"(?P<time>\d{1,2}:\d{2} ?[AP]M)", re.IGNORECASE)


def usage_limit_until(message: str, now: Optional[datetime] = None) -> tuple[bool, Optional[float]]:
    """(is this ChatGPT's own usage limit, when it resets — epoch seconds,
    or None when the message does not say). A time with no date is today;
    one that has already gone is this very minute, never tomorrow. Either
    way the end of the printed minute."""
    if not message or not _USAGE_LIMIT.search(message):
        return False, None
    match = _TRY_AGAIN_AT.search(message)
    if not match:
        return True, None
    clock = match.group("time").upper().replace(" ", "")
    try:
        at = datetime.strptime(clock, "%I:%M%p")
        if match.group("date"):
            day = re.sub(r"(?<=\d)(st|nd|rd|th)(?=,)", "", match.group("date"), flags=re.IGNORECASE)
            when = datetime.strptime(day.title(), "%b %d, %Y").replace(hour=at.hour,
                                                                        minute=at.minute)
        else:
            # Printed without a date only when the reset is today (Codex's
            # own format), so one that reads as past is this very minute —
            # seconds truncated, or a clock a little off — never tomorrow.
            now = now or datetime.now()
            when = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
            if when + timedelta(seconds=59) <= now:
                when = now
        # The printed minute's end: Codex drops the seconds, and reopening at
        # its start would send the next turn back into the limit.
        return True, (when + timedelta(seconds=59)).timestamp()
    except ValueError:
        return True, None


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------

@dataclass
class Event:
    kind: str                   # "tool" when an MCP call starts; "breach"
    name: str = ""


# The breach kind for an MCP call a server outside JARVIS's table answered.
UNCONFIGURED_SERVER = "unconfigured_mcp_server"


@dataclass
class Run:
    text: str = ""
    tools: list = field(default_factory=list)
    stop_reason: str = "result"  # result | timeout | error | chatgpt_limited
    error: str = ""
    retry_at: Optional[float] = None
    duration_sec: float = 0.0
    # An item type Codex was not allowed to produce, when it produced one.
    breach: Optional[str] = None
    # For an `UNCONFIGURED_SERVER` breach, which call: (server, tool), as
    # Codex reported them.
    breach_call: Optional[tuple] = None
    # The tools the gate reads as acting by their OWN names — `get&delete`,
    # which the CLI's spelling (`get_delete`) makes one verb — so the turn's
    # account of itself agrees with the gate (`pretool_gate.classify_call`).
    acting: set = field(default_factory=set)
    # Codex refused JARVIS's own command line before the thread started —
    # a setting this version no longer knows, under `--strict-config`.
    refused: Optional[str] = None
    started: bool = False


# How Codex words a configuration it will not take (`--strict-config`).
_REFUSED_CONFIG = re.compile(r"unknown (?:configuration )?field|unknown feature|"
                             r"invalid (?:value|type)|error loading config", re.IGNORECASE)


class CodexSession:
    """One Codex thread and the one process a turn of it runs. A thread is
    started fresh for every limit episode, every Claude generation and every
    "start fresh" (`reset`), and carries its OWN generation taint: what it
    has read that JARVIS did not write (`untrusted`), until it is reset."""

    def __init__(self, home: Path):
        self.home = home
        self.instructions = home / INSTRUCTIONS_NAME
        self.thread_id: Optional[str] = None
        self.untrusted: Optional[str] = None
        # Which thread this is, counted: a read that lands after its turn
        # has ended taints the thread it was made in, not a newer one.
        self.epoch = 0
        self._proc: Optional[asyncio.subprocess.Process] = None

    def reset(self) -> None:
        """A new thread: nothing said, nothing read."""
        self.thread_id = None
        self.untrusted = None
        self.epoch += 1

    def write_instructions(self, text: str) -> None:
        """The persona the next thread runs on. Rewritten only when a thread
        starts, so a resumed thread keeps reading the one it began with.
        Private by inheritance from the home it is written in."""
        self.instructions.write_text(text, encoding="utf-8")

    async def run(self, prompt: str, *, argv: list[str], timeout: float,
                  on_event: Callable[[Event], None],
                  servers: Optional[set] = None) -> Run:
        """One turn: `argv` with `prompt` on stdin, its JSONL read as it
        comes. Killed — with every MCP server and gateway it started — on
        timeout, on cancellation, the moment it reports an item that is not
        an answer, reasoning or an MCP call, the moment it calls a tool on a
        server that is not in `servers` (the table JARVIS gave it), and the
        moment one of its resource readers is answered by such a server:
        that server came from a configuration layer JARVIS does not
        control, and nothing gates it."""
        cwd = workdir()
        cwd.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        run = Run()
        failure = ""
        stderr_tail: list[str] = []
        proc = await process_tree.spawn(
            *argv, cwd=str(cwd), env=child_env(home=self.home),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=claude_env.STREAM_LINE_LIMIT)
        self._proc = proc

        async def lines(stream):
            # A line bigger than the limit is skipped, not fatal — the Claude
            # reader learned this the hard way (`Brain._read_stdout`) — and
            # decoded leniently before parsing, for the same reason.
            while True:
                try:
                    raw = await stream.readline()
                except ValueError:
                    log.warning("chatgpt fallback: skipped an oversized line from Codex")
                    continue
                if not raw:
                    return
                yield raw.decode("utf-8", "replace")

        async def drain_stderr():
            assert proc.stderr is not None
            async for text in lines(proc.stderr):
                stderr_tail.append(text.rstrip())
                del stderr_tail[:-20]

        def breach(kind: str, call: Optional[tuple] = None) -> None:
            run.breach, run.breach_call = kind, call
            run.tools.append(f"codex_{kind}")
            try:
                on_event(Event("breach", kind))
            except Exception:
                log.warning("chatgpt fallback: a breach listener failed", exc_info=True)
            process_tree.kill(proc)

        async def read_events():
            nonlocal failure
            assert proc.stdout is not None
            async for text in lines(proc.stdout):
                try:
                    event = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                item = event.get("item") if isinstance(event.get("item"), dict) else None
                if item is not None and kind in ("item.started", "item.updated", "item.completed"):
                    item_type = str(item.get("type") or "")
                    if item_type not in ALLOWED_ITEMS:
                        breach(item_type or "unnamed")
                        return
                if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                    self.thread_id = event["thread_id"]
                    run.started = True
                    continue
                if item is not None and item.get("type") == "mcp_tool_call":
                    server, tool = str(item.get("server") or ""), str(item.get("tool") or "")
                    named = reader_call(server, tool, item.get("arguments"))
                    if named is not None:
                        # Codex's own, not a JARVIS tool: not in the turn's
                        # tools, and a breach only once a server outside the
                        # table has answered it (`RESOURCE_READERS`) — the
                        # one it named, or one an all-server listing names.
                        if (kind == "item.completed" and servers is not None
                                and item.get("status") != "failed"):
                            if named and named not in servers:
                                breach(UNCONFIGURED_SERVER, (named, tool))
                                return
                            if not named:
                                listed = listed_servers(item.get("result"))
                                foreign = (sorted(listed - set(servers)) if listed is not None
                                           else ["an answer it could not read"])
                                if foreign:
                                    breach(UNCONFIGURED_SERVER, (foreign[0], tool))
                                    return
                        continue
                    if servers is not None and server not in servers:
                        breach(UNCONFIGURED_SERVER, (server, tool))
                        return
                    if kind == "item.started":
                        name = claude_env.mcp_tool_name(server, tool)
                        run.tools.append(name)
                        if pretool_gate.classify_call(name, claude_env.mcp_name_part(server),
                                                      tool) == "outward":
                            run.acting.add(name)
                        try:
                            on_event(Event("tool", name))
                        except Exception:
                            log.warning("chatgpt fallback: a tool listener failed", exc_info=True)
                    continue
                if kind == "item.completed" and item and item.get("type") == "agent_message":
                    if isinstance(item.get("text"), str) and item["text"].strip():
                        run.text = item["text"].strip()
                elif kind == "turn.failed":
                    error = event.get("error")
                    failure = str(error.get("message") if isinstance(error, dict) else error or "")
                elif kind == "error" and isinstance(event.get("message"), str):
                    # "Reconnecting… 2/5" is an error event too; it counts
                    # only if the turn then fails.
                    failure = failure or event["message"]

        # Reading starts before the prompt is written, and however the turn
        # ends the readers' outcome is collected, so a timeout or a
        # cancellation leaves nothing for the loop to complain about.
        readers = asyncio.gather(read_events(), drain_stderr())
        readers.add_done_callback(lambda f: f.cancelled() or f.exception())
        try:
            assert proc.stdin is not None
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass            # it exited early; what it said says why
            try:
                await asyncio.wait_for(asyncio.shield(readers), timeout)
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                if run.breach is None:
                    run.stop_reason = "timeout"
        finally:
            if proc.returncode is None:
                await process_tree.stop(proc)
            else:
                process_tree.release(proc)
            readers.cancel()
            self._proc = None
        run.duration_sec = time.monotonic() - started
        if run.breach:
            run.stop_reason = "error"
            if run.breach_call:
                run.error = (f"Codex called {run.breach_call[1]!r} on {run.breach_call[0]!r}, "
                             f"a server JARVIS did not give it")[:500]
            else:
                run.error = f"Codex reported a {run.breach} item, which JARVIS does not allow"
            return run
        if run.stop_reason == "timeout":
            run.error = "Codex did not finish in time"
            return run
        if proc.returncode == 0 and run.text:
            return run
        limited, retry_at = usage_limit_until(failure)
        tail = " ".join(stderr_tail[-3:])
        if limited:
            run.stop_reason, run.retry_at, run.error = "chatgpt_limited", retry_at, failure
            return run
        run.stop_reason = "error"
        run.error = (failure or tail or "Codex returned no answer")[:500]
        if not run.started and _REFUSED_CONFIG.search(tail):
            run.refused = tail[:300]
        return run

    async def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            await process_tree.stop(proc)
