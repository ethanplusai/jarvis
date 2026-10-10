"""Creating a brand-new project directory from a name someone said out loud.

The name arrives by speech, through an LLM. Nothing about it is trustworthy:
it may contain path separators, `..`, an absolute path, a leading dot, or
whatever a microphone turned "cost flex" into. So the name is validated
against an allowlist first, and then — belt and braces — the final path is
resolved and its parent compared with the resolved root. String checks alone
have never been enough to prove containment.

Two things this module will never do:

1. **Reuse or overwrite an existing directory.** The directory is created with
   `mkdir(exist_ok=False)`, which is atomic: there is no window between "does
   it exist?" and "create it" for something else to fill. An existing name is
   reported back, not adopted.
2. **Delete anything.** There is no removal path here at all, not even to
   clean up a half-made project.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("jarvis.project_maker")

# Where new projects go. `~/Projects` by default; JARVIS_PROJECTS_DIR moves it
# (the test suite points it at a tmp_path so nothing is ever created in the
# user's real tree).
DEFAULT_ROOT = "~/Projects"

MAX_NAME_CHARS = 64

# An allowlist, not a denylist. It must start with a letter or a digit, which
# is what rejects `.hidden`, `..`, `../evil` and `/etc/passwd` before any
# path is built — a separator is simply not a character this pattern admits.
# No `$`, and used with `fullmatch`: `$` matches before a trailing newline,
# so `.match()` accepted "chitauri\n" as a directory name.
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")

# Characters that can only ever be an attempt to leave the projects root, or
# to name something the shell and the filesystem disagree about. These are
# REFUSED, never slugified away: silently turning `../evil` into `evil` would
# create a project under a name the user never said.
_FORBIDDEN_CHARS = ("/", "\\", "\x00", "~", ":")

# Every apostrophe a microphone and an LLM between them can produce. Dropped
# rather than dashed, so "Tony Stark's website" is `tony-starks-website` and
# not `tony-stark-s-website`.
_APOSTROPHES = "'‘’ʼ´`"

_GIT_TIMEOUT_SEC = 10.0


class BadName(ValueError):
    """The name cannot be used for a directory. Nothing was created."""


def projects_root() -> Path:
    raw = os.getenv("JARVIS_PROJECTS_DIR") or DEFAULT_ROOT
    return Path(raw).expanduser()


def _slugify(name: str) -> str:
    """"Tony Stark's website" -> "tony-starks-website".

    Only the HUMAN parts of a name are transformed here — case, spaces,
    punctuation people say out loud. Everything dangerous has already been
    refused by `sanitise_name` before this is reached; this function is not
    a sanitiser and must never be used as one.
    """
    slug = name.lower()
    slug = "".join(ch for ch in slug if ch not in _APOSTROPHES)
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def sanitise_name(raw: str) -> str:
    """The spoken name as a safe single directory name, or BadName.

    People name things in English — "Tony Stark's website", "My App" — and
    refusing that ("letters, numbers, dashes and underscores only") is
    friction for no safety gained, because the dangerous shapes are a
    separate, much smaller set. So: refuse the dangerous shapes OUTRIGHT,
    then slugify what is left.

    Refused, never repaired: anything holding a path separator, `..`, a
    leading dot, or a home-relative or absolute path. Those are attempts to
    leave the projects root, and a repaired one would create a directory
    under a name nobody said.
    """
    name = (raw or "").strip()
    if not name:
        raise BadName("empty")
    if len(name) > MAX_NAME_CHARS:
        raise BadName("too long")
    if any(ch in name for ch in _FORBIDDEN_CHARS):
        raise BadName("path separator")
    if ".." in name:
        raise BadName("traversal")
    if name.startswith("."):
        raise BadName("leading dot")

    slug = _slugify(name)
    if not slug:
        raise BadName("nothing usable")
    # Belt and braces: whatever the slugifier produced must still satisfy the
    # allowlist the rest of this module was written against.
    if ".." in slug or not _SAFE_NAME.fullmatch(slug):
        raise BadName("unsafe characters")
    if len(slug) > MAX_NAME_CHARS:
        raise BadName("too long")
    return slug


def target_for(name: str, root: Path) -> Path:
    """The directory `name` would occupy under `root`, proven contained.

    `name` has already been through `sanitise_name`. This is the second,
    independent check: resolve both sides and require that the target's
    parent IS the root. A symlink somewhere in the root's own path is
    resolved on both sides, so it cannot make the comparison lie.
    """
    root_real = Path(os.path.realpath(str(root)))
    target = root_real / name
    resolved = Path(os.path.realpath(str(target)))
    if resolved.parent != root_real or resolved.name != name:
        raise BadName("escapes the projects root")
    return target


def _readme(name: str, description: str) -> str:
    body = description.strip() or f"{name} — created by JARVIS."
    return f"# {name}\n\n{body}\n"


async def _git_init(path: Path) -> bool:
    """`git init` in the new directory. False if git is absent or unhappy.

    Never raises: a project without a git repository is still a project, and
    the caller says so rather than failing the whole thing.
    """
    git = shutil.which("git")
    if not git:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            git, "init", "--quiet", cwd=str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
    except OSError as e:
        log.warning("git init could not start in %s: %s", path, e)
        return False
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=_GIT_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        log.warning("git init timed out in %s", path)
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        return False
    return rc == 0


async def create(raw_name: str, description: str = "",
                 root: Path | None = None) -> dict:
    """Make one new project. Raises BadName; otherwise always returns a dict.

    {"created": False, "reason": "exists", ...} means the name is taken and
    the existing directory was left completely untouched.
    """
    name = sanitise_name(raw_name)
    root = root or projects_root()
    root.mkdir(parents=True, exist_ok=True)
    target = target_for(name, root)
    root_real = Path(os.path.realpath(str(root)))

    try:
        # exist_ok=False, deliberately: atomic, and the only guard that
        # cannot be raced. An existing directory is never adopted.
        target.mkdir(exist_ok=False)
    except FileExistsError:
        return {"created": False, "reason": "exists", "name": name,
                "path": str(target), "root": str(root_real),
                "root_name": root_real.name}

    (target / "README.md").write_text(_readme(name, description),
                                      encoding="utf-8")
    git_ok = await _git_init(target)
    log.info("created project %s at %s (git=%s)", name, target, git_ok)
    return {"created": True, "name": name, "path": str(target),
            "root": str(root_real), "root_name": root_real.name,
            "git": git_ok}
