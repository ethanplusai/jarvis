"""Per-session token usage, read off Claude Code's own transcripts.

WHY THIS EXISTS
---------------
`usage_store` holds what the CLI told us about the SUBSCRIPTION's limits —
the five-hour and seven-day windows. That is the whole picture of "how close
am I to being cut off", and none of the picture of "what did each
conversation actually cost". The second question is only answerable from the
transcripts, because that is the only place a per-message `usage` block is
written down.

WHAT IS ON DISK
---------------
Two config roots must be read (`~/.claude` and `~/.claude-orcha`; see
`session_watch.config_roots`). Under each:

    projects/<encoded cwd>/<sessionId>.jsonl
        the conversation. Every `assistant` line carries
        `message.usage.{input,output,cache_read_input,cache_creation_input}_tokens`
        and `message.model`.

    projects/<encoded cwd>/<sessionId>/subagents/agent-<agentId>.jsonl
        one file per subagent that conversation dispatched. Same `sessionId`,
        `isSidechain: true`, its own `agentId`. Measured 2026-09-03: 1,573 of
        these against 947 conversations — subagent work is the majority of
        the files on this machine and most of nobody's picture of usage.

THE TRAPS, ALL THREE MEASURED ON THIS MACHINE
---------------------------------------------
1. THE ROOTS ARE HARDLINKED. `~/.claude/projects/…/x.jsonl` and
   `~/.claude-orcha/projects/…/x.jsonl` were the same inode, link count 2.
   Unioning the roots without deduping by (st_dev, st_ino) doubles every
   token on the page. Dedupe is by inode, not by filename, because two roots
   may also legitimately hold two different conversations.

2. THE CORPUS IS HUGE. 548 MB in 2,544 files, the largest 95 MB. A cold
   full parse takes ~3 s. So a `Cache` remembers each file's inode, length
   and running totals, and a rescan reads only the appended bytes. A file
   that shrank, or whose inode changed under the same path, is read again
   from zero — resuming into a rewritten file would carry a total that no
   longer describes it.

3. SUMMED INPUT TOKENS ARE NOT THE CONTEXT. Every turn re-sends the
   conversation, so summing `input_tokens` over a long session reports a
   200 k window as several million. The context is the LAST turn's
   input + cache_read + cache_creation, and it is reported separately.

HONESTY
-------
Absence is preserved as absence. A machine with no transcripts reports
`measured: False` and an empty list — never a confident zero. A session whose
transcript holds no assistant turn has `context_tokens: None`, not 0.

And JARVIS's own machinery is not the user's work. His brain (identified by
its cwd, exactly as `session_watch` identifies it) and every run he spawned
(a run id IS its `--session-id`) are reported in their own bucket and never
added into the user's totals.

No I/O beyond reading these files; no server imports; never raises on a file
being written underneath it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import session_watch

log = logging.getLogger("jarvis.usage_scan")

# How recently a subagent transcript must have been written for the agent to
# read as "working". Files are all we have here — there is no process to
# check — so this is always reported alongside the window it means, and the
# word used is "active", never "running".
ACTIVE_WITHIN_SEC = 90.0

# The prompt a subagent was launched with, clipped for a list row.
MAX_PROMPT = 240

# Only lines carrying this substring are handed to the JSON parser. Measured:
# it cuts 292 k lines to 149 k and a 548 MB scan to ~3 s. It is a filter on
# work, never on truth — a line with a usage block always contains it.
_USAGE_MARKER = b'"output_tokens"'


# ── quantities ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Tokens:
    """The four counts the CLI reports, kept apart because they are not
    interchangeable: cache reads are most of a long conversation's input and
    conflating them with fresh input misstates both."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation

    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_creation + other.cache_creation,
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "total": self.total,
        }


ZERO = Tokens()


def _int(raw) -> int:
    """A token count, or 0. `True` is an int in Python and is not a count."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0
    return value if value > 0 else 0


def _tokens_from(usage) -> Tokens:
    if not isinstance(usage, dict):
        return ZERO
    return Tokens(
        _int(usage.get("input_tokens")),
        _int(usage.get("output_tokens")),
        _int(usage.get("cache_read_input_tokens")),
        _int(usage.get("cache_creation_input_tokens")),
    )


# A moment `day_key` can actually name. `datetime.fromtimestamp` converts to
# LOCAL time, so a value near either end of datetime's range lands outside it
# once the UTC offset is applied and raises — measured, on a real stamp:
#
#     _epoch("0001-01-01T00:00:00.000Z")  ->  -62135596800.0
#     day_key(-62135596800.0)             ->  ValueError: year 0 is out of range
#
# `fromisoformat` accepts that string happily, so catching only its ValueError
# left the second call unguarded. Two years of slack at each end covers every
# UTC offset without needing to know the local one.
#
# Windows' localtime() is narrower still: it refuses anything before the
# epoch or past the year 3000, and `.timestamp()` on a naive datetime raises
# OSError for the bounds above before a single stamp is checked. A real
# transcript stamp is always well inside 1970-3000, so clamp to that there.
if sys.platform == "win32":
    _DAY_MIN = 2 * 86400.0
    _DAY_MAX = datetime(2999, 1, 1).timestamp()
else:
    _DAY_MIN = datetime(2, 1, 1).timestamp()
    _DAY_MAX = datetime(9997, 1, 1).timestamp()


def _epoch(stamp) -> float | None:
    """The ISO-8601 UTC timestamp on a transcript line, as epoch seconds.

    None for anything `day_key` could not then name — a timestamp we cannot
    place on a calendar is not a timestamp, and returning it would move the
    failure one line down into arithmetic that has no way to refuse.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    text = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
    try:
        when = datetime.fromisoformat(text).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return when if _DAY_MIN <= when <= _DAY_MAX else None


def day_key(epoch: float) -> str:
    """The local calendar day a moment falls in. Local, not UTC: "today" has
    to mean the user's today or the headline number is wrong every evening."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


# ── one file's running totals ───────────────────────────────────────────────

@dataclass
class FileTotals:
    """Everything read out of one transcript, and where reading stopped.

    Kept per FILE rather than per session so an incremental re-read has
    somewhere to resume, and so a session assembled from several files (its
    conversation plus its subagents) adds up without re-reading any of them.
    """
    dev: int = 0
    ino: int = 0
    offset: int = 0
    tokens: Tokens = ZERO
    turns: int = 0
    by_day: dict[str, Tokens] = field(default_factory=dict)
    by_model: dict[str, Tokens] = field(default_factory=dict)
    first_at: float | None = None
    last_at: float | None = None
    cwd: str = ""
    model: str = ""
    prompt: str = ""
    # The last turn's input+cache: what the model was actually carrying.
    context_tokens: int | None = None

    def copy(self) -> "FileTotals":
        """A scratch copy to parse into, so a half-finished read is thrown
        away rather than left in the cache.

        `Tokens` is frozen and the rest are scalars, so only the two buckets
        need copying — but they need it: sharing them would let an abandoned
        parse write its days and models into the cached entry anyway.
        """
        clone = FileTotals(**vars(self))
        clone.by_day = dict(self.by_day)
        clone.by_model = dict(self.by_model)
        return clone


class Cache:
    """Remembers where each transcript was last read to.

    Hold one of these for the life of the process. It is keyed by path, and
    every entry records the inode it was built from, so a rotated file is
    never resumed into.
    """

    def __init__(self) -> None:
        self._files: dict[str, FileTotals] = {}
        # Subagent sidecars, by path. Written once at spawn and never
        # rewritten, so there is nothing to invalidate.
        self.meta: dict[str, dict] = {}

    def get(self, path: Path) -> FileTotals | None:
        return self._files.get(str(path))

    def put(self, path: Path, totals: FileTotals) -> None:
        self._files[str(path)] = totals

    def forget_missing(self, seen: set[str]) -> None:
        for key in [k for k in self._files if k not in seen]:
            del self._files[key]

    def __len__(self) -> int:
        return len(self._files)


_DEFAULT_CACHE = Cache()


def _add(bucket: dict, key: str, tokens: Tokens) -> None:
    bucket[key] = bucket.get(key, ZERO) + tokens


def scan_file(path: Path, cache: Cache) -> tuple[FileTotals, int, bool]:
    """(totals for this file, bytes read this call, was it readable at all).

    Reads only what has been appended since the last call. A partial final
    line — this file is being written by another process right now — is left
    unconsumed so the next call sees it whole.

    THE THIRD VALUE IS THE HONESTY ONE. A file that could not be stat'd or
    opened — no Full Disk Access, the normal first-run state on macOS —
    contributes nothing, and saying so is the difference between "you have
    used nothing" and "I could not look". `report` counts these, not the
    files it merely walked past.

    THE CURSOR MOVES LAST. Everything is parsed into a scratch copy and only
    committed to the cache once the whole chunk is through, so an exception
    anywhere in the loop costs this call and nothing else. It used to
    advance `prior.offset` — the CACHED object — before parsing a byte, so a
    single unparseable line meant the next scan resumed past bytes nobody
    had read and those turns were gone for the life of the process.
    """
    prior = cache.get(path)
    try:
        st = path.stat()
    except OSError:
        # Transient (a file rotated between the listing and the stat) or
        # permanent. Either way the last thing we knew about this path is
        # still the best answer; a fresh `FileTotals()` would report a
        # measured session as having spent zero until the TTL expired.
        return (prior if prior is not None else FileTotals()), 0, False

    if (prior is not None and prior.dev == st.st_dev and prior.ino == st.st_ino
            and st.st_size >= prior.offset):
        if st.st_size == prior.offset:
            return prior, 0, True           # already read, to the last byte
        totals = prior.copy()
    else:
        # A new file, a rotated one (same path, different inode), or one that
        # shrank. Any resume would be a total that no longer describes it.
        totals = FileTotals(dev=st.st_dev, ino=st.st_ino)

    totals.dev, totals.ino = st.st_dev, st.st_ino

    try:
        with open(path, "rb") as fh:
            fh.seek(totals.offset)
            chunk = fh.read()
    except OSError:
        return (prior if prior is not None else totals), 0, False

    cut = chunk.rfind(b"\n")
    if cut < 0:
        return totals, 0, True              # readable; nothing complete yet
    consumed = chunk[: cut + 1]

    for raw in consumed.split(b"\n"):
        try:
            _scan_line(totals, raw)
        except Exception:
            # One line of somebody else's file, in a shape nobody predicted.
            # It costs itself and nothing around it: the alternative, once
            # measured, was a 503 across the whole Usage page.
            log.debug("unreadable transcript line in %s", path, exc_info=True)

    totals.offset += len(consumed)
    cache.put(path, totals)
    return totals, len(consumed), True


def _scan_line(totals: FileTotals, raw: bytes) -> None:
    """Fold one transcript line into a file's running totals."""
    if _USAGE_MARKER not in raw:
        _note_prompt(totals, raw)
        return
    try:
        line = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(line, dict):
        return
    message = line.get("message")
    if not isinstance(message, dict):
        return
    tokens = _tokens_from(message.get("usage"))
    if tokens.total == 0 and not isinstance(message.get("usage"), dict):
        return

    totals.turns += 1
    totals.tokens = totals.tokens + tokens
    model = message.get("model")
    if isinstance(model, str) and model:
        _add(totals.by_model, model, tokens)
        totals.model = model
    if isinstance(line.get("cwd"), str) and line["cwd"]:
        totals.cwd = line["cwd"]

    when = _epoch(line.get("timestamp"))
    if when is not None:
        _add(totals.by_day, day_key(when), tokens)
        totals.first_at = when if totals.first_at is None \
            else min(totals.first_at, when)
        totals.last_at = when if totals.last_at is None \
            else max(totals.last_at, when)
    # The context is what the LAST REAL request carried. Measured live
    # on session 5a0eaa6f: the final line of a busy transcript was a
    # `<synthetic>` turn with all four counts at zero, and taking it at
    # face value reported a 481k context as 0. A turn that carried
    # nothing was not a request — it is the CLI's own bookkeeping — so
    # it leaves the previous reading standing.
    carried = tokens.input + tokens.cache_read + tokens.cache_creation
    if carried > 0:
        totals.context_tokens = carried


def _note_prompt(totals: FileTotals, raw: bytes) -> None:
    """A subagent's first `user` line is the brief it was dispatched with —
    the only human-readable answer to "what is this agent doing"."""
    if totals.prompt or b'"user"' not in raw or b'"isSidechain":true' not in \
            raw.replace(b", ", b",").replace(b": ", b":"):
        return
    try:
        line = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(line, dict) or line.get("type") != "user":
        return
    content = (line.get("message") or {}).get("content") \
        if isinstance(line.get("message"), dict) else None
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "")
                break
    text = " ".join(text.split())
    if text:
        totals.prompt = text[:MAX_PROMPT]
    if isinstance(line.get("cwd"), str) and line["cwd"] and not totals.cwd:
        totals.cwd = line["cwd"]


# ── discovery ───────────────────────────────────────────────────────────────

@dataclass
class TranscriptSet:
    """Every file belonging to one conversation, across every root."""
    session_id: str
    main: list[Path] = field(default_factory=list)
    agents: dict[str, Path] = field(default_factory=dict)


def discover(roots: list[Path] | None = None) -> dict[str, TranscriptSet]:
    """Map session id -> its files, deduped by inode.

    The two roots are hardlinked on the live machine, so an inode already
    seen is the SAME file reached by a second name, not a second file.
    """
    found: dict[str, TranscriptSet] = {}
    seen: set[tuple[int, int]] = set()

    def claim(path: Path) -> bool:
        try:
            st = path.stat()
        except OSError:
            return False
        key = (st.st_dev, st.st_ino)
        if key in seen:
            return False
        seen.add(key)
        return True

    for root in (roots if roots is not None else session_watch.config_roots()):
        base = Path(root) / "projects"
        try:
            project_dirs = sorted(base.iterdir())
        except OSError:
            continue
        for pdir in project_dirs:
            try:
                entries = sorted(pdir.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.endswith(".jsonl") and entry.is_file():
                    if claim(entry):
                        found.setdefault(
                            entry.stem, TranscriptSet(entry.stem)
                        ).main.append(entry)
                elif entry.is_dir():
                    _claim_agents(entry, found, claim)
    return found


def _claim_agents(session_dir: Path, found: dict, claim) -> None:
    try:
        agent_files = sorted((session_dir / "subagents").iterdir())
    except OSError:
        return
    for f in agent_files:
        if not (f.name.endswith(".jsonl") and f.is_file()) or not claim(f):
            continue
        agent_id = f.stem[len("agent-"):] if f.stem.startswith("agent-") \
            else f.stem
        found.setdefault(
            session_dir.name, TranscriptSet(session_dir.name)
        ).agents[agent_id] = f


# ── the reduced picture ─────────────────────────────────────────────────────

# The sidecar the CLI writes beside every subagent transcript, measured
# 2026-09-03: `{"agentType": "general-purpose", "description": "Research OSS
# agent design skills", "toolUseId": "toolu_…", "parentAgentId": "a177e04…",
# "spawnDepth": 2}`. It is the ONLY place a subagent says what it is — the
# transcript holds the whole prompt and no type at all — and the only place a
# nested agent (spawnDepth > 1) is visible.
#
# The name is `agent-<id>.meta.json`, so `with_suffix(".json")` finds nothing:
# `.meta.json` is two suffixes and Path only replaces the last one. That is
# not hypothetical — this module looked for `.json`, the fixture wrote
# `.json`, and every one of these fields came back empty against the real
# machine while the tests stayed green.
MAX_DESCRIPTION = 120
META_SUFFIX = ".meta.json"


def read_agent_meta(path: Path, cache: Cache) -> dict:
    """The sidecar beside `path`, or empty fields. Written once at spawn, so
    cached by path forever. Never raises: it can be caught mid-write."""
    side = path.with_name(path.stem + META_SUFFIX)
    key = str(side)
    hit = cache.meta.get(key)
    if hit is not None:
        return hit
    try:
        body = json.loads(side.read_text())
    except (OSError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    out = {
        "agent_type": str(body.get("agentType") or ""),
        "description": " ".join(str(body.get("description") or "").split()
                                )[:MAX_DESCRIPTION],
        "parent_agent_id": str(body.get("parentAgentId") or ""),
        "depth": _int(body.get("spawnDepth")),
    }
    cache.meta[key] = out
    return out


@dataclass
class AgentUsage:
    """One subagent this conversation dispatched."""
    agent_id: str
    model: str
    tokens: Tokens
    turns: int
    first_at: float | None
    last_at: float | None
    prompt: str
    active: bool
    # From the sidecar; empty when there isn't one (older transcripts).
    agent_type: str = ""
    description: str = ""
    parent_agent_id: str = ""
    depth: int = 0

    def as_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "model": self.model,
            "tokens": self.tokens.as_dict(),
            "turns": self.turns,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "prompt": self.prompt,
            "active": self.active,
            "agent_type": self.agent_type,
            "description": self.description,
            "parent_agent_id": self.parent_agent_id,
            "depth": self.depth,
        }


@dataclass
class SessionUsage:
    """What one conversation, and everything it dispatched, has spent."""
    session_id: str
    cwd: str
    project: str
    tokens: Tokens                 # the conversation itself
    turns: int
    agents: list[AgentUsage]
    agent_tokens: Tokens           # everything it dispatched
    models: dict[str, Tokens]
    first_at: float | None
    last_at: float | None
    context_tokens: int | None     # the last turn's carried context, or None
    own: bool = False              # JARVIS's own machinery, not the user's

    @property
    def total_tokens(self) -> Tokens:
        return self.tokens + self.agent_tokens

    @property
    def active_agents(self) -> int:
        return sum(1 for a in self.agents if a.active)

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "project": self.project,
            "tokens": self.tokens.as_dict(),
            "agent_tokens": self.agent_tokens.as_dict(),
            "total_tokens": self.total_tokens.as_dict(),
            "turns": self.turns,
            "context_tokens": self.context_tokens,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "active_agents": self.active_agents,
            "agents": [a.as_dict() for a in self.agents],
            "models": [{"model": m, "tokens": t.as_dict()}
                       for m, t in sorted(self.models.items(),
                                          key=lambda kv: -kv[1].total)],
            "own": self.own,
        }


@dataclass
class DayUsage:
    day: str
    tokens: Tokens


@dataclass
class Report:
    measured: bool
    scanned_at: float
    sessions: list[SessionUsage]
    own_sessions: list[SessionUsage]
    totals: Tokens
    own_totals: Tokens
    today: Tokens
    daily: list[DayUsage]
    models: dict[str, Tokens]
    files: int
    bytes_read: int
    roots: list[str]


def _project_of(cwd: str, fallback: str) -> str:
    if not cwd:
        return fallback
    return session_watch.project_name(cwd)


def _merge(dst: dict, src: dict) -> None:
    for key, value in src.items():
        _add(dst, key, value)


def report(roots: list[Path] | None = None, *, now: float | None = None,
           cache: Cache | None = None,
           own_session_ids=frozenset()) -> Report:
    """Read (incrementally) and reduce. Never raises on a file underneath it."""
    at = time.time() if now is None else float(now)
    cache = _DEFAULT_CACHE if cache is None else cache
    root_paths = [Path(r) for r in
                  (roots if roots is not None else session_watch.config_roots())]

    sets = discover(root_paths)
    brain_cwd = session_watch.brain_cwd()

    sessions: list[SessionUsage] = []
    bytes_read = 0
    # Files actually READ, not files walked past. `measured` is derived from
    # this, and the whole point of the flag is that "0 tokens, read from 1
    # transcript" must be impossible to render off a file nobody could open.
    files = 0
    live_paths: set[str] = set()

    for session_id, group in sets.items():
        main = FileTotals()
        by_day: dict[str, Tokens] = {}
        by_model: dict[str, Tokens] = {}
        first_at = last_at = None
        context = None
        cwd = ""

        for path in group.main:
            live_paths.add(str(path))
            totals, read, was_read = scan_file(path, cache)
            files += 1 if was_read else 0
            bytes_read += read
            main.tokens = main.tokens + totals.tokens
            main.turns += totals.turns
            _merge(by_day, totals.by_day)
            _merge(by_model, totals.by_model)
            cwd = cwd or totals.cwd
            if totals.context_tokens is not None:
                context = totals.context_tokens
            first_at = _min(first_at, totals.first_at)
            last_at = _max(last_at, totals.last_at)

        agents: list[AgentUsage] = []
        agent_tokens = ZERO
        for agent_id, path in sorted(group.agents.items()):
            live_paths.add(str(path))
            totals, read, was_read = scan_file(path, cache)
            files += 1 if was_read else 0
            bytes_read += read
            agent_tokens = agent_tokens + totals.tokens
            _merge(by_day, totals.by_day)
            _merge(by_model, totals.by_model)
            cwd = cwd or totals.cwd
            first_at = _min(first_at, totals.first_at)
            last_at = _max(last_at, totals.last_at)
            meta = read_agent_meta(path, cache)
            agents.append(AgentUsage(
                agent_id=agent_id,
                model=totals.model,
                tokens=totals.tokens,
                turns=totals.turns,
                first_at=totals.first_at,
                last_at=totals.last_at,
                prompt=totals.prompt,
                active=(totals.last_at is not None
                        and at - totals.last_at <= ACTIVE_WITHIN_SEC),
                agent_type=meta["agent_type"],
                description=meta["description"],
                parent_agent_id=meta["parent_agent_id"],
                depth=meta["depth"],
            ))
        agents.sort(key=lambda a: (not a.active,
                                  -(a.last_at if a.last_at is not None else 0.0)))

        fallback = group.main[0].parent.name if group.main else \
            (next(iter(group.agents.values())).parent.parent.parent.name
             if group.agents else "")
        own = session_id in own_session_ids or (
            bool(cwd) and _same_path(cwd, brain_cwd))

        sessions.append(SessionUsage(
            session_id=session_id,
            cwd=cwd,
            project=_project_of(cwd, fallback),
            tokens=main.tokens,
            turns=main.turns,
            agents=agents,
            agent_tokens=agent_tokens,
            models=by_model,
            first_at=first_at,
            last_at=last_at,
            context_tokens=context,
            own=own,
        ))
        # Day and model buckets are attached to the session so the report can
        # roll up the USER's days without JARVIS's runs in them.
        sessions[-1]._by_day = by_day        # type: ignore[attr-defined]

    cache.forget_missing(live_paths)

    mine = [s for s in sessions if not s.own]
    theirs = [s for s in sessions if s.own]
    for group_ in (mine, theirs):
        group_.sort(key=lambda s: (s.last_at is None,
                                   -(s.last_at if s.last_at is not None else 0.0)))

    days: dict[str, Tokens] = {}
    models: dict[str, Tokens] = {}
    totals = ZERO
    for s in mine:
        totals = totals + s.total_tokens
        _merge(days, getattr(s, "_by_day", {}))
        _merge(models, s.models)
    own_totals = ZERO
    for s in theirs:
        own_totals = own_totals + s.total_tokens

    return Report(
        measured=files > 0,
        scanned_at=at,
        sessions=mine,
        own_sessions=theirs,
        totals=totals,
        own_totals=own_totals,
        today=days.get(day_key(at), ZERO),
        daily=[DayUsage(d, t) for d, t in sorted(days.items())],
        models=models,
        files=files,
        bytes_read=bytes_read,
        roots=[str(r) for r in root_paths],
    )


def _min(a: float | None, b: float | None) -> float | None:
    return b if a is None else (a if b is None else min(a, b))


def _max(a: float | None, b: float | None) -> float | None:
    return b if a is None else (a if b is None else max(a, b))


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a) == Path(b)
    except (TypeError, ValueError):
        return False


# ── the shape the dashboard reads ───────────────────────────────────────────

# How many days of history the sparkline gets. Beyond this the line is too
# dense to read and the payload stops being cheap.
DAILY_DAYS = 30
# How many conversations the payload carries, per ordering. The totals above
# still count every one of them.
MAX_SESSIONS = 40


def _listed(sessions: list, limit: int) -> tuple[list, int]:
    """(the conversations to ship, how many of the biggest are among them).

    TWO ORDERINGS, UNIONED, because two surfaces read this payload and want
    different things:

      * the Usage tab ranks by SPEND and then says "N smaller conversations
        not listed". That sentence is only true if the ones it ranked really
        are the biggest on the machine — so the biggest have to be in the
        payload. Truncating by recency alone made the unlisted ones merely
        OLDER, and the caption asserted a thousand conversations were all
        smaller than the twenty-five shown off a sort that never compared
        them.

      * the Sessions tab looks up what each LIVE conversation has spent.
        Truncating by spend alone would drop a conversation that started
        five minutes ago and has barely spent anything — which is most of
        the ones a person is actually looking at.

    The second value is the licence for the word "smaller": it says how many
    of the machine's biggest spenders are guaranteed present, so the client
    can check its own claim rather than assume it.
    """
    limit = max(0, limit)
    biggest = sorted(sessions, key=lambda s: -s.total_tokens.total)[:limit]
    keep = {s.session_id for s in sessions[:limit]}
    keep |= {s.session_id for s in biggest}
    # Emitted in the caller's order, which is by recency.
    return [s for s in sessions if s.session_id in keep], len(biggest)


def snapshot(roots: list[Path] | None = None, *, now: float | None = None,
             cache: Cache | None = None, own_session_ids=frozenset(),
             limit: int = MAX_SESSIONS) -> dict:
    """The JSON `/api/usage/sessions` returns.

    `measured: false` is the flag that means "nothing has been read" — the
    zeros beside it are the arithmetic of an empty set, not a measurement,
    and the UI must never render them as one.
    """
    r = report(roots, now=now, cache=cache, own_session_ids=own_session_ids)
    listed, largest_listed = _listed(r.sessions, limit)
    own_listed, _ = _listed(r.own_sessions, limit)
    return {
        "measured": r.measured,
        "scanned_at": r.scanned_at,
        "active_within_sec": ACTIVE_WITHIN_SEC,
        "roots": r.roots,
        "files": r.files,
        "bytes_read": r.bytes_read,
        "totals": r.totals.as_dict(),
        "own_totals": r.own_totals.as_dict(),
        "today": r.today.as_dict(),
        "session_count": len(r.sessions),
        "own_session_count": len(r.own_sessions),
        "project_count": len({s.project for s in r.sessions if s.project}),
        "active_agents": sum(s.active_agents for s in r.sessions),
        "daily": [{"day": d.day, "tokens": d.tokens.as_dict()}
                  for d in r.daily[-DAILY_DAYS:]],
        "models": [{"model": m, "tokens": t.as_dict()}
                   for m, t in sorted(r.models.items(),
                                      key=lambda kv: -kv[1].total)],
        # How many of the machine's biggest spenders are in `sessions`. The
        # Usage tab's "N smaller conversations not listed" is only a true
        # sentence about the rows it can prove are the largest.
        "largest_listed": largest_listed,
        "sessions": [s.as_dict() for s in listed],
        "own_sessions": [s.as_dict() for s in own_listed],
    }
