"""The review surface: what JARVIS proposes, and what JARVIS produced.

`builds.py` is the producer — it writes the spec into the project and spawns
the session that writes the phased plan. This module is the *reader*, and it
exists because of one thing the user said out loud:

    "JARVIS needs to open a clean/simple UI for specs and plans for people to
    actually see it and communicate feedback to JARVIS by voice."

`superpowers:brainstorming` has a hard human-approval gate, and the gate is
there to get a *human's* approval. You cannot approve a sixty-line spec that
was read aloud to you. The gate needs an eye — so the document goes on a page
and the answer comes back by voice.

Three things are load-bearing.

1. **Numbering is the mechanism, not decoration.** The user says "change
   three", "drop five", "approved". That only works if the number they say
   resolves to the same section on both sides of the conversation, so there
   is exactly ONE function that numbers a document — `sections_of` — and both
   readers call it: the `/api/specs` payload the page renders, and the
   `review_document` tool JARVIS answers from. Neither counts headings for
   itself. Numbering is a pure function of the file's text: no clock, no
   database, no ordering that a second reader could get wrong.

2. **Approval is a recorded act.** `start_build` used to *assume* the design
   was approved, which meant a restart forgot it and a revision inherited it.
   Approval is now a small JSON file in the project, beside the spec and the
   plan, holding a digest of the exact text that was approved. That is what
   makes the third state possible: a document whose text no longer matches
   its approval is **superseded**, and needs the user's eye again.

3. **This reads files from arbitrary project directories**, off a path that
   arrived in a URL. Containment is decided by `repo_read.resolve_within` —
   the existing precedent, which resolves both sides so a symlink, a `..` and
   an absolute path all fail identically — and then narrowed again to the two
   directories documents actually live in.

No server imports, no asyncio, no subprocess: `server.py` calls all of it,
the blocking parts through `asyncio.to_thread`.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import builds
import repo_read

# Approvals live beside the artifacts they approve, for the same reason the
# spec does: a human opening the project finds them, and they outlive the
# session, the compaction and the restart.
APPROVAL_DIR = "docs/superpowers/approvals"

# The only two directories a document may be read from. `resolve_within`
# already proves containment inside the project; this narrows it from "any
# file in the repo" to "the things this surface is about".
DOCUMENT_DIRS = (builds.SPEC_DIR, builds.PLAN_DIR)

# A spec is written by JARVIS from a conversation; a plan is written by the
# session from the spec. They are read differently, so they are told apart.
KIND_OF_DIR = {builds.SPEC_DIR: "spec", builds.PLAN_DIR: "plan"}

# Documents are read whole into the browser. A spec is a page of prose and a
# plan is a checklist; a megabyte of either is a file that went wrong.
MAX_DOCUMENT_BYTES = 400_000


# ---------------------------------------------------------------------------
# Numbering
# ---------------------------------------------------------------------------

# `str.splitlines()` splits on TEN characters; a regex `.` excludes only
# "\n" of them, and `\s` MATCHES all ten. So a line-oriented pattern written
# with `.` and `\s` still accepts nine separators — which is the same defect
# as the `$` above, one layer down. This class is "any character that is not
# one of the ten", and tests/test_anchored_patterns.py checks the list
# against `str.splitlines()` itself rather than trusting it.
_ON_ONE_LINE = r"[^\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]"

_HEADING = re.compile(rf"(#{{1,6}})[ \t]+({_ON_ONE_LINE}*?)[ \t]*#*[ \t]*")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Section:
    """One numbered, top-level section of a document.

    `number` is what the user says out loud. It is 1-based, contiguous, and
    derived from nothing but the text — which is why the page and JARVIS
    cannot disagree about it.
    """
    number: int
    title: str
    level: int
    body: str

    @property
    def text(self) -> str:
        """The section as it appears in the file, heading included."""
        head = f"{'#' * self.level} {self.title}"
        return f"{head}\n{self.body}" if self.body else head


@dataclass
class Document:
    """A parsed document: what comes before section 1, and the sections."""
    preamble: str = ""
    sections: list[Section] = field(default_factory=list)
    title: str = ""


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(line index, level, title) for every heading OUTSIDE a code fence.

    The fence check is not fussiness. A spec that quotes a Markdown example —
    and these documents are about software, so they do — would otherwise pick
    up the example's headings as sections and shift every number after it.
    One shifted number is a user saying "drop five" and losing section six.
    """
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for index, line in enumerate(text.splitlines()):
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif line.strip().startswith(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING.fullmatch(line)
        if heading and heading.group(2):
            found.append((index, len(heading.group(1)), heading.group(2)))
    return found


def _section_level(headings: list[tuple[int, int, str]]) -> int:
    """Which heading level counts as a top-level section.

    Normally the shallowest level in the document. The exception is the
    document's own name: a spec written by `builds.render_spec` opens with a
    single `# <topic> — Design` and then uses `##` throughout. Numbering that
    title as section 1 would make every spoken number one too high for the
    whole document, which is precisely the failure this surface exists to
    prevent — so a LONE shallowest heading, sitting first, is the title, and
    the sections are one level in.
    """
    levels = [level for _, level, _ in headings]
    top = min(levels)
    if len(levels) > 1 and levels[0] == top and levels.count(top) == 1:
        deeper = [level for level in levels if level > top]
        if deeper:
            return min(deeper)
    return top


def parse_document(text: str) -> Document:
    """Split a document into its preamble and its numbered sections."""
    lines = (text or "").splitlines()
    headings = _headings(text or "")
    if not headings:
        return Document(preamble="\n".join(lines), sections=[], title="")

    level = _section_level(headings)
    starts = [h for h in headings if h[1] == level]

    title = ""
    if headings[0][1] < level:
        title = headings[0][2]

    first = starts[0][0] if starts else len(lines)
    preamble_lines = lines[:first]
    if title:
        # Drop the title's own line from the preamble: it is the document's
        # name, shown as such, not part of the body.
        preamble_lines = [ln for i, ln in enumerate(lines[:first])
                          if i != headings[0][0]]

    sections: list[Section] = []
    for number, (index, _, heading_title) in enumerate(starts, start=1):
        end = starts[number][0] if number < len(starts) else len(lines)
        body = "\n".join(lines[index + 1:end]).strip("\n")
        sections.append(Section(number=number, title=heading_title,
                                level=level, body=body))

    return Document(preamble="\n".join(preamble_lines).strip("\n"),
                    sections=sections, title=title)


def sections_of(text: str) -> list[Section]:
    """The numbered sections of a document. THE numbering — see the module
    docstring. Both the page and JARVIS come through here."""
    return parse_document(text).sections


def section_number(text: str, number: int) -> Section | None:
    """The section the user just said a number for, or None if there isn't one."""
    for section in sections_of(text):
        if section.number == number:
            return section
    return None


# ---------------------------------------------------------------------------
# Paths — a string out of a URL is not a path until it has been proved one
# ---------------------------------------------------------------------------

def _document_dir(relative: Path) -> str | None:
    """Which of the two document directories this path sits directly in."""
    parent = relative.parent.as_posix()
    return parent if parent in DOCUMENT_DIRS else None


def resolve_document(project_path: str, relative: str) -> Path | None:
    """The real file `relative` names inside the project, or None.

    Containment is `repo_read.resolve_within`, which resolves BOTH sides
    before comparing — a `..`, an absolute path and a symlink pointing out of
    the project all fail there, and a string prefix test has never been
    enough. What that returns is then narrowed twice more: the file must sit
    directly in `docs/superpowers/specs` or `docs/superpowers/plans`, and it
    must be a Markdown file.

    None, never an exception and never a reason — the caller answers 404 for
    every miss, because telling a prober which of their attempts were
    traversal attempts buys them information and buys us nothing.
    """
    root = Path(project_path)
    try:
        real = repo_read.resolve_within(root, relative)
    except Exception:
        return None
    try:
        inside = real.relative_to(Path(root).resolve())
    except ValueError:
        try:
            inside = real.relative_to(Path(root))
        except ValueError:
            return None
    if _document_dir(inside) is None:
        return None
    if real.suffix.lower() != ".md":
        return None
    if not real.is_file():
        return None
    return real


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Approval — the act, recorded, in a file
# ---------------------------------------------------------------------------

def digest_of(text: str) -> str:
    """What was approved, in 64 characters. Any edit changes it."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def approval_filename(relative: str) -> str:
    """The record's filename, derived from the document's path.

    Flattened rather than mirrored, so the approvals directory is one flat
    list a person can read. The name is built from an already-contained
    relative path, so it carries no traversal of its own.
    """
    stem = Path(relative).as_posix()
    for prefix in DOCUMENT_DIRS:
        if stem.startswith(prefix + "/"):
            stem = stem[len(prefix) + 1:]
            return f"{Path(prefix).name}__{stem}.json"
    return f"{stem.replace('/', '__')}.json"


def approval_record_path(project_path: str, relative: str) -> Path:
    return Path(project_path) / APPROVAL_DIR / approval_filename(relative)


def record_approval(project_path: str, relative: str,
                    by: str = "voice") -> dict:
    """Write down that the user approved this exact text. Returns the record.

    Blocking. The digest is of the text as it is on disk right now: approving
    a document approves the words that were on the page, and nothing later.
    """
    path = resolve_document(project_path, relative)
    if path is None:
        raise ValueError("no such document")
    text = _read(path)
    if text is None:
        raise ValueError("unreadable document")

    record = {
        "document": Path(relative).as_posix(),
        "digest": digest_of(text),
        "approved_at": time.time(),
        "approved_by": by,
        "sections": len(sections_of(text)),
    }
    target = approval_record_path(project_path, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _stored_approval(project_path: str, relative: str) -> dict | None:
    try:
        raw = approval_record_path(project_path, relative).read_text(
            encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        # A corrupt record is not a yes. Read as "nobody approved this".
        return None
    return record if isinstance(record, dict) else None


def approval_of(project_path: str, relative: str,
                text: str | None = None) -> dict:
    """The approval state of one document: awaiting, approved or superseded.

    * **awaiting** — no record. Nobody has said yes to this text.
    * **approved** — a record whose digest is the text on disk.
    * **superseded** — a record for text that has since been revised. The
      revision did not inherit the yes the old words were given; it needs the
      user's eye again.
    """
    if text is None:
        path = resolve_document(project_path, relative)
        text = _read(path) if path is not None else None
    record = _stored_approval(project_path, relative)
    if record is None or text is None:
        return {"state": "awaiting", "approved_at": None, "approved_by": ""}
    approved = record.get("digest") == digest_of(text)
    return {
        "state": "approved" if approved else "superseded",
        "approved_at": float(record.get("approved_at") or 0.0),
        "approved_by": str(record.get("approved_by") or ""),
    }


# ---------------------------------------------------------------------------
# Documents and the two states
# ---------------------------------------------------------------------------

def _section_of_task(text: str, task) -> int:
    """The section number the plan's `## Task N` heading was numbered as.

    A plan's own "Task 4" and the surface's "section 4" are usually the same
    number and occasionally are not — a plan that opens with a `## Goal`
    heading shifts every task down one. The user says the number they can
    SEE, so the current task is reported as its section number, found by
    matching the heading rather than by assuming the two counts line up.
    """
    for section in sections_of(text):
        if builds.task_number_of(section.title) == task.number:
            return section.number
    return 0


def _progress_of(text: str) -> dict | None:
    """Task progress off a plan's checkboxes, through `builds.parse_plan`.

    Not re-implemented here on purpose: `build_status` already speaks from
    that parser, and two parsers would eventually disagree about how far a
    build had got — which is the class of lie the run pipeline exists to stop.
    """
    tasks = builds.parse_plan(text)
    if not tasks:
        return None
    current = next((t for t in tasks if not t.done), None)
    return {
        "done": sum(1 for t in tasks if t.done),
        "total": len(tasks),
        "current": current.title if current is not None else "",
        "current_section": _section_of_task(text, current) if current else 0,
        "steps_done": sum(t.steps_done for t in tasks),
        "steps_total": sum(t.steps_total for t in tasks),
    }


def _document_meta(project_path: str, path: Path, kind: str) -> dict | None:
    text = _read(path)
    if text is None:
        return None
    relative = f"{builds.SPEC_DIR if kind == 'spec' else builds.PLAN_DIR}/{path.name}"
    parsed = parse_document(text)
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return {
        "path": relative,
        "kind": kind,
        "title": parsed.title or path.stem,
        "modified": modified,
        "sections": len(parsed.sections),
        "approval": approval_of(project_path, relative, text),
        "progress": _progress_of(text) if kind == "plan" else None,
    }


def list_documents(project_path: str) -> list[dict]:
    """Every spec and plan in a project, newest first. Metadata only.

    Bodies are fetched one at a time by `read_document`: the list is a list,
    and shipping every spec in a project down one endpoint would make opening
    the tab as expensive as reading all of them.
    """
    out: list[dict] = []
    root = Path(project_path)
    for directory, kind in KIND_OF_DIR.items():
        try:
            found = sorted((root / directory).glob("*.md"))
        except OSError:
            continue
        for path in found:
            if not path.is_file():
                continue
            meta = _document_meta(project_path, path, kind)
            if meta is not None:
                out.append(meta)
    # Newest first. Two files can share a modification time — Windows
    # stamps writes at the system-timer tick, so a plan written right after
    # its spec ties with it more often than not — and a tie is broken the
    # way the files are made: a plan comes from a spec, so it is the later
    # one; then by path, so the answer never depends on directory order.
    out.sort(key=lambda d: (d["modified"], d["kind"] == "plan", d["path"]),
             reverse=True)
    return out


def read_document(project_path: str, relative: str) -> dict | None:
    """One document, numbered, with its approval state and its progress.

    This is what the page renders and what `review_document` answers from —
    the same sections, from the same `sections_of` call.
    """
    path = resolve_document(project_path, relative)
    if path is None:
        return None
    text = _read(path)
    if text is None:
        return None
    relative = Path(relative).as_posix()
    kind = KIND_OF_DIR.get(Path(relative).parent.as_posix(), "spec")
    parsed = parse_document(text)
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return {
        "path": relative,
        "kind": kind,
        "title": parsed.title or path.stem,
        "modified": modified,
        "preamble": parsed.preamble,
        "sections": [{"number": s.number, "title": s.title,
                      "level": s.level, "body": s.body}
                     for s in parsed.sections],
        "approval": approval_of(project_path, relative, text),
        "progress": _progress_of(text) if kind == "plan" else None,
    }


def outline(project_path: str, relative: str) -> list[tuple[int, str]]:
    """(number, title) for every section — the list JARVIS reads from.

    Deliberately built out of `read_document`, not out of a second parse: the
    guarantee that a number means the same thing to the user and to JARVIS is
    that there is one parse and both sides are handed its output.
    """
    doc = read_document(project_path, relative)
    if doc is None:
        return []
    return [(s["number"], s["title"]) for s in doc["sections"]]


def project_review(project_path: str) -> dict | None:
    """What this project needs from the user, or None if it has no documents.

    Four states, and only the first two are the ones the user asked for —
    a document awaiting approval, and finished work awaiting review. The two
    in between are what the surface shows while the work is happening.

    * **awaiting** — something needs an eye: never approved, or revised since
      it was. This outranks everything: an unapproved document is the whole
      reason the page exists.
    * **planning** — approved, and the session has not written a plan yet.
    * **building** — a plan with work left on it.
    * **review** — every task on the plan ticked: finished work, waiting to be
      looked at.
    """
    documents = list_documents(project_path)
    if not documents:
        return None

    plans = [d for d in documents if d["kind"] == "plan" and d["progress"]]
    plan = max(plans, key=lambda d: d["modified"]) if plans else None
    progress = plan["progress"] if plan else None

    pending = [d for d in documents if d["approval"]["state"] != "approved"]
    if pending:
        state = "awaiting"
    elif progress is None:
        state = "planning"
    elif progress["done"] >= progress["total"]:
        state = "review"
    else:
        state = "building"

    return {
        "state": state,
        "documents": documents,
        "progress": progress,
        "plan_path": plan["path"] if plan else "",
        "awaiting": [d["path"] for d in pending],
        "modified": max(d["modified"] for d in documents),
    }
