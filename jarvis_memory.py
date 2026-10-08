"""JARVIS's long-term memory: a folder of plain Markdown the user can read.

One fact per file so a single memory can be corrected or deleted by hand
without disturbing the rest. `MEMORY.md` is the curated index the brain always
sees; everything else is found by `recall`.

Nothing here is ever destructive: files are created or replaced wholesale by
their author, the index is appended to, and no function deletes anything. The
user edits this folder directly, and losing their words would be worse than
keeping a stale line.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import data_paths

MEMORY_INDEX_MAX = 80          # lines in MEMORY.md before we ask for a tidy-up
SLUG_MAX = 60

_APOSTROPHES = re.compile(r"['’‘\"“”]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Values that land on a STRUCTURED line
# ---------------------------------------------------------------------------
#
# `MEMORY.md` is `@`-imported by `jarvis_home/CLAUDE.md`, so every line of it
# is system text in every future generation; `memory/`, `projects/` and
# `journal/` are reached through `recall`, which is deliberately not a
# tainting tool precisely because "what actually protects it is that the
# WRITERS are gated" (server.TAINT_EXEMPT_TOOLS). `server.MEMORY_WRITERS` is
# one half of that gate — WHO may write. This is the other half: the SHAPE of
# what gets written.
#
# One memory is one LINE. `add_to_index` wrote
# `- [{title}]({slug}.md) — {hook}` with nothing but `.strip()`, and `strip()`
# leaves interior newlines, so a hook of
#
#     "he does.\n\n## Standing instruction from the operator\n…"
#
# put a heading of its own into the index — and `add_to_index`'s rewrite loop
# then preserves it for ever, as "prose the user added". Executed.
#
# `str.split()` with no argument splits on every character `str.splitlines()`
# knows about and then some, so the join below cannot leave a separator
# behind. That is deliberately asked of the language rather than of a
# hand-written list of characters: `server._env_value_problem` learned this
# the hard way — its list was "\n", "\r", "\0" and the readers split on ten.
FIELD_MAX_CHARS = 240

# The index's own link syntax. A `]` or a `(` in a TITLE closes the link and
# opens another, so `_INDEX_LINE_RE` parses back a different title and slug
# than the ones written — the row stops meaning what it says. Removed rather
# than escaped: a memory's title is a short name a person reads in a listing,
# and no name needs brackets.
_INDEX_STRUCTURAL = re.compile(r"[\[\]()]")


def one_line(value: str, limit: int = FIELD_MAX_CHARS) -> str:
    """`value` reduced to something that can only ever occupy one line."""
    flat = " ".join(str(value or "").split())
    return flat[: limit - 1] + "…" if len(flat) > limit else flat


class IndexFull(Exception):
    """`MEMORY.md` is at `MEMORY_INDEX_MAX` and this entry is a NEW one.

    Raised rather than silently dropped: the caller has to tell the user that
    nothing was written, and `tool_remember` says so out loud. `index_is_full`
    used to be advisory — a hint string handed to the brain, which is the
    thing an attacker is talking to — so the file that is loaded whole into
    every generation had no bound on it at all.
    """


class UnwritableValue(Exception):
    """The line this value would produce does not read back as itself.

    The wall, after the flattening above: whatever `one_line` and
    `_INDEX_STRUCTURAL` did or did not catch, a line is only written if the
    module's OWN reader parses it back to exactly the values handed in. Same
    round-trip rule as `server._env_value_problem`, and for the same reason —
    it needs no list of dangerous characters to be right.
    """


def _normalize_title(text: str) -> str:
    """Full-length normalised form of a title, used to tell whether two
    titles name the SAME memory.

    Unlike slugify(), this is never truncated: slugify() caps at SLUG_MAX
    for filenames, and comparing on the truncated form would make two
    genuinely different long titles that happen to share their first
    SLUG_MAX characters look identical. Trivial punctuation/case/apostrophe
    differences still collapse to the same normalised form, which is what
    lets "Tony's DB choice" and "tonys db choice" count as the same
    memory rather than two.
    """
    stripped = _APOSTROPHES.sub("", (text or "").lower())
    return _NON_ALNUM.sub("-", stripped).strip("-")


def slugify(text: str) -> str:
    """A filename a person can recognise in a directory listing."""
    # Strip apostrophes/quotes rather than treating them as separators, so
    # "Tony's" becomes "tonys" and not "tony-s".
    norm = _normalize_title(text)
    return (norm[:SLUG_MAX].rstrip("-") or "note")


def _title_of(path: Path) -> str | None:
    """The `# Title` header of an existing memory file, or None if it
    cannot be read/found (a file the user emptied or rewrote by hand)."""
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def write_memory(title: str, body: str) -> Path:
    """One fact, one file. Re-writing the same title updates it in place.

    Two different titles can slugify to the same filename (case, punctuation,
    or SLUG_MAX truncation). Blindly writing to that filename would silently
    destroy whatever memory already lives there. So before writing, an
    existing file at the target name is only reused if its own `# Title`
    header names the SAME memory (compared on the untruncated normalised
    form); otherwise a fresh, non-colliding filename is chosen so neither
    memory is lost.

    The TITLE is flattened to one line: it is written as a `# ` header and
    read back by `_title_of`, which takes the first `# ` line and nothing
    else, so a title with a break in it silently becomes a different title
    (and puts whatever followed the break into the file as prose of its own).
    The BODY is left alone — it is a paragraph, nothing parses it back a line
    at a time, and flattening it would lose the user's own shape.
    """
    data_paths.ensure_memory_layout()
    directory = data_paths.memory_dir()
    title = one_line(title)
    slug = slugify(title)
    norm = _normalize_title(title)

    path = directory / f"{slug}.md"
    n = 2
    while path.exists():
        existing_title = _title_of(path)
        if existing_title is not None and _normalize_title(existing_title) == norm:
            break  # same memory (mod trivial punctuation/case) — update in place
        path = directory / f"{slug}-{n}.md"
        n += 1

    stamp = datetime.now().strftime("%Y-%m-%d")
    path.write_text(f"# {title.strip()}\n\n_{stamp}_\n\n{body.strip()}\n")
    return path


def read_memory(name: str) -> str | None:
    path = data_paths.memory_dir() / f"{slugify(name)}.md"
    try:
        return path.read_text()
    except OSError:
        return None


def list_memories() -> list[str]:
    try:
        return sorted(p.stem for p in data_paths.memory_dir().glob("*.md"))
    except OSError:
        return []


INDEX_NAME = "MEMORY.md"
INDEX_HEADER = (
    "# What JARVIS remembers\n\n"
    "One line per memory. This file is loaded into every conversation, so it\n"
    "stays short; the detail lives in `memory/`.\n\n"
)


def _index_path() -> Path:
    return data_paths.brain_home() / INDEX_NAME


def ensure_layout() -> Path:
    """Create the whole memory folder, including an empty index.

    `CLAUDE.md` imports `@MEMORY.md`, and that import is what puts the index in
    front of the brain in every conversation. On a fresh install nothing had
    created the file, so the import dangled and JARVIS started with no index at
    all — seed it here so the promise `CLAUDE.md` makes is true from the first
    boot. Never overwrites an existing index.
    """
    home = data_paths.ensure_memory_layout()
    index = _index_path()
    if not index.exists():
        index.write_text(INDEX_HEADER)
    return home


def index_lines() -> list[str]:
    try:
        text = _index_path().read_text()
    except OSError:
        return []
    return [ln for ln in text.splitlines() if ln.startswith("- [")]


# The index's format, in one place: the writer round-trips through it before
# it commits a line, and `index_entries` parses rows back out of it. No `$`,
# and used with `fullmatch` — Python's `$` matches BEFORE a trailing newline,
# so `.match()` on a `$`-anchored pattern cannot answer "is the WHOLE value
# this shape". See tests/test_anchored_patterns.py, which holds every
# anchored pattern in this repository to that rule.
# `str.splitlines()` splits on TEN characters; a regex `.` excludes only
# "\n" of them, and `\s` MATCHES all ten. So a line-oriented pattern written
# with `.` and `\s` still accepts nine separators — which is the same defect
# as the `$` above, one layer down. This class is "any character that is not
# one of the ten", and tests/test_anchored_patterns.py checks the list
# against `str.splitlines()` itself rather than trusting it.
_ON_ONE_LINE = r"[^\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]"

_SEPARATORS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"

_INDEX_LINE_RE = re.compile(
    rf"- \[(?P<title>[^\]{_SEPARATORS}]*)\]"
    rf"\((?P<slug>[^)/\\{_SEPARATORS}]*?)\.md\)"
    rf"(?:[ \t]*[—–-][ \t]*(?P<hook>{_ON_ONE_LINE}*))?")


def _index_line(title: str, slug: str, hook: str) -> str:
    """The one line this memory occupies, proven to read back as itself.

    Raises `UnwritableValue` rather than writing something the index's own
    parser would disagree with. The check is a round trip through
    `_INDEX_LINE_RE` — the very regex `index_entries` uses — so it needs no
    list of dangerous characters and cannot fall behind the format.
    """
    line = f"- [{title}]({slug}.md) — {hook}"
    m = _INDEX_LINE_RE.fullmatch(line)
    if (line.splitlines() != [line] or m is None
            or m.group("title") != title or m.group("slug") != slug
            or (m.group("hook") or "") != hook):
        raise UnwritableValue(
            "that memory cannot be written as a single index line")
    return line


def add_to_index(title: str, hook: str) -> None:
    """One line per memory. A repeated title updates its hook in place.

    Rewrites only the generated lines and keeps whatever prose the user has
    added around them — which is exactly why a value that can write a line of
    its own is permanent here: an unrecognised line is preserved as the
    user's, for ever. Both values are flattened to one line and the finished
    line is round-tripped before anything is written.

    Raises `IndexFull` for a NEW entry once the index is at
    `MEMORY_INDEX_MAX`. This file is loaded whole into every generation, so
    "the brain will tidy it up" is not a bound — the brain is the thing being
    talked to. Updating a line that already exists does not grow the file and
    is always allowed.
    """
    data_paths.ensure_memory_layout()
    path = _index_path()
    title = _INDEX_STRUCTURAL.sub("", one_line(title))
    hook = one_line(hook)
    slug = slugify(title)
    line = _index_line(title, slug, hook)

    try:
        existing = path.read_text()
    except OSError:
        existing = INDEX_HEADER

    kept, replaced = [], False
    for ln in existing.splitlines():
        if ln.startswith("- [") and f"]({slug}.md)" in ln:
            kept.append(line)
            replaced = True
        else:
            kept.append(ln)
    if not replaced:
        if index_is_full():
            raise IndexFull(
                f"MEMORY.md already holds {MEMORY_INDEX_MAX} memories")
        kept.append(line)
    path.write_text("\n".join(kept).rstrip("\n") + "\n")


def index_is_full() -> bool:
    """True when the index has outgrown what belongs in every conversation."""
    return len(index_lines()) >= MEMORY_INDEX_MAX


def write_project_note(project: str, text: str) -> Path:
    """What JARVIS knows about one project, appended in order.

    Appends because last week's finding about chitauri is still true; a
    replace would quietly discard it.

    One note is one stamped LINE, so both values are flattened: a break in
    `text` writes further notes with no stamp on them, and a break in
    `project` writes lines under the `# ` header. Nothing here is ever
    deleted (see the module docstring), so a forged line, once written, is
    permanent — the same reason `add_to_index` is strict.
    """
    data_paths.ensure_memory_layout()
    project = one_line(project)
    text = one_line(text)
    path = data_paths.projects_dir() / f"{slugify(project)}.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not path.exists():
        path.write_text(f"# {project}\n\n")
    with path.open("a") as fh:
        fh.write(f"_{stamp}_ — {text}\n")
    return path


def read_project_note(project: str) -> str | None:
    path = data_paths.projects_dir() / f"{slugify(project)}.md"
    try:
        return path.read_text()
    except OSError:
        return None


_JOURNAL_STAMP_FMT = "%Y-%m-%d-%H%M%S-%f"   # fixed-width: lexicographic == chronological

# The last moment a journal was stamped with. Windows' clock under Python 3.12
# advances in ~15ms ticks, so two entries written back to back read the SAME
# microsecond, and the tie is then broken by the reason in the filename —
# "rotation" before "shutdown" — rather than by which was written last.
_last_journal_moment: datetime | None = None


def _journal_moment() -> datetime:
    """`datetime.now()`, but strictly later than any earlier journal stamp
    from this process."""
    global _last_journal_moment
    now = datetime.now()
    if _last_journal_moment is not None and now <= _last_journal_moment:
        now = _last_journal_moment + timedelta(microseconds=1)
    _last_journal_moment = now
    return now
_JOURNAL_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{6}-\d{6})-(.+)\.md")

# Reasons that mark an entry as a PLACEHOLDER: a tombstone proving a
# generation ended rather than vanished, written when the brain had nothing to
# hand over. They are still written to disk — that is their whole purpose — but
# they must never be carried into the next generation as "where you left off",
# or one silent shutdown would erase a real handover written minutes earlier
# and JARVIS would boot knowing nothing for the rest of time.
#
# The marker lives in the REASON, which `write_journal` already slugifies into
# the filename, for three reasons: the decision can be made from a directory
# listing without opening a single file; a marker inside the body would be one
# hand-edit (this folder is the user's to edit) away from being lost or
# copy-pasted onto a real entry; and a filename a person can read is
# self-documenting where a frontmatter key would not be.
PLACEHOLDER_REASONS = frozenset({"rotation-silent", "shutdown-silent"})

# `write_journal` appends "-2", "-3"… to break a filename collision, so the
# reason parsed back out of a name may carry that suffix.
_COLLISION_SUFFIX = re.compile(r"-\d+$")


def write_journal(text: str, reason: str = "shutdown",
                  untrusted_source: str | None = None) -> Path:
    """How one brain generation hands over to the next.

    `untrusted_source` is what the generation that wrote this note had read
    that JARVIS did not write — "a web page", "another session's transcript"
    — or None if it read nothing of the kind. It is recorded IN THE FILE
    because that is the only place it can survive a restart: the next
    generation after a restart reads this note off disk and the process that
    wrote it is gone. `brain.launch_prompt` wraps the note either way; this
    is what lets it also say where the note's author had been.

    The filename carries an explicit, fixed-width seconds-and-microseconds
    timestamp so that write order is recorded directly rather than inferred
    from filesystem metadata later. At microsecond resolution, plus the
    fixed width, two entries written back to back always get distinct names
    that already sort chronologically — the collision loop below is just a
    safety net for a clock that doesn't advance between calls.

    `reason` and `untrusted_source` both land on the entry's single `# `
    header line, so both are flattened to one line — a break in either forges
    a line of prose above the note, in the one file a fresh generation reads
    off disk at boot. The note itself is free prose: `brain.wrap_handover`
    puts it inside an untrusted block either way.
    """
    data_paths.ensure_memory_layout()
    reason = one_line(reason) or "shutdown"
    untrusted_source = one_line(untrusted_source) or None
    stamp = _journal_moment().strftime(_JOURNAL_STAMP_FMT)
    path = data_paths.journal_dir() / f"{stamp}-{slugify(reason)}.md"
    n = 2
    while path.exists():
        path = data_paths.journal_dir() / f"{stamp}-{slugify(reason)}-{n}.md"
        n += 1
    header_stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    provenance = (f"\n\nThe generation that wrote this had read "
                  f"{untrusted_source} that day."
                  if untrusted_source else "")
    path.write_text(
        f"# {header_stamp} ({reason}){provenance}\n\n{text.strip()}\n")
    return path


def _journal_stamp(path: Path) -> str | None:
    """The timestamp component parsed from a journal filename, or None if
    the file doesn't match (renamed by hand) — skipped, not a crash."""
    m = _JOURNAL_NAME_RE.fullmatch(path.name)
    return m.group(1) if m else None


def journal_reason(path: Path) -> str:
    """The reason component parsed from a journal filename, or "" if the
    name doesn't parse (renamed by hand). Best-effort by design."""
    m = _JOURNAL_NAME_RE.fullmatch(path.name)
    return m.group(2) if m else ""


def is_placeholder_reason(reason: str) -> bool:
    """True for a tombstone entry — one written to prove a generation ended,
    not because there was anything to hand over."""
    if reason in PLACEHOLDER_REASONS:
        return True
    return _COLLISION_SUFFIX.sub("", reason) in PLACEHOLDER_REASONS


def journal_entries() -> list[tuple[str, str, Path]]:
    """Every parseable entry as (stamp, reason, path), oldest first.

    Ordered on the timestamp recorded IN THE FILENAME, not filesystem mtime:
    this folder is user-editable, and correcting a typo in an old entry
    updates its mtime without making it the newest entry. The timestamp is
    fixed-width and zero-padded, so a plain string sort of the parsed
    component is already chronological order — no need to parse it into a
    datetime. A file whose name doesn't parse is skipped rather than
    crashing the sort.
    """
    try:
        candidates = list(data_paths.journal_dir().glob("*.md"))
    except OSError:
        return []
    entries = [(stamp, journal_reason(p), p)
               for p in candidates if (stamp := _journal_stamp(p))]
    entries.sort(key=lambda e: e[0])
    return entries


def latest_journal(limit: int = 1200, include_placeholders: bool = False) -> str | None:
    """The most recent real handover, bounded — it is prepended to every new
    brain, at rotation AND at a cold start.

    Placeholder entries are skipped by default: shutdown always writes one
    when the brain gave nothing, so carrying them forward would mean every
    restart after a silent shutdown handing the next generation a note that
    says only that the last one said nothing. They stay on disk; they are
    just never the thing that gets carried.
    """
    entries = journal_entries()
    if not include_placeholders:
        entries = [e for e in entries if not is_placeholder_reason(e[1])]
    if not entries:
        return None
    try:
        text = entries[-1][2].read_text()
    except OSError:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


EXCERPT_CHARS = 240

# Words too common to mean anything on their own. A query built entirely of
# these ("what did we do on chitauri" minus "chitauri") must not fall
# through to matching every file by substring — without this list "the" or
# "is" alone would come back a hit against nearly any body of prose.
_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do",
    "does", "for", "from", "had", "has", "have", "he", "her", "his", "i",
    "in", "is", "it", "its", "of", "on", "or", "our", "she", "so", "that",
    "the", "their", "them", "there", "they", "this", "to", "was", "we",
    "were", "what", "when", "which", "who", "with", "you", "your",
})


def _score(query_words: set[str], title: str, body: str) -> int:
    """Title matches count for more: a file named for the thing you asked
    about is almost always the one you meant."""
    title_l, body_l = title.lower(), body.lower()
    score = 0
    for w in query_words:
        if w in title_l:
            score += 10
        score += body_l.count(w)
    return score


_URL_RE = re.compile(r"https?://\S+")
_URL_LONG_MIN = 30      # shorter than this reads fine spoken as-is
_TABLE_ROW = re.compile(r"\|.*\|")


def _speakable(text: str) -> str:
    """Polish a line for text-to-speech without touching ordinary prose.

    Two shapes are topically correct but read badly aloud: a Markdown
    table row ("| zeltar pro | $99 |" -> "pipe zeltar pro pipe dollar 99
    pipe") and a long URL (character-soup). Only a line that actually
    looks like a table row has its pipes collapsed, and only a URL long
    enough to be unspeakable is replaced — a short URL or a stray "|" in
    normal prose is left alone.
    """
    def _tame_url(m: re.Match) -> str:
        url = m.group(0)
        if len(url) < _URL_LONG_MIN:
            return url
        domain = re.sub(r"^https?://", "", url).split("/")[0]
        domain = re.sub(r"^www\.", "", domain)
        return domain or "a link"

    text = _URL_RE.sub(_tame_url, text)
    if _TABLE_ROW.fullmatch(text.strip()):
        cells = [c.strip() for c in text.strip().strip("|").split("|")]
        text = ", ".join(c for c in cells if c)
    return text


def _cap(text: str) -> str:
    text = _speakable(" ".join(text.split()))
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1] + "…"


_LEADING_STAMP = re.compile(r"^_[^_\n]+_\s*[—-]\s*")


def _clean_line(line: str) -> str | None:
    """A line stripped to what could be spoken, or None if it is furniture.

    This is read aloud, so a matching line that is really just markdown
    furniture is no good: the "# Title" heading (the title word always
    matches there) or a bare "_2026-08-30_" timestamp line contain the
    query word but no sentence a butler could speak. Strip a leading
    heading marker, strip a project note's leading "_stamp_ — " (real
    content follows it, but the stamp itself is metadata, not something to
    read aloud), and drop any line with no letters in it at all.
    """
    candidate = line.strip().lstrip("#").strip()
    candidate = _LEADING_STAMP.sub("", candidate)
    if not candidate or not any(c.isalpha() for c in candidate):
        return None
    return candidate


def _excerpt(body: str, query_words: set[str]) -> str:
    """The line that matched, so the brain sees why this was returned.

    Priority order:
    1. A substantive line that CONTAINS a query word — the normal case.
    2. If every BODY line that matches is an echo (its words are all query
       words, e.g. a decorated aside like "zeltar!!! ???") return that line
       anyway: it's at least honest about why the file matched, which beats
       an unrelated sentence pulled from elsewhere in the body.
    3. Only when NO body line matches at all — a title-only hit, e.g.
       querying "chitauri" against chitauri.md whose body never repeats the
       word — fall back to the first substantive body line. A heading
       that merely echoes the query (the title IS the query) does not
       count as a body match for this purpose: "chitauri" the heading
       tells a listener nothing they didn't already know from the file's
       own name, so it must not preempt real body content the way a
       body-line echo is allowed to.
    """
    echo_only = None
    for line in body.splitlines():
        is_heading = line.strip().startswith("#")
        candidate = _clean_line(line)
        if candidate is None:
            continue
        low = candidate.lower()
        if not any(w in low for w in query_words):
            continue
        if set(re.split(r"[^a-z0-9]+", low)) - {""} <= query_words:
            if not is_heading:
                echo_only = echo_only or candidate   # matched, says nothing new
            continue
        return _cap(candidate)

    if echo_only is not None:
        return _cap(echo_only)

    for line in body.splitlines():
        if line.strip().startswith("#"):
            continue
        candidate = _clean_line(line)
        if candidate:
            return _cap(candidate)

    return _cap(body)


def _sources() -> list[tuple[str, Path]]:
    out = []
    for kind, directory in (("memory", data_paths.memory_dir()),
                            ("project", data_paths.projects_dir()),
                            ("journal", data_paths.journal_dir())):
        try:
            for path in sorted(directory.glob("*.md")):
                out.append((kind, path))
        except OSError:
            continue
    return out


def search(query: str, limit: int = 5) -> list[dict]:
    """Scored scan of the folder. No index, so it can never be stale."""
    words = {
        w for w in re.split(r"[^a-z0-9]+", (query or "").lower())
        if len(w) > 1 and w not in _STOP_WORDS
    }
    if not words:
        return []
    hits = []
    for kind, path in _sources():
        try:
            body = path.read_text()
        except OSError:
            continue
        score = _score(words, path.stem, body)
        if score:
            hits.append({"kind": kind, "name": path.stem, "path": str(path),
                         "excerpt": _excerpt(body, words), "score": score})
    hits.sort(key=lambda h: (-h["score"], h["name"]))
    return hits[:limit]


# ---------------------------------------------------------------------------
# Listings — what the dashboard's Memory view reads.
#
# Read-only by construction: nothing here creates the folder. A brain that has
# never remembered anything has no `jarvis/` directory at all, and a GET must
# report that as empty rather than bring it into being.
# ---------------------------------------------------------------------------

# `_INDEX_LINE_RE` lives beside `add_to_index`, which round-trips every line
# through it before writing — one format, one place.


def index_entries() -> list[dict]:
    """MEMORY.md, parsed. A line the user has reshaped by hand is skipped
    rather than half-parsed into a row that says nothing."""
    out = []
    for line in index_lines():
        m = _INDEX_LINE_RE.fullmatch(line.strip())
        if not m:
            continue
        out.append({"title": (m.group("title") or "").strip(),
                    "slug": m.group("slug"),
                    "hook": (m.group("hook") or "").strip()})
    return out


def _file_entries(directory: Path) -> list[dict]:
    """slug / title / mtime for each `.md` in one folder, newest first.

    `title` is the file's own `# ` header where it has one — the user renames
    files and rewrites headers by hand, and the header is the human answer —
    falling back to the slug so a row is never blank.
    """
    try:
        paths = sorted(directory.glob("*.md"))
    except OSError:
        return []
    out = []
    for path in paths:
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue                      # deleted between the glob and here
        out.append({"slug": path.stem,
                    "title": _title_of(path) or path.stem,
                    "modified": modified})
    out.sort(key=lambda e: e["modified"], reverse=True)
    return out


def memory_entries() -> list[dict]:
    return _file_entries(data_paths.memory_dir())


def project_entries() -> list[dict]:
    return _file_entries(data_paths.projects_dir())


def _stamp_to_epoch(stamp: str) -> float:
    """Epoch seconds for a journal filename's own timestamp.

    NOT mtime: this folder is user-editable, and correcting a typo in an old
    entry updates its mtime without changing when the entry was written. That
    exact confusion has already cost this milestone one bug.
    """
    try:
        return datetime.strptime(stamp, _JOURNAL_STAMP_FMT).timestamp()
    except ValueError:
        return 0.0


def journal_entries_meta() -> list[dict]:
    """slug / when / reason for each journal entry, newest first."""
    return [{"slug": path.stem, "when": _stamp_to_epoch(stamp), "reason": reason}
            for stamp, reason, path in reversed(journal_entries())]


def latest_journal_slug() -> str | None:
    """The entry a new brain will actually carry — so the dashboard marks the
    same one, placeholders skipped, rather than merely the newest file."""
    entries = [e for e in journal_entries() if not is_placeholder_reason(e[1])]
    return entries[-1][2].stem if entries else None


DOC_DIRS = {
    "memory": data_paths.memory_dir,
    "project": data_paths.projects_dir,
    "journal": data_paths.journal_dir,
}


def doc_path(kind: str, slug: str) -> Path | None:
    """The file for one (kind, slug), or None if there isn't one — including
    every case where `slug` tries to name a file outside its own folder.

    `slug` arrives from a URL, so it is treated as hostile. The string checks
    reject the obvious before anything touches the filesystem; the
    containment check afterwards is what actually decides, because string
    checks alone cannot see a symlink inside the folder that points out of
    it, and resolution alone would accept a traversal if the folder itself
    were reached through one. Both sides are resolved, so the comparison is
    between two real paths.
    """
    get_dir = DOC_DIRS.get(kind)
    if get_dir is None:
        return None
    if not slug or "/" in slug or "\\" in slug or "\x00" in slug:
        return None
    if slug.startswith(".") or Path(slug).name != slug:
        return None
    try:
        root = get_dir().resolve()
        candidate = (root / f"{slug}.md").resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None
