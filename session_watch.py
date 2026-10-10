"""Watch every Claude Code session on this machine.

Three levels, and the distinction matters: a *process* is one `claude`; a
*conversation* is one `sessionId` and one transcript; a *project* is one cwd.
Measured on 2026-09-03, 17 live processes were 14 conversations in 10
projects — three processes shared one hammer conversation. Counting processes
is what made JARVIS miscount sessions out loud, so speech uses conversations.

Pure filesystem reading: no asyncio, no server imports, no LLM. Every parse is
failure-tolerant, because these files are written live by other processes and
will be read mid-write.
"""

from __future__ import annotations

import json
import os

import procs
import re
from dataclasses import dataclass, field
from pathlib import Path

# Both roots must be read: the CLI's default and the one Orcha sets via
# CLAUDE_CONFIG_DIR. On the dev machine `~/.claude/sessions` was empty and
# every session lived under `~/.claude-orcha` — a plain-Terminal user is the
# mirror image, so neither root may be dropped.
DEFAULT_ROOTS = ("~/.claude", "~/.claude-orcha")


def config_roots() -> list[Path]:
    """Every directory that may hold a `sessions/` roster, in priority order."""
    roots = [Path(r).expanduser() for r in DEFAULT_ROOTS]
    extra = os.getenv("JARVIS_CLAUDE_CONFIG_DIRS", "")
    for raw in extra.split(os.pathsep):
        raw = raw.strip()
        if raw:
            p = Path(raw).expanduser()
            if p not in roots:
                roots.append(p)
    return roots


def pid_alive(pid) -> bool:
    """True if the process exists, without touching it. See `procs.pid_alive`
    for why this is not `os.kill(pid, 0)` — on Windows that sends Ctrl+C.

    Kept as a name on this module (rather than imported straight into the
    callers) so tests can stub the roster's view of liveness in one place.
    """
    return procs.pid_alive(pid)


def encode_cwd(cwd: str) -> str:
    """The CLI's transcript directory name: EVERY non-alphanumeric becomes `-`.

    Not just `/`. A worktree path contains dots, and the `/`-only shortcut
    silently fails to find its transcript.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def _ms(value) -> float | None:
    """Roster timestamps are epoch milliseconds; we work in seconds."""
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RosterEntry:
    """One `sessions/<pid>.json` — one live `claude` process."""
    pid: int
    session_id: str
    cwd: str
    name: str
    root: Path
    kind: str = "interactive"
    entrypoint: str = "cli"
    status: str | None = None
    waiting_for: str | None = None
    started_at: float | None = None
    status_updated_at: float | None = None
    socket_path: str | None = None
    version: str = ""
    # The roster's `name`, raw, when it may be the thread's own — None when
    # there is none or the CLI says it derived it. `_thread_name` decides.
    given_name: str | None = None

    @property
    def steerable(self) -> bool:
        """A process can only be steered if it bound an inbox socket.

        Measured: 4 of 17 live entries had none. `ListAgents` cannot see those
        at all, which is why this watcher exists.

        Never raises. On Windows the inbox is a named pipe, and `stat()` on a
        pipe another client currently holds open answers ERROR_PIPE_BUSY
        (231) instead of "exists" — measured live: it took every watcher
        tick down for as long as the pipe was busy. A busy pipe is a bound
        pipe; anything else that fails is unknown, and unknown is "no".
        """
        if not self.socket_path:
            return False
        try:
            return Path(self.socket_path).exists()
        except OSError as e:
            return getattr(e, "winerror", None) == 231


def _parse_entry(path: Path, root: Path) -> RosterEntry | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None            # unreadable, empty, or caught mid-write
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data["pid"])
        session_id = str(data["sessionId"])
        cwd = str(data["cwd"])
    except (KeyError, TypeError, ValueError):
        return None            # without these three it is not a session
    return RosterEntry(
        pid=pid,
        session_id=session_id,
        cwd=cwd,
        name=str(data.get("name") or Path(cwd).name or session_id[:8]),
        root=root,
        kind=str(data.get("kind") or "interactive"),
        entrypoint=str(data.get("entrypoint") or "cli"),
        status=data.get("status") if isinstance(data.get("status"), str) else None,
        waiting_for=(data.get("waitingFor")
                     if isinstance(data.get("waitingFor"), str) and data.get("waitingFor")
                     else None),
        started_at=_ms(data.get("startedAt")),
        status_updated_at=_ms(data.get("statusUpdatedAt")),
        socket_path=(str(data["messagingSocketPath"])
                     if isinstance(data.get("messagingSocketPath"), str) else None),
        version=str(data.get("version") or ""),
        given_name=(data["name"] if isinstance(data.get("name"), str)
                    and data.get("nameSource") != "derived" else None),
    )


def read_roster(roots: list[Path] | None = None) -> list[RosterEntry]:
    """Every well-formed roster entry across every root. Never raises."""
    entries: list[RosterEntry] = []
    seen: set[tuple[int, str]] = set()
    for root in (roots if roots is not None else config_roots()):
        d = Path(root) / "sessions"
        try:
            files = sorted(d.glob("*.json"))
        except OSError:
            continue
        for f in files:
            entry = _parse_entry(f, Path(root))
            if entry is None:
                continue
            key = (entry.pid, entry.session_id)
            if key in seen:          # the same pid registered under two roots
                continue
            seen.add(key)
            entries.append(entry)
    return entries


# A 64 KB tail was sufficient for every one of the 14 live transcripts on
# 2026-09-03, including a 95 MB one, because `ai-title` and `last-prompt` are
# rewritten on every turn and therefore always sit near the end.
TAIL_BYTES = 64 * 1024
MAX_RECENT_TOOLS = 5
MAX_TEXT = 600


@dataclass
class Recap:
    """What a session is doing, read from the end of its transcript."""
    exists: bool = False
    title: str | None = None          # the CLI's own `aiTitle`
    last_prompt: str | None = None    # the CLI's own `lastPrompt`
    last_text: str | None = None      # what the session last said
    recent_tools: list[str] = field(default_factory=list)

    def summary(self) -> str | None:
        """One line describing the session: its title, else its last prompt."""
        return self.title or self.last_prompt


def transcript_path(root: Path, cwd: str, session_id: str) -> Path:
    return Path(root) / "projects" / encode_cwd(cwd) / f"{session_id}.jsonl"


def tail_objects(path: Path, nbytes: int = TAIL_BYTES) -> list[dict]:
    """Parse the JSON objects in the last `nbytes` of a JSONL file.

    Seeking into a multi-megabyte file always lands mid-line, so the first
    (partial) line is discarded. Unparseable lines are skipped: this file is
    appended to by another process while we read it.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()          # discard the partial line
            else:
                fh.seek(0)
            raw = fh.read()
    except OSError:
        return []
    out: list[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _clip(text, limit: int = MAX_TEXT) -> str | None:
    """One line of clipped text, or None — for a value of ANY shape.

    Every caller reads a field out of somebody else's transcript, so the type
    check belongs here rather than at each call site: the call sites had it
    twice out of three times, and the third raised AttributeError on a
    non-string `text` block, which escaped to `SessionWatcher._loop`, was
    logged as a warning and swallowed, and left `self.snapshot` FROZEN at the
    last good poll for as long as that line stayed inside the 64 KB tail.
    `/api/sessions` went on answering 200 the whole time.
    """
    if not isinstance(text, str) or not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def recap_from(objs: list[dict]) -> Recap:
    """Reduce transcript lines to a recap. Unknown line types are ignored —
    19 types were observed on the live machine and only these five are used."""
    r = Recap(exists=True)
    tools: list[str] = []
    for o in objs:
        kind = o.get("type")
        if kind == "ai-title":
            r.title = _clip(o.get("aiTitle"), 200) or r.title
        elif kind == "last-prompt":
            r.last_prompt = _clip(o.get("lastPrompt")) or r.last_prompt
        elif kind == "assistant" and not o.get("isSidechain"):
            message = o.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    r.last_text = _clip(block.get("text")) or r.last_text
                elif block.get("type") == "tool_use" and block.get("name"):
                    # `_clip` like its three siblings above, and for the same
                    # reason: this is a string out of somebody else's
                    # transcript, of any length and any shape, and it reaches
                    # a header line in `tool_session_detail`.
                    tools.append(_clip(block["name"]) or "")
    r.recent_tools = tools[-MAX_RECENT_TOOLS:]
    return r


def _roots_of(group: list, primary) -> list[Path]:
    """Every root this conversation registered in, the primary's first.

    Order matters: where two roots both hold a transcript (they are
    hardlinked on the live machine, so this is the normal case) the
    primary's is the one read.
    """
    out = [primary.root]
    for e in group:
        if e.root not in out:
            out.append(e.root)
    return out


def _first_recap(roots: list[Path], cwd: str, session_id: str) -> Recap:
    """The first root that actually has this conversation's transcript.

    `Recap(exists=False)` — no transcript anywhere — is what makes a session
    `fresh`, so looking under one root and stopping reported a busy
    conversation as "not started".
    """
    for root in roots:
        recap = read_recap(root, cwd, session_id)
        if recap.exists:
            return recap
    return Recap(exists=False)


def read_recap(root: Path, cwd: str, session_id: str,
               nbytes: int = TAIL_BYTES) -> Recap:
    """Recap a session, or `Recap(exists=False)` if it has no transcript.

    Absence is meaningful: a session nobody has prompted writes no transcript
    at all, which is how `fresh` is detected without parsing anything.
    """
    path = transcript_path(root, cwd, session_id)
    if not path.exists():
        return Recap(exists=False)
    return recap_from(tail_objects(path, nbytes))


# Derived conversation states.
FRESH = "fresh"          # alive but never prompted (no transcript) — never announced
NEEDS_YOU = "needs_you"
WORKING = "working"
SHELL = "shell"
IDLE = "idle"
GONE = "gone"
UNKNOWN = "unknown"      # alive, but the roster carries no status for it

# Who is at the other end of a conversation — its ORIGIN. Derived from the
# roster's `entrypoint` by `_origin`, whose table and history are below.
TERMINAL = "terminal"        # `claude` in a terminal
DESKTOP = "desktop"          # the Claude desktop app, Cowork included
EDITOR = "editor"            # the VS Code extension (and its forks)
REMOTE = "remote"            # claude.ai, the mobile app, Slack, Teams, ssh
BACKGROUND = "background"    # a program started it and answers for it
OTHER = "other"              # an entrypoint the table does not know

# Keywords matched against a lowercased `waitingFor` reason to flag one that
# a peer message cannot clear. The reason set itself is OPEN — "input needed"
# turned up after only "permission prompt" and "dialog open" had been seen,
# and more will appear. "input needed" is deliberately NOT in this list: a
# peer message over the inbox socket is exactly what a session waiting on
# input wants, so it stays answerable rather than getting waved off as
# needing a human hand. An unrecognised reason is likewise treated as
# answerable by default — JARVIS attempts it and reports the real outcome,
# rather than refusing something he could have done because the string
# didn't happen to be on this list yet.
HUMAN_HAND_REASONS = ("permission", "dialog", "trust", "login", "auth")

_QUESTION_TOOLS = ("AskUserQuestion", "ExitPlanMode")


_URL_RE = re.compile(r"\S+://\S+|\bwww\.\S+")


def _looks_like_a_question(text: str | None) -> bool:
    """Did the session stop to ask something?

    A bare `"?" in tail` is too eager: a URL query string counts. Measured on
    the live machine — a session that had hit its spend limit ended with
    `…/settings/usage?from=cc_cli_limit_message`, and was announced as needing
    the user. Needs-you announcements interrupt at URGENT priority, so a false
    positive here nags the user about a session that wants nothing.

    URLs are stripped first, then a question mark only counts where a question
    actually ends: at the close of the message or of one of its last lines.
    """
    if not text:
        return False
    cleaned = _URL_RE.sub(" ", text).rstrip()
    if not cleaned:
        return False
    if cleaned.endswith("?"):
        return True
    # A question can be followed by options ("...or SQLite?\n1. Postgres").
    return any(line.rstrip().endswith("?") for line in cleaned.splitlines()[-6:])


@dataclass
class SessionState:
    """One conversation: what it is, what it is doing, and how to reach it."""
    session_id: str
    cwd: str
    project: str
    state: str
    pids: list[int] = field(default_factory=list)
    primary_pid: int | None = None
    roster_name: str = ""
    voice_name: str = ""
    # The thread's own name — what the Claude app's sidebar calls it — made
    # sayable, or None when the roster carries only a name the CLI derived
    # from the folder. See `_thread_name`; it wins over the folder in
    # `_assign_voice_names`.
    thread_name: str | None = None
    # The words of `voice_name` that ARE the thread's own name — all of it,
    # or the base `_name_group` composed round it — so a sentence can say
    # them as a thread's name (`server._said_name`). None for a session
    # named by its folder. Set, and cleared, by `_assign_voice_names`.
    thread_part: str | None = None
    needs: str | None = None
    title: str | None = None
    last_prompt: str | None = None
    last_text: str | None = None
    recent_tools: list[str] = field(default_factory=list)
    # When the SESSION STARTED. Used for age ordering (`_name_by_age`'s
    # "the newer hammer" / "the older hammer"). Do not use this for spoken
    # elapsed time — see `since` below.
    started: float | None = None
    # When the CURRENT STATE began. Used for spoken elapsed time ("hammer has
    # been waiting about an hour"). Measured on the live roster: the largest
    # gap between `startedAt` and `statusUpdatedAt` was 102 HOURS (VoyageStudios,
    # pid 37497), with another material 4.2-hour gap on a hammer session — so
    # `started` and `since` are NOT interchangeable and must stay two fields.
    # Do not "simplify" these back into one.
    since: float | None = None
    # Who answers its prompts: one of the origin constants above, derived
    # from `entrypoint` through a closed table — so it is safe to branch on
    # and never needs saying raw. `entrypoint` IS the roster's own string,
    # kept for the dashboard to show; nothing that speaks prints it.
    origin: str = TERMINAL
    entrypoint: str = ""
    steerable: bool = False
    socket_path: str | None = None
    # Is this the conversation the user is actually sitting at, in this
    # project? See `_mark_primary` for the rule and why it can answer "no"
    # about every conversation in a project.
    primary: bool = False
    primary_reason: str = ""
    # Subagents this conversation dispatched, counted off its own
    # `<sessionId>/subagents/` folder. `agents_active` means "written to
    # within AGENT_ACTIVE_WITHIN_SEC", never "a process is alive" — there is
    # no process here to check.
    agents_seen: int = 0
    agents_active: int = 0
    # `agents_seen` hit MAX_AGENT_FILES: it is a floor, not a total.
    agents_capped: bool = False
    # What a PROGRAM's session is paused on while that program answers it —
    # a `waitingFor` reason, or "" when the roster named none. None when it
    # is not waiting on its host. Never set together with `needs`: see
    # `_derive_state`, and tests/test_prompt_owner.py for why it exists.
    waiting_on_host: str | None = None

    @property
    def announceable(self) -> bool:
        """`fresh` sessions are never spoken about unprompted: nobody has
        started them, so 'chitauri needs you' would be nonsense."""
        return self.state not in (FRESH,)

    @property
    def needs_a_human_hand(self) -> bool:
        """True when the thing it waits on cannot be answered over the socket."""
        reason = (self.needs or "").lower()
        return any(word in reason for word in HUMAN_HAND_REASONS)

    def summary(self) -> str | None:
        return self.title or self.last_prompt


# ── which conversation is the main one ──────────────────────────────────────
#
# There was no notion of a PRIMARY conversation before this. The rule, in
# full, because a rule nobody can state is a rule nobody can check:
#
#   Primary is decided PER PROJECT. A conversation is ELIGIBLE if it is
#   alive, has been prompted at least once (a `fresh` session has no
#   transcript and is nobody's main session), and a PERSON is at the other
#   end of it — an origin in `ATTENDED_ORIGINS`: a terminal, the desktop app,
#   an editor, a remote app. A program's session is never the main one:
#   nobody is typing into it. Nor is one whose entrypoint is not in the
#   table — nothing is claimed about who is typing into that.
#
#   (Eligibility used to be "origin is terminal", with every unknown
#   entrypoint defaulted TO terminal. So on a machine run from the desktop
#   app every real conversation was "a background conversation", and the one
#   Paperclip was driving through the SDK was "the only live conversation
#   here" — measured, 2026-09-28.)
#
#   Among the eligible, the most recently active wins, measured by `since`
#   (the roster's `statusUpdatedAt`).
#
#   AND: if the runner-up is within PRIMARY_MARGIN_SEC of the leader, NEITHER
#   is primary. Two conversations touched a minute apart are equally live and
#   the signal cannot separate them; claiming one anyway would be a guess
#   wearing a fact's clothes.
#
# Signals deliberately NOT used: transcript size (a long conversation is an
# old one, not an active one) and whether JARVIS spawned it (his own runs are
# filtered out of the roster upstream, in server._snapshot_or_empty, so they
# never reach this rule at all).
PRIMARY_MARGIN_SEC = 120.0

PRIMARY_ONLY = "the only live conversation here"
PRIMARY_RECENT = "most recently active"
PRIMARY_TIED = "equally live as another here"
NOT_PRIMARY_BACKGROUND = "a background conversation"
NOT_PRIMARY_UNPLACED = "started by something I can't place"
NOT_PRIMARY_FRESH = "never prompted"
NOT_PRIMARY_GONE = "finished"


def _mark_primary(sessions: list[SessionState]) -> None:
    """Set `primary` / `primary_reason` on every conversation."""
    by_project: dict[str, list[SessionState]] = {}
    for s in sessions:
        if s.state == GONE:
            s.primary, s.primary_reason = False, NOT_PRIMARY_GONE
        elif s.state == FRESH:
            s.primary, s.primary_reason = False, NOT_PRIMARY_FRESH
        elif s.origin in PROGRAM_ORIGINS:
            s.primary, s.primary_reason = False, NOT_PRIMARY_BACKGROUND
        elif s.origin not in ATTENDED_ORIGINS:
            s.primary, s.primary_reason = False, NOT_PRIMARY_UNPLACED
        else:
            s.primary, s.primary_reason = False, NOT_PRIMARY_BACKGROUND
            by_project.setdefault(s.project, []).append(s)

    for group in by_project.values():
        if len(group) == 1:
            group[0].primary, group[0].primary_reason = True, PRIMARY_ONLY
            continue
        ranked = sorted(
            group,
            key=lambda s: s.since if s.since is not None else float("-inf"),
            reverse=True)
        lead, runner = ranked[0], ranked[1]
        lead_at = lead.since
        runner_at = runner.since
        separated = (lead_at is not None and
                     (runner_at is None or lead_at - runner_at > PRIMARY_MARGIN_SEC))
        if not separated:
            # Inside the margin nothing is claimed. Every member of the tie
            # says so; the ones further back are simply background.
            for s in ranked:
                at = s.since
                tied = at is not None and lead_at is not None and \
                    lead_at - at <= PRIMARY_MARGIN_SEC
                s.primary_reason = PRIMARY_TIED if tied else NOT_PRIMARY_BACKGROUND
            continue
        lead.primary, lead.primary_reason = True, PRIMARY_RECENT
        for s in ranked[1:]:
            s.primary, s.primary_reason = False, NOT_PRIMARY_BACKGROUND


# ── subagents ───────────────────────────────────────────────────────────────

# A subagent transcript written this recently is taken to be working. This is
# a FILE age, stated as one — there is no process to check here, and calling
# it "running" would be a claim the data does not support.
AGENT_ACTIVE_WITHIN_SEC = 90.0
# Cap the stat() calls one conversation can cost a 1 Hz poll. 209 subagent
# transcripts were measured under a single conversation.
MAX_AGENT_FILES = 300


def count_agents(roots, cwd: str, session_id: str,
                 now: float) -> tuple[int, int, bool]:
    """(subagent transcripts seen, how many were written recently, capped?).

    `roots` is one config root or several. Several are deduped BY FILE NAME:
    the two roots are hardlinked on the live machine, so `agent-x.jsonl`
    under each is one agent reached by two names, and unioning the listings
    naively doubles every count on the page.

    The CLI puts one file per dispatched subagent in
    `projects/<encoded cwd>/<sessionId>/subagents/`. Measured 2026-09-03:
    1,573 such files against 947 conversations.

    THE CAP COUNTS TRANSCRIPTS, NOT DIRECTORY ENTRIES. Every `agent-x.jsonl`
    has an `agent-x.json` sidecar beside it, so one live folder held 418
    entries for 209 agents — slicing the raw listing first reported 150,
    which looked exactly like a real number. Filter, then cap.

    BOTH COUNTS ARE FLOORS ONCE THE CAP IS HIT. `seen` obviously; `active`
    just as much, because the slice is taken by FILE NAME and file names are
    uncorrelated with recency — the 300 examined may hold none of the busy
    ones. The third value says so, and both pills must render it.

    Never raises: this folder belongs to another process, and files appear
    and vanish underneath it.
    """
    by_name: dict[str, Path] = {}
    for root in ([roots] if isinstance(roots, (str, Path)) else roots):
        d = (Path(root) / "projects" / encode_cwd(cwd) / session_id
             / "subagents")
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for f in entries:
            if f.name.endswith(".jsonl"):
                by_name.setdefault(f.name, f)

    transcripts = [by_name[name] for name in sorted(by_name)]
    capped = len(transcripts) > MAX_AGENT_FILES
    seen = active = 0
    for f in transcripts[:MAX_AGENT_FILES]:
        try:
            st = f.stat()
        except OSError:
            continue
        if not os.path.isfile(f):
            continue
        seen += 1
        if now - st.st_mtime <= AGENT_ACTIVE_WITHIN_SEC:
            active += 1
    return seen, active, capped


def project_name(cwd: str) -> str:
    """The project name for a cwd, tolerant of Claude Code's own worktree layout.

    Claude Code creates worktrees under `<repo>/.claude/worktrees/<branch>`, so
    a plain `Path(cwd).name` on such a path yields the branch/worktree name
    (e.g. "runs-dashboard") instead of the repo the user actually means (e.g.
    "jarvis"). When the path contains a `.claude/worktrees` segment, the
    project is the directory name that PRECEDES `.claude`.

    This covers Claude Code's own worktree convention ONLY. A worktree created
    elsewhere (via plain `git worktree add`, outside `.claude/worktrees`) is
    still named after its own directory: detecting that properly would need a
    `git` subprocess call, and this runs on a 1-second poll loop, so it is
    deliberately not attempted.

    Two worktrees of the same repo therefore share a project name here, and
    are told apart afterward by the existing voice-naming rules (folder, then
    age) — that is correct and desirable, not a bug.
    """
    parts = Path(cwd).parts
    if ".claude" in parts:
        i = parts.index(".claude")
        if i > 0 and parts[i - 1] and i + 1 < len(parts) and parts[i + 1] == "worktrees":
            return parts[i - 1]
    return Path(cwd).name or cwd


def worktree_branch(cwd: str) -> str:
    """The worktree name in `<repo>/.claude/worktrees/<branch>`, or "".

    The other half of `project_name`: that function answers "which project
    is this", this one answers "which copy of it". Two worktrees of one repo
    share a project name on purpose, so anything that shows both at once
    needs this to tell them apart.
    """
    parts = Path(cwd).parts
    if ".claude" not in parts:
        return ""
    i = parts.index(".claude")
    if i > 0 and i + 2 < len(parts) and parts[i + 1] == "worktrees":
        return parts[i + 2]
    return ""


# ── who is at the other end ─────────────────────────────────────────────────
#
# Every roster entry carries the `entrypoint` the CLI was started with
# (`CLAUDE_CODE_ENTRYPOINT`), and that is the one thing on disk that says who
# answers the session's prompts: a person at a terminal, a person in the
# desktop app or an editor, or a PROGRAM — the Agent SDK, `claude -p`, an MCP
# client, a CI action — that started it and takes its prompts over stdio.
#
# This table used to hold four entries and call everything else a terminal.
# Measured live, 2026-09-28: Paperclip drove a session in stark-armory-next
# through the TypeScript Agent SDK (`sdk-ts`, launched by `claude-agent-acp`
# with `--permission-prompt-tool stdio`). Paperclip approved each of its
# permission prompts itself, in 0.06 to 2.34 seconds; JARVIS, reading
# "terminal", announced six of them in seven minutes as needing the user's
# own keystroke, and crowned the session "the only live conversation here".
#
# The keys are the CLI's own list (`Vin` in the 2.1.270 binary), every one
# placed. A value the list does not have is `OTHER` — neither a person's
# session nor a program's is claimed for it. A missing `entrypoint` is
# already `cli` by the time it gets here (`_parse_entry`): older CLIs wrote
# none, and they were terminals.
_ORIGIN_BY_ENTRYPOINT = {
    "cli": TERMINAL,
    "claude-desktop": DESKTOP,
    "claude-desktop-3p": DESKTOP,
    "local-agent": DESKTOP,
    "local_agent": DESKTOP,
    "claude-vscode": EDITOR,
    "remote": REMOTE,
    "remote_baku": REMOTE,
    "remote_cowork": REMOTE,
    "remote_desktop": REMOTE,
    "remote_mobile": REMOTE,
    "ssh-remote": REMOTE,
    "claude_in_slack": REMOTE,
    "claude-in-slack": REMOTE,
    "claude-in-teams": REMOTE,
    "sdk-cli": BACKGROUND,
    "sdk-ts": BACKGROUND,
    "sdk-py": BACKGROUND,
    "mcp": BACKGROUND,
    "claude-code-github-action": BACKGROUND,
    "bench": BACKGROUND,
    "remote_trigger": BACKGROUND,
    "remote_cowork_trigger": BACKGROUND,
    # Not in the CLI's list; the previous table carried it, and a desktop
    # session is what it would mean.
    "desktop": DESKTOP,
}

# A person answers these sessions' prompts, wherever they are shown.
ATTENDED_ORIGINS = frozenset({TERMINAL, DESKTOP, EDITOR, REMOTE})
# The program that started these answers them — over stdio, by its own policy
# or its own UI. Nothing JARVIS or the user can press reaches them.
PROGRAM_ORIGINS = frozenset({BACKGROUND})

# How long a program may sit on one of its session's prompts before it is the
# user's business. The longest answer measured from a real host was 2.34 s;
# past a minute the host is either waiting on a person in its own UI (an
# editor speaking ACP asks there) or has hung, and either way the user is the
# only one left who can move it — so the user is told, once, as the program's
# prompt. Short enough that a hung host does not hide its session for long.
HOST_ANSWER_GRACE_SEC = 60.0


def _origin(entry: RosterEntry) -> str:
    return _ORIGIN_BY_ENTRYPOINT.get(entry.entrypoint, OTHER)


def brain_cwd() -> str:
    """Where JARVIS's own brain process runs: `data_paths.brain_home()`
    (`<JARVIS_DATA_DIR>/jarvis`), computed here WITHOUT importing
    `data_paths` and without its `mkdir` side effect.

    This module stays pure filesystem *reading* — it is polled once a second
    — so it must not create directories as a side effect of building a
    snapshot, which `data_paths.data_dir()` does. The formula is kept in
    exact lockstep with `data_paths.brain_home()` by
    `test_brain_cwd_matches_data_paths_brain_home` in the test suite.
    """
    raw = os.getenv("JARVIS_DATA_DIR")
    base = Path(raw).expanduser() if raw else (Path(__file__).parent / "data")
    return str(base / "jarvis")


# The private spelling this module has always used. `usage_scan` needs the
# same answer — the brain's transcript is not the user's work either — and
# reaching into another module's underscore would be worse than one alias.
_brain_cwd = brain_cwd


def _is_own_brain(entry: RosterEntry) -> bool:
    """True for JARVIS's own brain process, never a user conversation.

    Measured live: the brain registers with `entrypoint="sdk-cli"` and a cwd
    exactly equal to the brain home directory. The cwd match is checked
    FIRST and is the deciding signal — a roster `name` of "jarvis" is NOT
    used, because a user can legitimately have a project called "jarvis"
    (this user does, at .../Projects/jarvis) and a name-based check would
    wrongly exclude that real conversation too.
    """
    if entry.entrypoint != "sdk-cli":
        return False
    try:
        return _same_dir(entry.cwd, brain_cwd())
    except (TypeError, ValueError):
        return False


def _same_dir(a: str, b: str) -> bool:
    """Two spellings of one directory.

    `realpath`, not a textual compare: `JARVIS_DATA_DIR` pointing at a
    symlink — the ordinary shape when data lives on another volume — spells
    the brain's home one way here and another way in the roster entry the
    process wrote, and the brain then appeared as one of the user's own
    conversations. `realpath` does not require the path to exist.
    """
    if not a or not b:
        return False
    return os.path.realpath(a) == os.path.realpath(b)


def _pick_primary(entries: list[RosterEntry]) -> RosterEntry:
    """Which process do we steer? Alive first, then one with a live socket,
    then the most recently active."""
    return sorted(
        entries,
        key=lambda e: (pid_alive(e.pid), e.steerable, e.status_updated_at or 0.0),
        reverse=True,
    )[0]


def _host_holds(entry: RosterEntry, now: float) -> bool:
    """Is this wait still its host program's to answer?

    Only a program's session has a host, and only inside the grace. With no
    `statusUpdatedAt` there is no telling how long the host has had it, and
    absence of evidence is not a long wait: the user is not interrupted on
    a guess.
    """
    if _origin(entry) not in PROGRAM_ORIGINS:
        return False
    began = entry.status_updated_at
    return began is None or now - began < HOST_ANSWER_GRACE_SEC


def _derive_state(entries: list[RosterEntry], recap: Recap,
                  now: float) -> tuple[str, str | None, str | None]:
    """The conversation's state, the reason it needs you if it does, and the
    reason it is waiting on its host program if it is doing that instead.

    At most one of the two reasons is set. A wait in a PROGRAM's session is
    the program's: the session reads `working` — the program is driving it,
    nothing is asked of the user — and `waiting_on_host` says what it paused
    on (`""` when the roster named nothing). Past `HOST_ANSWER_GRACE_SEC` it
    is the user's, as any other wait is.
    """
    if not any(pid_alive(e.pid) for e in entries):
        return GONE, None, None
    if not recap.exists:
        # Nobody has ever prompted it. True even when it sits at a startup
        # dialog — measured on chitauri-67, which was `waiting`/`dialog open`.
        return FRESH, None, None

    live = [e for e in entries if pid_alive(e.pid)]
    waiting = next((e for e in live if e.waiting_for), None) or \
        next((e for e in live if e.status == "waiting"), None)
    if waiting is not None:
        if _host_holds(waiting, now):
            return WORKING, None, waiting.waiting_for or ""
        return NEEDS_YOU, waiting.waiting_for, None
    if any(e.status == "busy" for e in live):
        return WORKING, None, None
    if any(e.status == "shell" for e in live):
        return SHELL, None, None
    if all(e.status is None for e in live):
        return UNKNOWN, None, None
    # Idle, but it may have stopped to ask something. A program's session
    # asked its PROGRAM, which is what reads its result: that is not the
    # user being wanted, however the sentence ends.
    if all(_origin(e) in PROGRAM_ORIGINS for e in live):
        return IDLE, None, None
    if any(t in _QUESTION_TOOLS for t in recap.recent_tools) or \
            _looks_like_a_question(recap.last_text):
        return NEEDS_YOU, None, None
    return IDLE, None, None


# A thread's own name, as the one wall every voice name meets downstream
# (`server._VOICE_NAME_RE`) will admit it: word characters, spaces and light
# punctuation, beginning and ending on a word character, at most sixty-four
# characters. Cut to that HERE, where the name is made, rather than left for
# the wall to refuse — a refused name is said as "that session", which is the
# very thing this name exists to fix. tests/test_header_lines.py drives what
# this produces through the real wall.
_SAYABLE_MAX = 64
_UNSAYABLE_RE = re.compile(r"[^\w ,.\-/+']+")
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'"})
# Room left for the connective tissue `_name_group` puts round a name two
# threads share: "the {name} in an unclear state" is 24 characters of it.
_SHARED_NAME_MAX = 40


def _clip_words(text: str, limit: int) -> str:
    """`text` without leading or trailing punctuation, cut to `limit` at a
    space — or hard, when one word is longer than the whole limit."""
    text = re.sub(r"\A\W+|\W+\Z", "", text)
    if len(text) <= limit:
        return text
    head = text[:limit + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head else text[:limit]
    return re.sub(r"\W+\Z", "", cut)


def _sayable_name(text) -> str | None:
    """A thread's name as a sentence can hold it, or None if nothing of it
    survives. "Fix: the login redirect" is said "Fix the login redirect";
    quotes, brackets, colons and every line separator become spaces."""
    if not isinstance(text, str):
        return None
    text = text.translate(_APOSTROPHES).replace("&", " and ")
    text = " ".join(_UNSAYABLE_RE.sub(" ", text).split())
    return _clip_words(text, _SAYABLE_MAX) or None


def _thread_name(entry: RosterEntry) -> str | None:
    """The name the user knows this thread by, or None.

    Measured live, 2026-09-30: the Claude desktop app writes the thread's
    sidebar name into the roster's `name` — "Tell brain turns apart from
    cross-session wakes", `nameSource: "user"`, and one with no `nameSource`
    at all — while every thread it starts without a folder runs in a scratch
    folder named like `scratch-2026-09-24-1a3f1e`. Named by that folder, two
    threads were announced as "the newest" and "the second"
    scratch-2026-09-24-1a3f1e, and the user could tell neither apart nor say
    either back.

    A terminal session's `name` is the CLI's own ("chitauri-67",
    `nameSource: "derived"`): not chosen by anyone and not sayable, so it is
    never used — and a name with no source that is just the folder, or the
    folder plus a short suffix, is taken for one of those too.
    """
    name = _sayable_name(entry.given_name)
    if name is None:
        return None
    lowered = name.casefold()
    for folder in {Path(entry.cwd).name.casefold(),
                   project_name(entry.cwd).casefold()}:
        if folder and (lowered == folder or re.fullmatch(
                re.escape(folder) + r"-[a-z0-9]{1,8}", lowered)):
            return None
    return name


def _assign_voice_names(sessions: list[SessionState]) -> None:
    """Give every conversation a name a person can say and hear.

    A thread with a name of its own is called by it (`_thread_name`) — that
    is what the user sees in the Claude app and what he will say back. Only
    the rest are named by their folder.

    Collisions measured on the live machine: `hammer` had two conversations in
    ONE directory (so the folder cannot disambiguate) and `chitauri` had three
    across TWO directories (so the folder can). Roster suffixes like `-4b` are
    never used: they are neither sayable nor hearable.
    """
    # A thread named exactly what some project here is called would share
    # that project's spoken name, and "which one?" would have no answer the
    # user could give. Such a thread is named by its folder instead.
    projects = {s.project.casefold() for s in sessions}
    by_thread: dict[str, list[SessionState]] = {}
    by_folder: list[SessionState] = []
    for s in sessions:
        # Named afresh: a session carried forward, or re-named once runs are
        # excluded, may be named by its folder this time.
        s.thread_part = None
        key = s.thread_name.casefold() if s.thread_name else None
        if key and key not in projects:
            by_thread.setdefault(key, []).append(s)
        else:
            by_folder.append(s)

    for group in by_thread.values():
        if len(group) == 1:
            group[0].voice_name = group[0].thread_part = group[0].thread_name
            continue
        # Two threads given one name ("New conversation"): that name is the
        # base, and the chain below tells them apart as it does a folder.
        # The oldest thread's spelling, so the name does not flip with the
        # order the roster happened to be listed in.
        eldest = min(group, key=lambda s: (s.started or 0.0, s.session_id))
        base = _clip_words(eldest.thread_name, _SHARED_NAME_MAX)
        _name_group(group, base)
        for s in group:
            s.thread_part = base

    _name_by_folder(by_folder)


def _name_by_folder(sessions: list[SessionState]) -> None:
    """Name conversations after their project folder: alone it is the bare
    project name, and several share it in the ways `_name_group` says."""
    by_project: dict[str, list[SessionState]] = {}
    for s in sessions:
        by_project.setdefault(s.project, []).append(s)

    for project, group in by_project.items():
        if len(group) == 1:
            group[0].voice_name = project
            continue
        by_parent: dict[str, list[SessionState]] = {}
        for s in group:
            by_parent.setdefault(Path(s.cwd).parent.name, []).append(s)
        if len(by_parent) > 1:
            # The parent folder tells them apart — "chitauri in Desktop" vs
            # "chitauri in Projects". A folder may still hold several, so those
            # get the same fallback chain applied on top.
            for parent, sub in by_parent.items():
                base = f"{project} in {parent}"
                if len(sub) == 1:
                    sub[0].voice_name = base
                else:
                    _name_group(sub, base)
        else:
            _name_group(group, project)


def _name_group(group: list[SessionState], base: str) -> None:
    """Name several conversations that share a `base` (project, or "project
    in parent"). Tries, in order, the strongest distinguisher that actually
    distinguishes them: what they are ABOUT, then what they are DOING, and
    only as a last resort how OLD they are. Age was the original — and
    weakest — rule; a user was told "the newer one" / "the older one" with no
    hint of which conversation was which. Each rule applies to the WHOLE
    group at once: a group is never named half by topic and half by state,
    because that would make one half's names orphan the other half's
    convention when the user answers back.
    """
    if _name_by_topic(group, base):
        return
    if _name_by_state(group, base):
        return
    _name_by_age(group, base)


# Generic words that carry no topic of their own, stripped from a title
# before a short phrase is pulled from what's left.
_TITLE_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "for", "of", "in", "on", "with",
    "at", "by", "from", "is", "are", "this", "that", "it", "its", "your",
    "my", "our",
})


def _topic_words(project: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", project.lower()) if w}


def _topic_phrase(title: str, project: str) -> str | None:
    """A short (<=2 word), sayable phrase distilled from a conversation's
    title: generic stopwords and the project's own name are stripped out,
    and the last couple of remaining words are kept — the part of a title
    that tends to carry what's actually distinct about it ("...architecture
    and handoff" -> "architecture handoff"). None if nothing is left to
    distinguish it by.
    """
    tokens = [w for w in re.split(r"[^a-z0-9]+", title.lower()) if w]
    proj_words = _topic_words(project)
    kept = [w for w in tokens if w not in _TITLE_STOPWORDS and w not in proj_words]
    if not kept:
        return None
    return " ".join(kept[-2:])


def _name_by_topic(group: list[SessionState], base: str) -> bool:
    """Name every conversation in the group by what it's ABOUT.

    Succeeds only when EVERY member has a non-empty title AND the phrases
    pulled from those titles are pairwise distinct — a title missing on even
    one member, or two titles boiling down to the same phrase, means topic
    naming cannot tell the whole group apart, so nothing is assigned and the
    caller falls through to the next rule for the group as a whole.
    """
    phrases: dict[str, str] = {}
    for s in group:
        title = (s.title or "").strip()
        if not title:
            return False
        phrase = _topic_phrase(title, s.project)
        if not phrase:
            return False
        phrases[s.session_id] = phrase
    if len(set(phrases.values())) != len(phrases):
        return False
    names = {sid: f"{base}, the {phrase} one" for sid, phrase in phrases.items()}
    # Two title words have no length of their own; a name longer than the
    # wall admits is said as "that session", which tells the group apart by
    # nothing. State or age can still do it.
    if any(len(name) > _SAYABLE_MAX for name in names.values()):
        return False
    for s in group:
        s.voice_name = names[s.session_id]
    return True


# Short, sayable phrases for what a conversation is doing right now, used
# when topic naming can't tell a group apart (title missing or identical)
# but their states differ.
_STATE_PHRASES = {
    NEEDS_YOU: "that needs you",
    WORKING: "that's working",
    IDLE: "that's idle",
    SHELL: "in a shell",
    GONE: "that's finished",
    FRESH: "that hasn't started",
    UNKNOWN: "in an unclear state",
}


def _name_by_state(group: list[SessionState], base: str) -> bool:
    """Name every conversation in the group by what it is DOING.

    Succeeds only when every member's state is pairwise distinct — two
    conversations both sitting idle are not told apart by state either, so
    the caller falls through to age for the group as a whole.
    """
    states = [s.state for s in group]
    if len(set(states)) != len(states):
        return False
    for s in group:
        phrase = _STATE_PHRASES.get(s.state, "in an unclear state")
        s.voice_name = f"the {base} {phrase}"
    return True


def _name_by_age(group: list[SessionState], base: str) -> None:
    """Two conversations in one folder: 'the newer hammer' / 'the older hammer'.
    Beyond two, fall back to ordinals, which are still sayable. The last
    resort of the naming chain — it carries no information about what a
    conversation IS, only how old it is."""
    ordered = sorted(group, key=lambda s: s.started or 0.0, reverse=True)
    if len(ordered) == 2:
        ordered[0].voice_name = f"the newer {base}"
        ordered[1].voice_name = f"the older {base}"
        return
    words = ("newest", "second", "third", "fourth", "fifth")
    for i, s in enumerate(ordered):
        s.voice_name = (f"the {words[i]} {base}" if i < len(words)
                        else f"{base} number {i + 1}")


# Words a person says around a name that carry no identity of their own.
# "the newer one" distinguishes; "the", "one", "session" do not.
_FILLER = frozenset({
    "the", "a", "an", "one", "ones", "session", "sessions", "conversation",
    "conversations", "project", "in", "on", "at", "my", "please", "that",
    "this", "it", "s", "lets", "let", "go", "with", "use", "about",
    # JARVIS says a thread as "the thread “…”" (`server._said_name`), and
    # whoever says it back says it the same way.
    "thread", "threads",
})


def _name_words(s: "SessionState") -> set[str]:
    """Every word that could identify this conversation out loud."""
    import re as _re
    text = f"{s.voice_name} {s.project}".lower()
    return {w for w in _re.split(r"[^a-z0-9-]+", text) if w}


def _prefer_real(matches: list[SessionState]) -> list[SessionState]:
    """Among several name/project matches, a `fresh` conversation (never
    prompted, no transcript) is noise, not a real candidate — offering it
    alongside a real conversation turns "ask chitauri where we left off"
    into a clarifying question about a session with nothing to report.

    Drop the fresh ones IF at least one match is real. If EVERY match is
    fresh, keep them all: the user may genuinely mean a fresh session, and
    silently returning nothing would be wrong. And when several matches are
    real, this changes nothing — ambiguity among real conversations must
    still return all of them so the caller asks; that is a safety property,
    not a rough edge to smooth over.
    """
    real = [s for s in matches if s.state != FRESH]
    return real if real else matches


@dataclass
class Snapshot:
    """Every conversation on this machine at one instant."""
    sessions: list[SessionState] = field(default_factory=list)
    taken_at: float = 0.0

    def by_id(self, session_id: str) -> SessionState | None:
        return next((s for s in self.sessions if s.session_id == session_id), None)

    def excluding(self, session_ids) -> "Snapshot":
        """This snapshot without those conversations, RE-DERIVED.

        For JARVIS's own `claude -p` runs, which register in the roster like
        anything else (measured 2026-09-04: `entrypoint: "sdk-cli"`,
        `kind: "interactive"`). Dropping the rows is not enough on its own,
        because the two things computed ACROSS a project — the voice name and
        the "main" badge — were computed with the runs still in. A user with
        one conversation in a project a run had also touched was told about
        "the newer" and "the older" one, and then only one of them survived
        the filter.

        The same snapshot object comes back when nothing is excluded, so the
        common case costs nothing.
        """
        ids = set(session_ids)
        if not ids:
            return self
        kept = [s for s in self.sessions if s.session_id not in ids]
        if len(kept) == len(self.sessions):
            return self
        _assign_voice_names(kept)
        _mark_primary(kept)
        kept.sort(key=lambda s: (s.project, s.voice_name))
        return Snapshot(sessions=kept, taken_at=self.taken_at)

    def by_project(self) -> dict[str, list[SessionState]]:
        out: dict[str, list[SessionState]] = {}
        for s in self.sessions:
            out.setdefault(s.project, []).append(s)
        return out

    def needing_you(self) -> list[SessionState]:
        """Waiting conversations, most-recently-waiting first.

        Ordered by `since` (when the CURRENT STATE began) — NOT `started`
        (when the conversation began). Those two fields differ by up to 102
        hours on the live roster; sorting by `started` here would surface a
        conversation that has sat waiting for days ahead of one that started
        waiting a minute ago.
        """
        return sorted(
            (s for s in self.sessions if s.state == NEEDS_YOU),
            key=lambda s: s.since if s.since is not None else float("-inf"),
            reverse=True)

    def resolve(self, reference: str, last_mentioned: str | None = None
                ) -> list[SessionState]:
        """Map what the user said to conversations.

        Returns EVERY candidate when ambiguous. The caller must ask; picking
        `.first` steers the wrong session and cannot be undone.
        """
        ref = " ".join((reference or "").lower().split())
        if not ref:
            return []
        if ref in ("that one", "that", "it", "the same one", "that session"):
            s = self.by_id(last_mentioned) if last_mentioned else None
            return [s] if s else []

        exact = [s for s in self.sessions if s.voice_name.lower() == ref]
        if exact:
            return exact
        sid = [s for s in self.sessions if s.session_id == reference]
        if sid:
            return sid
        roster = [s for s in self.sessions if s.roster_name.lower() == ref]
        if roster:
            return roster

        # Exact PROJECT match before any substring fallback. Verified live:
        # with two `hammer` conversations plus a separate `hammer-private`
        # project, substring matching alone made "hammer" a hit inside
        # "hammer-private" too, offering an unrelated sibling project. An
        # exact project-name match (case-insensitive) is unambiguous and
        # must win outright, without also pulling in substring siblings.
        exact_project = [s for s in self.sessions if s.project.lower() == ref]
        if exact_project:
            return _prefer_real(exact_project)

        # A qualified answer to our OWN disambiguation question must resolve.
        #
        # Found live: asked "which one?", the user said "the newer one", and
        # the substring rule below matched BOTH candidates again — the project
        # name is inside every one of its own voice names — so the question
        # looped forever and JARVIS said he could not pick. The ask-which-one
        # flow was a dead end.
        #
        # So: if every distinguishing word the user said appears in a voice
        # name, that name is a candidate, and the narrowest set wins. "newer
        # webapp-fresh" keeps only "the newer webapp-fresh"; a bare
        # "webapp-fresh" still matches both and still asks.
        ref_words = {w for w in re.split(r"[^a-z0-9-]+", ref) if w and w not in _FILLER}
        if ref_words:
            token_hits = [s for s in self.sessions
                          if ref_words <= _name_words(s)]
            if token_hits:
                narrowed = _prefer_real(token_hits)
                if len(narrowed) < len(self.sessions):
                    return narrowed

        # Substring, both directions: "the chitauri one" contains "chitauri",
        # and "hammer" is contained in "the newer hammer".
        loose = [s for s in self.sessions
                 if s.voice_name.lower() in ref or s.project.lower() in ref
                 or ref in s.voice_name.lower()]
        return _prefer_real(loose)


def build_snapshot(entries: list[RosterEntry] | None = None,
                   roots: list[Path] | None = None,
                   now: float | None = None) -> Snapshot:
    """Read everything and reduce it to conversations.

    Processes are grouped by `sessionId`: measured live, 17 processes were 14
    conversations. The conversation is the unit JARVIS names and counts.
    """
    import time as _time
    if entries is None:
        entries = read_roster(roots)
    # JARVIS's own brain registers in the roster like any Claude Code
    # session; it is never one of the user's conversations. Filtered here
    # (not in `read_roster`, whose contract is EVERY well-formed entry) so
    # every caller of `build_snapshot` gets user-facing conversations only.
    entries = [e for e in entries if not _is_own_brain(e)]

    grouped: dict[str, list[RosterEntry]] = {}
    for e in entries:
        grouped.setdefault(e.session_id, []).append(e)

    sessions: list[SessionState] = []
    at = now if now is not None else _time.time()
    for session_id, group in grouped.items():
        primary = _pick_primary(group)
        # EVERY root the conversation registered in, not just the primary's.
        # `_pick_primary` chooses by liveness and recency and knows nothing
        # about which root holds the transcript, so a session registered in
        # both roots with its file under the second one read as `fresh` —
        # "not started" — which is the one state JARVIS never announces.
        roots_here = _roots_of(group, primary)
        recap = _first_recap(roots_here, primary.cwd, session_id)
        state, needs, waiting_on_host = _derive_state(group, recap, at)
        agents_seen, agents_active, agents_capped = count_agents(
            roots_here, primary.cwd, session_id, at)
        live = [e for e in group if pid_alive(e.pid)]
        steerable_entry = next((e for e in live if e.steerable), None)
        sessions.append(SessionState(
            session_id=session_id,
            cwd=primary.cwd,
            project=project_name(primary.cwd),
            state=state,
            pids=[e.pid for e in group],
            primary_pid=primary.pid,
            roster_name=primary.name,
            thread_name=_thread_name(primary),
            needs=needs,
            title=recap.title,
            last_prompt=recap.last_prompt,
            last_text=recap.last_text,
            recent_tools=recap.recent_tools,
            started=(primary.started_at or primary.status_updated_at),
            since=(primary.status_updated_at or primary.started_at),
            origin=_origin(primary),
            entrypoint=primary.entrypoint,
            waiting_on_host=waiting_on_host,
            steerable=steerable_entry is not None,
            socket_path=steerable_entry.socket_path if steerable_entry else None,
            agents_seen=agents_seen,
            agents_active=agents_active,
            agents_capped=agents_capped,
        ))

    _assign_voice_names(sessions)
    _mark_primary(sessions)
    sessions.sort(key=lambda s: (s.project, s.voice_name))
    return Snapshot(sessions=sessions, taken_at=at)


# A conversation that has died is kept this long so a completion can still be
# announced after the process is gone.
GONE_RETENTION_SEC = 600.0
# Work shorter than this is a flicker, not a job worth announcing.
MIN_WORK_SEC = 30.0


class SessionWatcher:
    """Polls the roster and publishes only the transitions worth speaking about.

    Startup is silent by construction: the first poll fills `_previous` without
    emitting, so JARVIS never greets you by reciting the fourteen sessions that
    were already open.
    """

    def __init__(self, roots: list[Path] | None = None, interval: float = 1.0):
        self.roots = roots
        self.interval = interval
        self.snapshot = Snapshot()
        self._previous: dict[str, str] = {}          # session_id -> state
        self._working_since: dict[str, float] = {}
        self._gone_at: dict[str, float] = {}
        self._gone_cache: dict[str, SessionState] = {}
        self._subscribers: list = []
        self._started = False
        self._task = None
        # Captured by start() so _publish() can tell whether it is running on
        # the loop's own thread. Kept as a pair (not just the loop) because
        # asyncio.AbstractEventLoop exposes no public "which thread owns me"
        # check.
        self._event_loop = None
        self._loop_thread = None

    def on_event(self, callback) -> None:
        self._subscribers.append(callback)

    def _publish(self, kind: str, session: SessionState, at: float) -> None:
        """Notify every subscriber, always on the event-loop thread.

        Subscribers (e.g. server._on_session_event) commonly call
        asyncio.create_task, which requires a running event loop on the
        CALLING thread. Polling happens off that thread: _loop() drives
        poll_once() via asyncio.to_thread(...), so _publish() is invoked from
        a worker thread. Calling subscribers inline there made create_task
        raise RuntimeError("no running event loop") -- and because that
        exception was swallowed by the try/except below, the watcher looked
        healthy while /ws/sessions never delivered a single event. Route each
        callback onto the loop thread with call_soon_threadsafe whenever a
        loop was captured by start(); only call inline when no loop was
        captured, i.e. poll_once() driven directly (as the sync tests do).
        """
        import threading
        event = {"kind": kind, "at": at, "session": session_to_dict(session)}

        def _invoke(cb) -> None:
            try:
                cb(event)
            except Exception:            # one bad subscriber must not stop the rest
                import logging
                logging.getLogger(__name__).warning(
                    "session event subscriber failed", exc_info=True)

        loop = self._event_loop
        on_loop_thread = loop is not None and threading.current_thread() is self._loop_thread
        for cb in list(self._subscribers):
            if loop is not None and not on_loop_thread:
                loop.call_soon_threadsafe(_invoke, cb)
            else:
                _invoke(cb)

    def poll_once(self, now: float | None = None) -> Snapshot:
        import time as _time
        now = now if now is not None else _time.time()
        snap = build_snapshot(roots=self.roots, now=now)

        # Carry recently-dead conversations forward so a completion can still
        # be announced after the process has exited.
        present = {s.session_id for s in snap.sessions}
        for sid, cached in list(self._gone_cache.items()):
            if sid in present:
                del self._gone_cache[sid]
                self._gone_at.pop(sid, None)
                continue
            if now - self._gone_at.get(sid, now) > GONE_RETENTION_SEC:
                del self._gone_cache[sid]
                self._gone_at.pop(sid, None)
                self._previous.pop(sid, None)
                self._working_since.pop(sid, None)
            else:
                snap.sessions.append(cached)
        for sid in list(self._previous):
            if sid not in present and sid not in self._gone_cache:
                prior = self.snapshot.by_id(sid)
                if prior is not None:
                    gone = SessionState(**{**prior.__dict__, "state": GONE,
                                           "steerable": False, "socket_path": None})
                    self._gone_cache[sid] = gone
                    self._gone_at[sid] = now
                    snap.sessions.append(gone)

        # Every session PUBLISHED must have been through naming together.
        # `build_snapshot()` already named the live sessions before any of
        # them were known here; gone-cache entries retained above (or just
        # created above) still carry the voice_name assigned in an EARLIER
        # poll, computed without knowledge of whoever is live now. A session
        # that returns from the gone-cache under a fresh session_id can
        # therefore collide with a live conversation's name unless naming is
        # redone across the full, final set.
        _assign_voice_names(snap.sessions)
        # Same reason naming is redone here: a conversation carried forward
        # from the gone-cache was ranked against whoever was live in an
        # EARLIER poll. Primary is a comparison, so it must be recomputed
        # over the final set or a dead session can still hold the crown.
        _mark_primary(snap.sessions)
        snap.sessions.sort(key=lambda s: (s.project, s.voice_name))

        first_poll = not self._started
        self.snapshot = snap                      # snapshot BEFORE notifying
        self._started = True

        for s in snap.sessions:
            was = self._previous.get(s.session_id)
            if s.state == WORKING and was != WORKING:
                self._working_since[s.session_id] = now

            if not first_poll and s.announceable:
                if s.state == NEEDS_YOU and was != NEEDS_YOU:
                    self._publish("needs_you", s, now)
                elif was == WORKING and s.state in (IDLE, GONE, UNKNOWN):
                    # A session that exits while its roster entry is still
                    # being torn down can land on `unknown` (no status field
                    # at all) rather than cleanly on `gone` — treat that the
                    # same as `idle`/`gone` for the "finished" announcement,
                    # or a session that exits this way is never reported.
                    started = self._working_since.get(s.session_id)
                    if started is not None and now - started >= MIN_WORK_SEC:
                        self._publish("finished", s, now)
            self._previous[s.session_id] = s.state

        return snap

    async def start(self) -> None:
        import asyncio
        import threading
        if self._task is not None:
            return
        self._event_loop = asyncio.get_running_loop()
        self._loop_thread = threading.current_thread()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        import asyncio
        while True:
            try:
                await asyncio.to_thread(self.poll_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging
                logging.getLogger(__name__).warning("watch tick failed", exc_info=True)
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        import asyncio
        task, self._task = self._task, None
        # Clear unconditionally: a restarted watcher must never hold a loop
        # (or thread reference) belonging to an event loop that is gone.
        self._event_loop = None
        self._loop_thread = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass                          # expected: this is our own cancel()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "session watcher did not stop cleanly", exc_info=True)


def session_to_dict(s: SessionState) -> dict:
    """The JSON shape used by /api/sessions, /ws/sessions, and the tools."""
    return {
        "session_id": s.session_id,
        "voice_name": s.voice_name,
        "roster_name": s.roster_name,
        "thread_name": s.thread_name,
        "thread_part": s.thread_part,
        "project": s.project,
        "cwd": s.cwd,
        "state": s.state,
        "needs": s.needs,
        "needs_a_human_hand": s.needs_a_human_hand,
        "title": s.title,
        "summary": s.summary(),
        "last_prompt": s.last_prompt,
        "last_text": s.last_text,
        "recent_tools": list(s.recent_tools),
        # TWO stamps, never one. `started` is when the conversation began;
        # `since` is when its CURRENT STATE began. The largest gap measured
        # on the live roster was 102 HOURS, so anything that shows one where
        # it means the other is wrong by days and looks entirely plausible.
        # Either may be None: an absent stamp is an absence of evidence, and
        # 0 would render as 1970 and read as a measurement.
        "started": s.started,
        "since": s.since,
        "origin": s.origin,
        # The roster's own word for how it was started ("sdk-ts"), beside
        # the origin JARVIS derived from it, so a reader can check the one
        # against the other.
        "entrypoint": s.entrypoint,
        # Set only while a program's session waits on that program — a
        # `working` session, paused on something it is not asking the user.
        "waiting_on_host": s.waiting_on_host,
        "steerable": s.steerable,
        "pids": list(s.pids),
        "primary_pid": s.primary_pid,
        # The verdict AND the reason. A bare boolean is a claim; the reason
        # is what lets a reader check it — including when it says nobody here
        # is clearly the main one.
        "primary": s.primary,
        "primary_reason": s.primary_reason,
        "agents_seen": s.agents_seen,
        "agents_active": s.agents_active,
        "agents_capped": s.agents_capped,
    }
