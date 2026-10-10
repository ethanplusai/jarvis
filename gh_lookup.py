"""GitHub, through `gh`: the fast path for a question about a repository.

The user, out loud: "can you search that open SEO GitHub and read it yourself
so you can see what the license says." Measured on that exact question:

    gh api /repos/{owner}/{repo}/license   0.5s   MIT, exact
    WebFetch through a claude -p turn      9.2s   correct
    WebSearch through a claude -p turn    15.9s   correct, with sources

A large share of what he asks about is repositories, `gh` is installed and
authenticated on this machine, and it answers authoritatively in half a
second. So repositories do not go to a web search.

Three rules hold this file together:

1. **It takes what a person SAYS.** "the arcreactor repo", "my Arc Loop repo" —
   not `owner/name`. Everything arrives through speech recognition.
2. **It never picks.** "arcreactor" really does match five repositories from five
   owners; answering a licence question about the wrong one is worse than
   asking which. One match is an answer, several are a question.
3. **`gh` is a subprocess taking user-derived input.** Every call is an
   argument LIST — there is no shell anywhere in here, so there is nothing
   for a semicolon or a backtick to end — and every call has a deadline.

No new dependency: `gh`, `asyncio` and `json`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
from dataclasses import dataclass, field

log = logging.getLogger("jarvis.gh")

# One `gh` call. An unauthenticated or rate-limited `gh` fails on its own in
# well under a second (measured: 0.4s for `Bad credentials (HTTP 401)`), so
# this is not the path to a spoken error — it is the guard against a call that
# never returns at all, which the caller's own deadline could not clean up.
GH_CALL_TIMEOUT = 5.0

# How many repositories a search may come back with. Enough to hear the
# ambiguity, few enough to say out loud.
SEARCH_LIMIT = 5

# How many of them JARVIS names when he asks which one.
NAMES_WHEN_ASKING = 3

# The top of a README, for "what IS this?". The tool result is capped at 1,500
# characters and the untrusted body at 1,200, so a bigger number here would
# only be cut off by `_wrap_untrusted` — the description and the licence, the
# things actually asked for, must never be the part that falls off the end.
README_CHARS = 700

# GitHub's own grammar for the two halves of a full name. Everything that
# reaches a header line is matched against this, so a name can never carry a
# quote, a newline or an angle bracket into text the brain reads as JARVIS's
# own (the `<title>` that wrote its own untrusted wrapper is the reason).
#
# No `^`, no `$`, and used ONLY with `fullmatch`. Python's `$` matches before
# a trailing newline, so `FULL_NAME_RE.match("owner/name\n")` succeeded and
# the newline went on to `_spoken_repo_name`, which puts the value in a
# sentence with no untrusted block around it. Same defect as
# `server._plain_name`; tests/test_anchored_patterns.py holds every anchored
# pattern in this repository to the rule.
_OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
_NAME = r"[A-Za-z0-9._-]{1,100}"
FULL_NAME_RE = re.compile(rf"{_OWNER}/{_NAME}")
_URL_RE = re.compile(
    rf"(?:https?://)?(?:www\.)?github\.com/({_OWNER})/({_NAME}?)(?:\.git)?/?")

# Words a person says around a repository's name that are not part of it.
# Deliberately short: "open" and "source" are NOT here, because "open SEO" is
# the actual name of the thing he asked about.
_FILLER = {
    "the", "a", "an", "my", "our", "your", "his", "her", "their", "that",
    "this", "repo", "repos", "repository", "repositories", "github",
    "called", "named", "please",
}


@dataclass
class Repo:
    full_name: str
    description: str = ""
    licence: str = ""          # SPDX id, or "" when GitHub detects none
    stars: int = 0
    pushed_at: str = ""        # ISO 8601, as GitHub gives it
    archived: bool = False
    private: bool = False
    readme: str = ""


@dataclass
class Candidate:
    full_name: str
    description: str = ""


@dataclass
class Lookup:
    """One repository, or several to choose between, or a reason for neither.

    `problem` is a short machine-readable cause the caller turns into a
    sentence: no_gh, auth, rate_limited, not_found, timeout, unavailable.
    """
    repo: Repo | None = None
    candidates: list[Candidate] = field(default_factory=list)
    problem: str = ""
    query: str = ""


def gh_path() -> str | None:
    """Where `gh` is, or None. Its own function so a test can move it."""
    return shutil.which("gh")


async def _run_gh(args: list[str], timeout: float) -> tuple[int, str, str]:
    """(returncode, stdout, stderr) for one `gh` call.

    THE seam. An argument list, never a command string: `args` carries a
    repository name that came out of speech recognition by way of a language
    model, and may have come out of somebody's web page before that.
    """
    binary = gh_path()
    if binary is None:
        raise FileNotFoundError("gh")
    import process_tree
    proc = await process_tree.spawn(
        binary, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return (proc.returncode or 0,
                out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
    finally:
        await process_tree.stop(proc, grace=1)


def _problem_from(stderr: str) -> str:
    """What `gh` actually prints, in the shapes it actually prints them:
    `gh: Not Found (HTTP 404)`, `gh: Bad credentials (HTTP 401)`,
    `gh: API rate limit exceeded ... (HTTP 403)`."""
    text = (stderr or "").lower()
    if "rate limit" in text:
        return "rate_limited"
    if ("401" in text or "bad credentials" in text or "gh auth login" in text
            or "authentication" in text):
        return "auth"
    if "404" in text or "not found" in text:
        return "not_found"
    return "unavailable"


def spoken_to_query(spoken: str) -> str:
    """What to search for, out of what the user said."""
    words = [w for w in re.split(r"\s+", str(spoken).strip()) if w]
    kept = []
    for i, word in enumerate(words):
        bare = word.lower().strip(".,'\"")
        if bare in _FILLER:
            continue
        # "on github", "from GitHub" — the preposition goes with the word it
        # belongs to. Bare "on" and "in" stay: a repository may be called
        # `on-the-fly`.
        nxt = words[i + 1].lower().strip(".,'\"") if i + 1 < len(words) else ""
        if bare in ("on", "in", "from", "at") and nxt == "github":
            continue
        kept.append(word)
    return " ".join(kept or words)


def full_name_in(spoken: str) -> str | None:
    """`owner/name` when the user (or the brain) already had it — including
    as a github.com address — otherwise None."""
    text = str(spoken).strip()
    m = _URL_RE.fullmatch(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return text if FULL_NAME_RE.fullmatch(text) else None


def _slugs(query: str) -> set[str]:
    """The forms a spoken name could take as a repository name: "Arc Loop" is
    `arcloop`, `arc-loop` or `arc_loop` on GitHub."""
    low = query.lower().strip()
    squashed = re.sub(r"\s+", "", low)
    return {low, squashed, re.sub(r"\s+", "-", low), re.sub(r"\s+", "_", low)}


class _Session:
    """One lookup. Holds the login so it is fetched at most once."""

    def __init__(self):
        self.login: str | None = None
        self.failed = ""

    async def call(self, args: list[str]) -> tuple[int, str, str] | None:
        """One `gh` call, or None with `self.failed` set."""
        try:
            rc, out, err = await _run_gh(args, GH_CALL_TIMEOUT)
        except FileNotFoundError:
            self.failed = "no_gh"
            return None
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("gh %s timed out", args[:2])
            self.failed = "timeout"
            return None
        except Exception as e:
            log.warning("gh %s failed: %s", args[:2], e)
            self.failed = "unavailable"
            return None
        if rc != 0:
            self.failed = _problem_from(err)
            return None
        return rc, out, err

    async def search(self, query: str, own: bool) -> list[Candidate]:
        args = ["search", "repos", "--limit", str(SEARCH_LIMIT),
                "--json", "fullName,description"]
        if own:
            login = await self.who()
            if not login:
                return []
            args += ["--owner", login]
        # `--` LAST, and the query after it. The query is text a model
        # produced out of speech and may be echoing a page or a README; it
        # went in positionally with nothing in front of it, so one beginning
        # with a dash was read by `gh` as a flag. Low severity — this is
        # argv, there is no shell, and `gh search repos` has no destructive
        # flag to reach — but "an argument the caller chose is data" is the
        # rule this whole seam exists for. `describe`'s `full_name` needs no
        # such thing: `FULL_NAME_RE` requires an alphanumeric first
        # character, so it can never begin with a dash.
        args += ["--", query]
        answer = await self.call(args)
        if answer is None:
            # A search that finds nothing is not a failure to report as one.
            if self.failed == "not_found":
                self.failed = ""
            return []
        try:
            rows = json.loads(answer[1] or "[]")
        except json.JSONDecodeError:
            return []
        out = []
        for row in rows if isinstance(rows, list) else []:
            full = str((row or {}).get("fullName") or "")
            if FULL_NAME_RE.fullmatch(full):
                out.append(Candidate(full, str(row.get("description") or "")))
        return out

    async def who(self) -> str | None:
        if self.login is None:
            answer = await self.call(["api", "user", "--jq", ".login"])
            self.login = (answer[1].strip() if answer else "") or ""
        return self.login or None

    async def describe(self, full_name: str) -> Repo | None:
        """Metadata and README together — two calls, one wait. The README is
        what answers "what IS this?" when the description is a line long."""
        meta_call = self.call(["api", f"repos/{full_name}"])
        readme_call = self.call(["api", f"repos/{full_name}/readme",
                                 "-H", "Accept: application/vnd.github.raw"])
        meta, readme = await asyncio.gather(meta_call, readme_call)
        if meta is None:
            return None
        # A missing README is not a missing repository: plenty have none.
        self.failed = ""
        try:
            data = json.loads(meta[1] or "{}")
        except json.JSONDecodeError:
            self.failed = "unavailable"
            return None
        licence = ((data.get("license") or {}) or {}).get("spdx_id") or ""
        if licence.upper() in ("NOASSERTION", "NULL"):
            licence = ""
        return Repo(
            full_name=str(data.get("full_name") or full_name),
            description=str(data.get("description") or ""),
            licence=str(licence),
            stars=int(data.get("stargazers_count") or 0),
            pushed_at=str(data.get("pushed_at") or ""),
            archived=bool(data.get("archived")),
            private=bool(data.get("private")),
            readme=_readme_text(readme[1] if readme else ""),
        )


def _readme_text(raw: str) -> str:
    """`Accept: raw` gives the file itself. Older gh versions (and the plain
    endpoint) give base64 in a JSON envelope — decode that rather than reading
    a wall of base64 out loud."""
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            blob = json.loads(text).get("content") or ""
            text = base64.b64decode(blob).decode("utf-8", "replace")
        except Exception:
            return ""
    return text[:README_CHARS]


async def look_up(spoken: str) -> Lookup:
    """The whole question: what the user said in, one repository or a choice
    out. Never raises."""
    query = spoken_to_query(spoken)
    result = Lookup(query=query)
    if not query:
        result.problem = "not_found"
        return result
    # Asked before anything is spawned: a machine without `gh` should hear the
    # reason instantly, not after a failed exec.
    if gh_path() is None:
        result.problem = "no_gh"
        return result

    session = _Session()
    named = full_name_in(spoken)
    if named:
        repo = await session.describe(named)
        result.repo = repo
        result.problem = "" if repo else (session.failed or "not_found")
        return result

    # His own first: "my Arc Loop repo" is a repository he owns, and his
    # account is a far smaller haystack than GitHub.
    mine = await session.search(query, own=True)
    if session.failed in ("no_gh", "auth", "rate_limited", "timeout"):
        result.problem = session.failed
        return result
    chosen = _choose(mine, query)
    if chosen is None:
        if len(mine) > 1:
            result.candidates = mine[:NAMES_WHEN_ASKING]
            return result
        theirs = await session.search(query, own=False)
        if session.failed in ("no_gh", "auth", "rate_limited", "timeout"):
            result.problem = session.failed
            return result
        chosen = _choose(theirs, query)
        if chosen is None:
            if len(theirs) > 1:
                result.candidates = theirs[:NAMES_WHEN_ASKING]
            else:
                result.problem = "not_found"
            return result

    repo = await session.describe(chosen.full_name)
    result.repo = repo
    if repo is None:
        result.problem = session.failed or "not_found"
    return result


def _choose(candidates: list[Candidate], query: str) -> Candidate | None:
    """The one obvious answer, or None to ask.

    One candidate is the answer. Several are a question UNLESS exactly one of
    them is named the thing that was asked for — five repositories called
    `arcreactor` are still five, and that is the case this exists to refuse.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    wanted = _slugs(query)
    exact = [c for c in candidates
             if c.full_name.split("/", 1)[1].lower() in wanted]
    return exact[0] if len(exact) == 1 else None
