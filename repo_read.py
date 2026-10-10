"""Reading a repository, cheaply.

JARVIS could see what SESSIONS were doing and knew nothing about the CODE.
Asking "what does chitauri actually do" or "where's the auth logic" meant
spawning a run: minutes of wall clock and a slice of the subscription, for a
question a grep answers in milliseconds.

So these are primitives, not intelligence. No model, no `claude` subprocess,
no network — three bounded filesystem reads that the brain composes. It
already reasons; this gives it eyes.

Everything here is SYNCHRONOUS and blocking on purpose. The voice loop must
never wait on a disk walk, so `server.py` calls all of it through
`asyncio.to_thread`. The one asynchronous entry point is `search`, which
prefers ripgrep when it is installed and otherwise falls back to the pure
walk below — no new dependency either way.

Two rules govern every function:

1. **Bounded.** A repository can be a million files across a network mount.
   Every walk has a depth, a file count, a byte budget and a wall-clock
   deadline, and stops at whichever it hits first. A truncated answer that
   arrives in 40 ms beats a complete one that stalls the microphone.
2. **Nothing sensitive, ever.** The user's home directory is itself a
   "project" on this machine, which makes containment alone far too
   permissive — `~/.ssh/id_rsa` is "inside a project". So containment is the
   floor, not the ceiling: `sensitive_reason` refuses credentials, keys,
   dotfile directories and `.env` outright, even inside a legitimate repo,
   and the walk never lists or greps them either.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

# --- bounds ---------------------------------------------------------------
#
# Measured against this repository (≈190 tracked files, a node_modules and a
# .venv both skipped): the overview walk lands around 15 ms and a full-text
# search around 60 ms. The budgets below are perhaps ten times what a real
# project needs, and exist only so that a pathological tree — a mounted
# volume, a checked-in dataset — cannot hold the voice loop open.

MAX_DEPTH = 8               # directories below the project root
MAX_DIRS = 4_000            # directories entered in one walk
MAX_FILES = 20_000          # files considered in one walk
MAX_WALK_SECONDS = 2.0      # wall clock for the walk itself

SEARCH_MAX_FILE_BYTES = 512_000     # a file bigger than this is not searched
SEARCH_BYTE_BUDGET = 32 * 1024 * 1024
SEARCH_SECONDS = 3.0
SEARCH_MAX_HITS = 8                 # hits actually reported
SEARCH_HIT_COUNT_CAP = 500          # hits counted before we stop counting
SEARCH_LINE_CHARS = 100

READ_MAX_LINES = 60
READ_LEAD_LINES = 8         # lines shown BEFORE the line asked about
READ_MAX_CHARS = 1_100      # sits under server._WRAP_CONTENT_CAP (1200)
READ_MAX_FILE_BYTES = 4 * 1024 * 1024

README_CHARS = 340

# Directories that are build output, dependencies or version-control
# plumbing. Skipping them is what makes the walk fast AND what makes the
# answer useful — "node_modules" is not what this project is.
IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor",
    "dist", "build", "out", "target", "coverage", "htmlcov",
    ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".nox", ".gradle", ".idea", "pods",
    ".next", ".nuxt", ".svelte-kit", ".parcel-cache", ".turbo", ".cache",
    "site-packages", ".terraform", ".eggs", "obj",
})

# Extensions never worth reading aloud or grepping: binaries, media, and
# machine-written lock files nobody asks a question about.
BINARY_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".bmp", ".tiff",
    ".mp3", ".mp4", ".wav", ".aiff", ".mov", ".avi", ".m4a", ".ogg", ".webm",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar",
    ".so", ".dylib", ".dll", ".a", ".o", ".pyc", ".pyo", ".class", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".db", ".sqlite", ".sqlite3", ".bin", ".dat", ".pack", ".idx",
    ".exe", ".img", ".dmg", ".iso",
})

LOCKFILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "bun.lockb", "uv.lock",
})

# --- the sensitive-file wall ---------------------------------------------
#
# Checked against the path RELATIVE to the project root, never the absolute
# one. This repository lives under `.claude/worktrees/`, so testing the
# absolute path against a dot-directory rule would refuse the whole project;
# testing the relative path refuses `~/.ssh/id_rsa` when home is the project
# and lets `server.py` through when this worktree is.

SENSITIVE_DIRS = frozenset({
    ".ssh", ".aws", ".gnupg", ".gpg", ".kube", ".docker", ".gcloud", ".azure",
    ".config", ".password-store", ".netrc", ".npm", ".yarn", ".cargo",
    ".local", ".mozilla", ".keychain", ".keys", "secrets", ".secrets",
    "library", ".authinfo", ".subversion", ".gem", ".m2",
})

# A filename containing any of these is refused. Deliberately narrow strings
# — "token" is not here, because `tokenizer.py` is ordinary code.
SENSITIVE_NAME_PARTS = (
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credential", "secrets.", ".secret", "passwd", "shadow", "htpasswd",
    "private_key", "privatekey", "apikey", "api_key", "access_key",
)

SENSITIVE_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk", ".kdbx",
})

SENSITIVE_EXACT = frozenset({
    ".netrc", ".pypirc", ".git-credentials", ".npmrc", ".htpasswd",
    "credentials", "credentials.json", ".claude.json", ".dockercfg",
    "known_hosts", "authorized_keys", ".bash_history", ".zsh_history",
    ".python_history", ".sqlite_history", ".viminfo",
    # JARVIS's own loopback bearer token (`data_paths.tool_token_path()`),
    # the single thing standing between a local process and every acting
    # tool he has. It lives in `<data>/jarvis/`, and `data/` defaults to a
    # directory INSIDE JARVIS's own repository — which he can now read. An
    # exact name, not a "token" fragment in SENSITIVE_NAME_PARTS, because
    # `tokenizer.py` is ordinary code and must stay readable.
    "tool-token",
})

# A `.env` is a secret; a `.env.example` is documentation, and is often the
# single most useful file for answering "what does this project need".
SAFE_ENV_NAMES = frozenset({
    ".env.example", ".env.sample", ".env.template", ".env.dist",
    "env.example", ".env.defaults",
})

# Dot-prefixed names that are ordinary project furniture. Anything else
# beginning with a dot is refused unread: on a machine where home is a
# project, the dotfiles are exactly where the credentials live, and guessing
# which unknown dotfile is harmless is not a game worth playing.
SAFE_DOT_NAMES = frozenset({
    ".github", ".gitlab", ".gitignore", ".gitattributes", ".gitmodules",
    ".vscode", ".editorconfig", ".dockerignore", ".nvmrc", ".node-version",
    ".python-version", ".ruby-version", ".tool-versions", ".prettierignore",
    ".eslintignore", ".flake8", ".isort.cfg", ".coveragerc", ".babelrc",
    ".claude", ".agents", ".superpowers", ".husky", ".changeset", ".circleci",
    ".well-known", ".storybook", ".devcontainer", ".readthedocs.yaml",
})

SAFE_DOT_PREFIXES = (".eslintrc", ".prettierrc", ".stylelintrc", ".babelrc",
                     ".markdownlint", ".yamllint")

LANGUAGES = {
    ".py": "Python", ".pyi": "Python",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".rs": "Rust", ".go": "Go", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".m": "Objective-C",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".cs": "C#", ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir",
    ".sh": "Shell", ".zsh": "Shell", ".bash": "Shell",
    ".sql": "SQL", ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".less": "CSS",
    ".md": "Markdown", ".rst": "reStructuredText",
}

# Counted, but never the headline: "mostly JSON" tells nobody anything.
NOT_A_HEADLINE = frozenset({"Markdown", "reStructuredText"})

README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme",
                "readme.markdown")


class Refused(Exception):
    """A path JARVIS will not touch. The message is what he says aloud."""


# --- paths ----------------------------------------------------------------

def sensitive_reason(relative: Path) -> str | None:
    """Why this path (relative to the project root) must not be read.

    None means it is fine. Anything else is a sentence fragment for the log;
    the spoken refusal is fixed, because telling a caller precisely which
    rule it tripped is a probing oracle.
    """
    parts = [p for p in relative.parts if p not in ("", ".")]
    for i, raw in enumerate(parts):
        part = raw.lower()
        is_last = i == len(parts) - 1

        if part in SENSITIVE_DIRS or part in SENSITIVE_EXACT:
            return f"{raw} is a sensitive name"
        if any(fragment in part for fragment in SENSITIVE_NAME_PARTS):
            return f"{raw} looks like a credential"
        if is_last and Path(part).suffix in SENSITIVE_SUFFIXES:
            return f"{raw} looks like a key"
        if part.startswith(".env"):
            if part not in SAFE_ENV_NAMES:
                return f"{raw} is an environment file"
            continue
        if part.startswith("."):
            if part in SAFE_DOT_NAMES or part.startswith(SAFE_DOT_PREFIXES):
                continue
            return f"{raw} is a dotfile JARVIS does not know"
    return None


# --- JARVIS's own data is not part of anybody's project -------------------
#
# `data_dir()` defaults to `<repo>/data`, and `_repo_project` resolves
# "yourself" to that repository — so `read_file` on JARVIS's own source
# reached his whole data directory. Confirmed live:
# `{"project": "jarvis", "path": "data/jarvis/mcp.json"}` returned the
# tool-token file's path and every `env` block the user had written into
# `connections.json`, which is the file they are told to paste credentials
# into. `read_file` is not an acting tool, so no origin gate stood in the
# way; `WebFetch` is the CLI's own ungated tool, so the exfiltration leg was
# free.
#
# `SENSITIVE_EXACT` held "tool-token" and nothing else in there. `mcp.json`,
# `connections.json`, `MEMORY.md`, `memory/`, `projects/`, `journal/` and
# `jarvis.db` were all readable, and the last four are what `CLAUDE.md`
# `@`-imports into every turn as trusted system text.
#
# By ABSOLUTE PATH and not by name, deliberately: a project may legitimately
# hold its own `mcp.json` or `MEMORY.md`, and the user is entitled to ask
# about those. The whole data directory goes in one line rather than seven
# filenames, so a file added to it later is covered without anybody
# remembering to come back here.
#
# `data_paths` is imported for this. It is a leaf module — no model, no
# subprocess, no network — and making the wall unconditional is worth more
# than keeping this file import-free: a hook the caller has to install is a
# hook somebody forgets.

_PRIVATE_ROOTS_CACHE: tuple = (object(), (), ())


def _identity(path) -> tuple | None:
    """`(st_dev, st_ino)` for a path, or None if it cannot be stat'd.

    This is what "the same directory" MEANS to the kernel. Every other
    answer — a string, a case-folded string, a resolved string — is a guess
    about spelling, and the guesses are what let `DATA/` walk past the wall.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def jarvis_private_roots() -> tuple[Path, ...]:
    """Absolute directories JARVIS will not read out of ANY project.

    Cached against `JARVIS_DATA_DIR` rather than computed per file: this is
    consulted inside a walk over as many as 20,000 entries, and `data_dir()`
    creates the directory as a side effect.
    """
    return _private_roots()[0]


def _private_roots() -> tuple[tuple[Path, ...], tuple]:
    """The roots, and their `(st_dev, st_ino)` identities in the same order.

    An identity may be None if the directory has since gone; the string
    comparisons below still stand in that case.
    """
    global _PRIVATE_ROOTS_CACHE
    key = os.getenv("JARVIS_DATA_DIR")
    cached_key, cached, ids = _PRIVATE_ROOTS_CACHE
    if cached and cached_key == key:
        return cached, ids
    try:
        import data_paths
        cached = (Path(os.path.realpath(str(data_paths.data_dir()))),)
    except Exception:                                # pragma: no cover
        cached = ()
    ids = tuple(_identity(str(r)) for r in cached)
    _PRIVATE_ROOTS_CACHE = (key, cached, ids)
    return cached, ids


# `os.stat` on the ancestor is the last of the three tests and by far the
# most expensive, so its answer is remembered for the run of a walk: one
# directory holds many files and they all share the ancestor being checked.
# Bounded so a long-lived process cannot grow it without limit.
_ANCESTOR_ID_CACHE: dict[str, tuple | None] = {}
_ANCESTOR_ID_CACHE_MAX = 4096


def _cached_identity(path: str) -> tuple | None:
    got = _ANCESTOR_ID_CACHE.get(path, False)
    if got is not False:
        return got                                   # type: ignore[return-value]
    ident = _identity(path)
    if len(_ANCESTOR_ID_CACHE) >= _ANCESTOR_ID_CACHE_MAX:
        _ANCESTOR_ID_CACHE.clear()
    _ANCESTOR_ID_CACHE[path] = ident
    return ident


def _under(absolute: Path, root: Path, root_id: tuple | None) -> bool:
    """Is `absolute` the private root, or inside it?

    Four tests, cheapest first, and every one of them is needed:

    1. The strings match. The ordinary case, and free.
    2. They match case-insensitively. `realpath` does not case-normalise on
       macOS, so `DATA/jarvis/mcp.json` and `data/jarvis/mcp.json` are the
       same file with different spellings. Fail CLOSED: on a case-sensitive
       volume this refuses a genuinely different `DATA/`, which costs the
       user a directory nobody has and buys the same rule on both.
    3. The kernel says the ancestor at the root's own depth IS the root.
       This is the one that is actually true rather than merely usually
       true: it catches a spelling neither string test predicted, a macOS
       firmlink (`/System/Volumes/Data/Users/…` is `/Users/…`), and a bind
       mount. One `stat`, not one per component — containment is decided
       entirely by whether that single directory is the private root.
    4. Failing that, some ancestor is the root by identity at a DIFFERENT
       depth: a symlinked ancestor anywhere above changes the component
       count, so (3)'s arithmetic looks at the wrong directory. Only
       ancestors that share the root's own basename are stat'd, so this
       costs a string compare per component and a syscall almost never.
    """
    parts, rparts = absolute.parts, root.parts
    if len(parts) >= len(rparts):
        prefix = parts[:len(rparts)]
        if prefix == rparts:
            return True
        if [p.casefold() for p in prefix] == [p.casefold() for p in rparts]:
            return True
        if root_id is not None and _cached_identity(str(Path(*prefix))) == root_id:
            return True
    if root_id is None or not rparts:
        return False
    leaf = rparts[-1].casefold()
    for i in range(len(parts), 0, -1):
        if parts[i - 1].casefold() == leaf \
                and _cached_identity(str(Path(*parts[:i]))) == root_id:
            return True
    return False


def private_reason(absolute) -> str | None:
    """Why this absolute path is JARVIS's own private data, or None.

    The caller is responsible for having resolved symlinks first where that
    matters — `resolve_within` does, so a link out of a project and into the
    brain home fails exactly as a `..` does. A symlink whose TARGET is
    mis-cased survives `realpath` with the mis-cased spelling intact, which
    is why `_under` does not trust the spelling.
    """
    absolute = Path(absolute)
    roots, ids = _private_roots()
    for root, root_id in zip(roots, ids):
        if _under(absolute, root, root_id):
            return f"{absolute.name} is JARVIS's own data"
    return None


def resolve_within(root: Path, target: str) -> Path:
    """The real path `target` names inside `root`, or raise `Refused`.

    Both sides are resolved before they are compared, so a symlink out of
    the project, a `..` and an absolute path elsewhere all fail the same
    way — a string prefix test has never been enough. Same reasoning as
    `project_maker.target_for` and `server._inside_a_project`.
    """
    real_root = Path(os.path.realpath(str(root)))
    raw = Path(target or "").expanduser()
    candidate = raw if raw.is_absolute() else real_root / raw
    real = Path(os.path.realpath(str(candidate)))

    if real != real_root and real_root not in real.parents:
        raise Refused("outside")

    relative = real.relative_to(real_root) if real != real_root else Path(".")
    if sensitive_reason(relative) or private_reason(real):
        raise Refused("sensitive")
    return real


def _skip_dir(name: str) -> bool:
    lowered = name.lower()
    if lowered in IGNORED_DIRS:
        return True
    # The sensitive wall applies to the WALK, not only to a named path.
    # Measured live with the user's home directory as the project: without
    # this, `Library/` was descended into (58,000 files, several of them
    # iCloud placeholders that block for seconds on read) and a search could
    # in principle have surfaced a line out of it. `.ssh` and `.aws` were
    # already caught by the dot rule below; `Library` and `secrets` were not.
    if lowered in SENSITIVE_DIRS:
        return True
    if name.startswith(".") and lowered not in SAFE_DOT_NAMES:
        return True
    return False


def _skip_file(name: str) -> bool:
    lowered = name.lower()
    if lowered in LOCKFILES:
        return True
    if Path(lowered).suffix in BINARY_EXTS:
        return True
    return sensitive_reason(Path(name)) is not None


# --- the one bounded walk everything shares -------------------------------

class Walk:
    """What one bounded traversal found, and whether it ran out of budget."""

    def __init__(self):
        self.files: list[tuple[str, int]] = []   # (path relative to root, bytes)
        self.dirs = 0
        self.complete = True


def walk(root: Path, deadline: float | None = None) -> Walk:
    """Breadth-first, skipping the ignore list, bounded on all four axes.

    Breadth-first on purpose: when the budget runs out, what has been seen is
    the top of the tree — the part that answers "what is this project" —
    rather than an arbitrarily deep corner of it.
    """
    out = Walk()
    stop_at = deadline if deadline is not None else time.monotonic() + MAX_WALK_SECONDS
    # Resolved, so the walk's containment answers cannot depend on how the
    # caller happened to spell the project root — the roots come from a
    # directory scan, and one of them reaching `data/` as `DATA/` used to be
    # enough to make a search grep the brain home.
    root = Path(os.path.realpath(str(root)))
    queue: list[tuple[Path, str, int]] = [(root, "", 0)]
    # Read once for the whole walk, not per entry. Refusing a NAMED path is
    # not enough on its own: a grep would read the user's MCP token out of
    # `mcp.json` line by line without ever naming the file, exactly as the
    # `Library/` case in `_skip_dir` showed.
    private, private_ids = _private_roots()

    def _is_private(path: str) -> bool:
        # Identity, not spelling — the same rule `private_reason` applies,
        # and it has to be the same rule, because a grep never names the
        # file it reads and so never reaches `private_reason` at all.
        p = Path(os.path.abspath(path))
        return any(_under(p, r, i) for r, i in zip(private, private_ids))

    while queue:
        directory, prefix, depth = queue.pop(0)
        if out.dirs >= MAX_DIRS or len(out.files) >= MAX_FILES:
            out.complete = False
            break
        if time.monotonic() > stop_at:
            out.complete = False
            break
        out.dirs += 1
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth + 1 > MAX_DEPTH:
                                out.complete = False
                                continue
                            if _skip_dir(entry.name) or _is_private(entry.path):
                                continue
                            queue.append((Path(entry.path),
                                          f"{prefix}{entry.name}/", depth + 1))
                        elif entry.is_file(follow_symlinks=False):
                            if _skip_file(entry.name) or _is_private(entry.path):
                                continue
                            # Checked HERE as well as at the top of the loop:
                            # one directory holding a million files would
                            # otherwise be scanned in full before anything
                            # noticed the budget was gone.
                            if len(out.files) >= MAX_FILES:
                                out.complete = False
                                break
                            out.files.append(
                                (f"{prefix}{entry.name}", entry.stat().st_size))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


# --- git ------------------------------------------------------------------

def git_branch(root: Path) -> str | None:
    """The checked-out branch, read straight off `.git` — no subprocess.

    Handles a worktree, where `.git` is a FILE holding `gitdir: …`; this
    repository is exactly that, so the plain-directory path alone would have
    reported "not a git repo" for the project being worked in.
    """
    dot = root / ".git"
    try:
        if dot.is_file():
            pointer = dot.read_text(errors="replace").strip()
            if not pointer.startswith("gitdir:"):
                return None
            head = Path(pointer.split(":", 1)[1].strip()) / "HEAD"
        elif dot.is_dir():
            head = dot / "HEAD"
        else:
            return None
        text = head.read_text(errors="replace").strip()
    except OSError:
        return None
    if text.startswith("ref: refs/heads/"):
        return text[len("ref: refs/heads/"):].strip() or None
    return "a detached head" if text else None


# --- the README, said out loud --------------------------------------------

_BADGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<!--.*?-->|<[^>]+>", re.DOTALL)


def readme_opening(root: Path, limit: int = README_CHARS) -> str:
    """The first real prose in the README, trimmed hard.

    Titles, badges and HTML are stripped: "# JARVIS" followed by four shields
    is not an answer to "what is this". Cut at a sentence boundary where one
    is near the limit, so what is spoken ends like a sentence.
    """
    path = None
    try:
        with os.scandir(root) as entries:
            names = {e.name.lower(): e.name for e in entries if e.is_file()}
    except OSError:
        return ""
    for candidate in README_NAMES:
        if candidate in names:
            path = root / names[candidate]
            break
    if path is None:
        return ""
    try:
        raw = path.read_text(errors="replace")[:20_000]
    except OSError:
        return ""

    text = _HTML.sub(" ", _LINK.sub(r"\1", _BADGE.sub("", raw)))
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # A blank line is a paragraph break, not the end of the prose.
            # Breaking here read only "Just A Rather Very Intelligent
            # System." off this project's own README — a tagline, not an
            # answer. Headings and rules below are what actually end it.
            continue
        if stripped.startswith(("#", ">", "---", "===", "|", "```", "***")):
            if lines:
                break
            continue
        lines.append(stripped.replace("**", "").replace("`", ""))
        if sum(len(x) for x in lines) > limit:
            break
    prose = " ".join(lines).strip()
    if len(prose) <= limit:
        return prose
    cut = prose[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop > limit // 2:
        return cut[:stop + 1]
    return cut.rstrip() + "…"


# --- 1. repo_overview -----------------------------------------------------

def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def overview(root: Path, name: str) -> tuple[str, str]:
    """(headline, untrusted body) — the "what is this?" answer.

    The headline is derived facts JARVIS computed: counts, languages, the
    branch. The body is repository CONTENT — the README's own words and its
    own file names — and is what the caller wraps as untrusted.
    """
    # Resolved here as well as inside `walk`, because the top-level listing
    # and the README below use it directly.
    root = Path(os.path.realpath(str(root)))
    found = walk(root)
    files = found.files

    counts: dict[str, int] = {}
    for rel, _size in files:
        language = LANGUAGES.get(Path(rel).suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    headline_langs = [l for l in ranked if l[0] not in NOT_A_HEADLINE] or ranked
    spoken = _join([f"{lang} ({n} files)" if i == 0 else f"{lang} ({n})"
                    for i, (lang, n) in enumerate(headline_langs[:3])])

    total = len(files)
    about = "at least " if not found.complete else ""
    if spoken:
        first = f"{name} — mostly {spoken}; {about}{total} files in all."
    elif total:
        first = f"{name} — {about}{total} files, no code I recognise."
    else:
        first = f"{name} — empty, as far as I can see."

    branch = git_branch(root)
    if branch:
        first += f" A git repository on {branch}."

    body_parts = []
    # The README of the brain home is JARVIS's own, not a project's.
    opening = None if private_reason(root) else readme_opening(root)
    if opening:
        body_parts.append(f"README: {opening}")

    # `_skip_dir`/`_skip_file` are the NAME rules — lockfiles, dotfiles,
    # credential-shaped names. They are not the private-data wall, and this
    # listing used to consult only them. `_resolve_project_or_explain`
    # matches by substring, so `project="jarv"` resolves to `<data>/jarvis`,
    # and the listing then printed `files CLAUDE.md, connections.json,
    # mcp.json` — the walk above had already refused every one of those.
    # The old test pointed at the PARENT project, where the listing never
    # reaches the brain home, which is why it passed.
    try:
        with os.scandir(root) as entries:
            top_dirs = sorted(e.name for e in entries
                              if e.is_dir(follow_symlinks=False)
                              and not _skip_dir(e.name)
                              and not private_reason(e.path))
        with os.scandir(root) as entries:
            top_files = sorted(e.name for e in entries
                               if e.is_file(follow_symlinks=False)
                               and not _skip_file(e.name)
                               and not private_reason(e.path))
    except OSError:
        top_dirs, top_files = [], []

    structure = []
    if top_dirs:
        shown = top_dirs[:8]
        more = f" (+{len(top_dirs) - len(shown)} more)" if len(top_dirs) > len(shown) else ""
        structure.append("folders " + ", ".join(f"{d}/" for d in shown) + more)
    if top_files:
        shown = top_files[:8]
        more = f" (+{len(top_files) - len(shown)} more)" if len(top_files) > len(shown) else ""
        structure.append("files " + ", ".join(shown) + more)
    if structure:
        body_parts.append("Top level: " + "; ".join(structure) + ".")

    return first, "\n".join(body_parts)


# --- 2. search_repo -------------------------------------------------------

class Hits:
    def __init__(self):
        self.lines: list[str] = []      # "path:line: text"
        self.found = 0
        self.capped = False             # more than we bothered to count
        self.tool = "walk"


def _rg_path() -> str | None:
    """Where ripgrep is, or None. Its own function so a test can force the
    pure-Python path, which is what actually runs on a machine without it."""
    return shutil.which("rg")


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) > SEARCH_LINE_CHARS:
        return text[:SEARCH_LINE_CHARS].rstrip() + "…"
    return text


async def _search_rg(root: Path, query: str, binary: str) -> Hits | None:
    """ripgrep, when it is installed. None if it could not be used at all."""
    args = [
        binary, "--fixed-strings", "--ignore-case", "--line-number",
        "--no-heading", "--color", "never", "--no-messages",
        "--max-filesize", str(SEARCH_MAX_FILE_BYTES),
        "--max-count", "3", "--threads", "4",
    ]
    for ignored in sorted(IGNORED_DIRS):
        args += ["--glob", f"!{ignored}/"]
    args += ["--", query, str(root)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (OSError, ValueError):
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), SEARCH_SECONDS)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    if proc.returncode not in (0, 1):
        return None

    hits = Hits()
    hits.tool = "rg"
    for raw in stdout.decode("utf-8", "replace").splitlines():
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        path_text, number, body = parts
        try:
            real = Path(path_text).resolve()
            relative = real.relative_to(Path(os.path.realpath(str(root))))
        except (ValueError, OSError):
            continue
        if sensitive_reason(relative) or private_reason(real):
            continue
        hits.found += 1
        if hits.found > SEARCH_HIT_COUNT_CAP:
            hits.capped = True
            break
        if len(hits.lines) < SEARCH_MAX_HITS:
            hits.lines.append(f"{relative.as_posix()}:{number}: {_clip(body)}")
    return hits


def search_by_walk(root: Path, query: str) -> Hits:
    """The fallback, and on a machine with no ripgrep the only path.

    Case-insensitive LITERAL matching, never a regex. The query came out of a
    microphone via a model, and `re` backtracks: one malformed pattern would
    hang the walk that the voice loop is waiting on. Matching is done on
    bytes so a file is only decoded when it actually contains the needle.
    """
    hits = Hits()
    needle = query.encode("utf-8", "replace").lower()
    if not needle:
        return hits

    deadline = time.monotonic() + SEARCH_SECONDS
    budget = SEARCH_BYTE_BUDGET
    for relative, size in walk(root, deadline=deadline).files:
        if time.monotonic() > deadline or budget <= 0:
            hits.capped = True
            break
        if size > SEARCH_MAX_FILE_BYTES or size == 0:
            continue
        try:
            data = (root / relative).read_bytes()
        except OSError:
            continue
        budget -= len(data)
        if b"\x00" in data[:4096]:          # binary, whatever its extension
            continue
        if needle not in data.lower():
            continue
        per_file = 0
        for number, line in enumerate(data.splitlines(), start=1):
            if needle not in line.lower():
                continue
            hits.found += 1
            per_file += 1
            if len(hits.lines) < SEARCH_MAX_HITS:
                hits.lines.append(
                    f"{relative}:{number}: "
                    f"{_clip(line.decode('utf-8', 'replace'))}")
            if per_file >= 3:
                break
        if hits.found > SEARCH_HIT_COUNT_CAP:
            hits.capped = True
            break
    return hits


async def search(root: Path, query: str) -> Hits:
    """ripgrep if it is on PATH, the bounded walk otherwise."""
    binary = _rg_path()
    if binary:
        result = await _search_rg(root, query, binary)
        if result is not None:
            return result
    return await asyncio.to_thread(search_by_walk, root, query)


# --- 3. read_file ---------------------------------------------------------

class Window:
    def __init__(self):
        self.text = ""
        self.first = 0
        self.last = 0
        self.total = 0
        self.truncated = False
        self.note = ""


def read_window(path: Path, around=None) -> Window:
    """A bounded window on one file — never the whole of a large one.

    `around` is a line number, or a string to find. Either way the answer
    says which lines came back out of how many, and says so explicitly when
    what was returned is not the whole file.
    """
    size = path.stat().st_size
    if size > READ_MAX_FILE_BYTES:
        raise Refused("huge")
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise Refused("binary")
    lines = data.decode("utf-8", "replace").splitlines()

    out = Window()
    out.total = len(lines)
    if not lines:
        out.text = ""
        return out

    centre = None
    if isinstance(around, bool):
        around = None
    if isinstance(around, int):
        centre = around
    elif isinstance(around, str) and around.strip():
        needle = around.strip().lower()
        if needle.isdigit():
            centre = int(needle)
        else:
            for number, line in enumerate(lines, start=1):
                if needle in line.lower():
                    centre = number
                    break
            if centre is None:
                out.note = f"nothing matching {around.strip()} in it"

    if centre is None:
        start = 0
    else:
        centre = max(1, min(centre, out.total))
        # A small lead, not half the window. Measured live against brain.py:
        # centring line 325 with a 30-line lead started at 295, and the
        # character cap then cut the window off at 314 — the answer did not
        # contain the line that was asked about. The context the caller wants
        # is mostly what FOLLOWS the hit anyway.
        start = max(0, centre - 1 - READ_LEAD_LINES)

    text, first, last, clipped = _clip_window(lines, start)
    if centre is not None and last < centre:
        # Long lines ate the lead as well. Put the hit itself on the first
        # line rather than return a window that does not reach it.
        text, first, last, clipped = _clip_window(lines, centre - 1)

    out.first, out.last = first, last
    out.truncated = clipped or first > 1 or last < out.total
    out.text = text
    return out


def _clip_window(lines: list[str], start: int) -> tuple[str, int, int, bool]:
    """(text, first line, last line, was it cut) for a window at `start`."""
    chunk = lines[start:start + READ_MAX_LINES]
    text = "\n".join(chunk)
    clipped = False
    last = start + len(chunk)
    if len(text) > READ_MAX_CHARS:
        text = text[:READ_MAX_CHARS].rstrip()
        last = start + text.count("\n") + 1
        clipped = True
    return text, start + 1, last, clipped
