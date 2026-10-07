"""Single source of truth for where JARVIS writes its data.

Set JARVIS_DATA_DIR to run an isolated instance without touching the
live database.
"""

import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.data_paths")

_DEFAULT = Path(__file__).parent / "data"


def data_dir() -> Path:
    """Return the data directory, creating it if needed."""
    raw = os.getenv("JARVIS_DATA_DIR")
    path = Path(raw).expanduser() if raw else _DEFAULT
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """Return the path to the main SQLite database."""
    return data_dir() / "jarvis.db"


_TEMPLATE_DIR = Path(__file__).parent / "jarvis_home"
_PERSONA_NAME = "CLAUDE.md"
_SEED_NAME = ".claude-md-seed.json"
_CONNECTIONS_NAME = "connections.json"
_CONNECTIONS_SEED_NAME = ".connections-seed.json"


def brain_home() -> Path:
    """Where the brain lives: its cwd, its CLAUDE.md, and (later) its memory."""
    return data_dir() / "jarvis"


def persona_template_path() -> Path:
    """The CLAUDE.md this release ships."""
    return _TEMPLATE_DIR / _PERSONA_NAME


def persona_path() -> Path:
    """The CLAUDE.md the brain actually reads."""
    return brain_home() / _PERSONA_NAME


def persona_seed_path() -> Path:
    """What JARVIS last wrote into `persona_path`, as a hash.

    Beside the file rather than in the database on purpose: the pair travels
    together, a user who copies their brain home to a new machine carries the
    record with it, and deleting it is a safe thing to do (it costs one
    upgrade, never an edit).
    """
    return brain_home() / _SEED_NAME


def connections_template_path() -> Path:
    """The empty, self-explaining connections.json this release ships."""
    return _TEMPLATE_DIR / _CONNECTIONS_NAME


def connections_path() -> Path:
    """The ONE file a user declares their own MCP servers in.

    Beside CLAUDE.md and the generated mcp.json deliberately: the brain's home
    is where everything the brain reads lives, it is outside the repo (so a
    `git pull` cannot touch it), and it is already the directory a user is
    pointed at when they want to change how JARVIS thinks.
    """
    return brain_home() / _CONNECTIONS_NAME


def connections_seed_path() -> Path:
    """What JARVIS last wrote into `connections_path`, as a hash. See
    `persona_seed_path` — same record, same reasons."""
    return brain_home() / _CONNECTIONS_SEED_NAME


# Every CLAUDE.md this project has ever shipped, by sha256 of its bytes.
#
# This exists for exactly one moment: the FIRST run after the self-updating
# persona landed, on an install that has a CLAUDE.md but no seed record. The
# file is then either an untouched older template or the user's own work, and
# the bytes are the only evidence there is. If they match something we
# shipped, nobody has edited it and it is safe to replace; if they match
# nothing, we assume the user wrote it and never touch it again.
#
# APPEND the new hash whenever jarvis_home/CLAUDE.md changes —
# `test_every_template_this_project_has_shipped_is_listed` walks the file's
# git history and fails with the exact line to paste. Forgetting is not a
# crash: it silently marks that release's users "edited", and they receive no
# further improvement.
KNOWN_TEMPLATE_HASHES = frozenset({
    "fa669514729ae29315c5b40a29587cc48e0636bf88b2e1e466ad21f3cfa0398a",  # brain home under JARVIS_DATA_DIR
    "05e770673312b589529b570f63ec49167eb901d0363642040bef11411ed5ae43",  # announce what needs you now
    "4238420bdd91569b1c8598a5af81a0f9550cee52a5b166267c2ec9d6f645a9dd",  # remember, recall, project_note
    "4f924161c1fc104c339d027bc58cddf19396d520c0f29891937da00d23f60c59",  # answer_dialog
    "f66fb4c85550925effb8b663ca525cf47e1be543439711c187749c73ca2992d6",  # create a project, check on work
    "fd016ad6e8a16e8942d51247dd44db717e5a357b7c8167f9ce3c73cf98ddb9e1",  # spawned runs finish the work
    "c7e908b35d3166fc84c17d27d5b565e54a8b144a458f866ce530c48b94a3d51b",  # read a repo, not just watch sessions
    "f7a1a8a1edade7c50e28d0cbd319233f8b9bb76076f95691ac3f3fb12e9c4b34",  # real builds
    "7639e2a9b5e387ec3975980df2641bfb4ee75e703155be308edca335405e272a",  # "Sonnet" comes through as "Sonic"
    "567d76449e621136e1682ec8627c85195f6b75abc103066f33267cf251fab606",  # "Look it up" has an answer
    "66ea84adef02de313e6f1e1696d5998b1fc26e24e3bce912bf1d2d2195c37a44",  # the repository question
    "87e4c3dda601a952a81cb76ff71805aa846c4155f63e03897c34dc79a39c319f",  # "can you see my screen"
    "94971b048c2911ad7f4505fc943fed8d0b640d7e726bcb00000b28ee549ff97c",  # connected services
    "b47aabe098727e47c7cd8eef07b371693ab71cd010a19987fd778dba9dbab339",  # fictional project names in the examples
    "2cec270d83fe01e504ea9111f20ff14a7003d7aa124daa82852d5351206a8542",  # fictional repo names too
    "67f15193bae6d048b148a8439a32d4667048fdf73224c5ea8439a96b7de59944",  # how he differs from the public repo
    "bdf6109c0c6328830c6c1a2c78617b64b7c0df53da78a343589b03f715e040a3",  # "what updates" is not a changelog request
    "8b038b5497293a55d1aa18e9ce759d3270228c98ae8b3ceb3bb053eb84509310",  # anything read off this machine is information
    "dfec0e28f7fc734987a1bcffd4feda103bde961f4a4721ce7957a7dccae96487",  # send it, do not ask twice
    "092df6a5e43bc5ed0a31e9f79b1f77cc4849754d1f4ab4807bded122ab97ad5f",  # say "start fresh" when a memory is refused
})

# The same list, for the connections file. APPEND the new hash whenever
# jarvis_home/connections.json changes —
# `test_every_connections_template_this_project_has_shipped_is_listed` walks
# the file's git history and fails with the exact line to paste.
KNOWN_CONNECTIONS_HASHES = frozenset({
    "8c27da80e7ea11fff914e9212823ff72294ad1ac7a5a2daf9e40c2bc095fa979",  # the doorway
})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recorded_seed_hash(seed: Path) -> Optional[str]:
    """The hash of what JARVIS last wrote, or None if we cannot read one.

    Anything unreadable, corrupt or the wrong shape is None — "we do not
    know". Never a guess: the one thing this value must never do is claim a
    match that would send an edited file to the overwriter.
    """
    try:
        body = json.loads(seed.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning(f"data_paths: unreadable persona seed record ignored ({e})")
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("sha256")
    return value if isinstance(value, str) and value else None


def _write_atomically(path: Path, text: str) -> bool:
    """Replace `path` in one step, or leave it exactly as it was.

    A half-written CLAUDE.md is a broken persona on the next launch, which is
    a worse outcome than a stale one.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        log.warning(f"data_paths: could not write {path} ({e})")
        return False
    return True


def _record_seed(seed: Path, template: Path, digest: str) -> None:
    _write_atomically(seed, json.dumps({
        "sha256": digest,
        "written_at": time.time(),
        "template": str(template),
        "note": (f"The sha256 of the {template.name} JARVIS wrote next to this "
                 f"file. While they match, JARVIS keeps that file up to date "
                 f"with the one it ships. Edit {template.name} and they stop "
                 f"matching, and JARVIS never touches it again."),
    }, indent=2) + "\n")


# The last live copy of each file we warned about, so a hands-off file does not
# log the same warning on every memory write (ensure_memory_layout runs on all
# of them). Deliberately not a "did we sync yet" flag: the decision itself runs
# every time, so nothing can pass a test by being skipped.
_warned_for: dict[str, str] = {}


def _sync_template(template: Path, target: Path, seed: Path,
                   known_hashes: frozenset, what: str) -> str:
    """Bring `target` up to `template` — unless it is the user's. Returns
    "seeded", "updated", "current" or "kept".

    ONE function for every file this project ships and the user may then edit
    (the persona, the connections file), because the destructive half is the
    same in each and a second copy is a second chance to get it wrong.

    Two halves, and both matter:

    * An UNEDITED file must update. A brain home seeded once and never
      touched again would keep its first prompt forever, so every behaviour
      fix shipped afterwards would be inert on that install — the bug this
      replaces.
    * An EDITED file must never be silently overwritten. We know an edit by
      the file no longer matching what we wrote (the seed record), and we say
      so in the log, naming both paths, so the user can merge by hand.

    Where the evidence does not settle it, the conservative branch wins:
    "kept" costs an upgrade, an overwrite costs the user's work.
    """
    home = brain_home()
    home.mkdir(parents=True, exist_ok=True)
    key = str(target)

    try:
        shipped = template.read_text()
    except OSError as e:                                # pragma: no cover
        log.warning(f"data_paths: cannot read the {what} template ({e})")
        return "kept"
    shipped_hash = _sha256(shipped.encode("utf-8"))

    try:
        live = target.read_bytes()
    except FileNotFoundError:
        if _write_atomically(target, shipped):
            _record_seed(seed, template, shipped_hash)
        return "seeded"
    except OSError as e:                                # pragma: no cover
        log.warning(f"data_paths: cannot read {target} ({e})")
        return "kept"

    live_hash = _sha256(live)
    if live_hash == shipped_hash:
        # Byte-identical to what we ship, so it is unmodified whatever the
        # record says. Record it (an install upgrading into this code has no
        # record yet) and touch nothing else.
        if _recorded_seed_hash(seed) != shipped_hash:
            _record_seed(seed, template, shipped_hash)
        _warned_for.pop(key, None)
        return "current"

    recorded = _recorded_seed_hash(seed)
    if recorded is not None:
        unmodified = live_hash == recorded
        why = "it still matches what JARVIS wrote"
    else:
        # First run after this shipped: no record exists, so the bytes are the
        # only evidence. A version this project once shipped is provably
        # untouched; anything else we treat as the user's.
        unmodified = live_hash in known_hashes
        why = "it is an older template of ours, unedited"

    if unmodified:
        if _write_atomically(target, shipped):
            _record_seed(seed, template, shipped_hash)
            log.info(f"data_paths: updated {target} to the {what} shipped with "
                     f"this version ({why})")
            _warned_for.pop(key, None)
            return "updated"
        return "kept"                                   # pragma: no cover

    if _warned_for.get(key) != live_hash:
        _warned_for[key] = live_hash
        log.warning(
            f"data_paths: {target} has been edited, so the {what} shipped "
            f"with this version was NOT applied. The new one is at "
            f"{template} — merge what you want from it by hand, or delete "
            f"your copy to take it whole.")
    return "kept"


def sync_persona() -> str:
    """Keep the brain's CLAUDE.md in step with the one this release ships.
    See `_sync_template` for the rule."""
    return _sync_template(persona_template_path(), persona_path(),
                          persona_seed_path(), KNOWN_TEMPLATE_HASHES, "persona")


def sync_connections() -> str:
    """Seed (and keep current) the file a user declares their MCP servers in.

    The moment they put a server in it the file becomes theirs and is never
    written again — which is the whole promise: their configuration survives
    every upgrade, because upgrading is exactly when a "helpful" rewrite would
    disconnect them.
    """
    return _sync_template(connections_template_path(), connections_path(),
                          connections_seed_path(), KNOWN_CONNECTIONS_HASHES,
                          "connections file")


def ensure_brain_home() -> Path:
    """Create the brain home and keep the two files it ships in step with
    their templates: the persona, and the connections file.

    See `_sync_template`: an unedited copy is updated to what this release
    ships, an edited one is left alone and warned about.
    """
    sync_persona()
    sync_connections()
    return brain_home()


def memory_dir() -> Path:
    return brain_home() / "memory"


def projects_dir() -> Path:
    return brain_home() / "projects"


def journal_dir() -> Path:
    return brain_home() / "journal"


def ensure_memory_layout() -> Path:
    """Create the brain's memory folder. Plain Markdown, user-editable.

    Seeds (and updates) the persona via ensure_brain_home() and never
    overwrites anything the user has written.
    """
    home = ensure_brain_home()
    for d in (memory_dir(), projects_dir(), journal_dir()):
        d.mkdir(parents=True, exist_ok=True)
    return home


def usage_path() -> Path:
    """The last rate-limit observation from the CLI (see usage_store.py)."""
    return data_dir() / "usage.json"


def tool_token_path() -> Path:
    """The bearer token the MCP child uses to reach /internal/tool."""
    return brain_home() / "tool-token"


def ensure_tool_token() -> str:
    """Create the loopback tool token if absent and return it.

    The token is what admits a caller to JARVIS's acting tools and to every
    state-changing HTTP route (see web_auth), so it is created with O_EXCL at
    mode 0600 directly — never briefly world-readable at umask permissions
    between write and chmod.

    A pre-existing file is still adopted, because it has to be across
    restarts, but it is adopted through ONE file descriptor: opened
    O_NOFOLLOW, checked with fstat, chmodded with fchmod and read with that
    same fd. The old version did `path.chmod(); path.read_text()`, two
    lookups of a name an attacker could change in between — and both of them
    followed symlinks, so a link planted at this path meant any file the user
    owns could be forced to 0600, and the token JARVIS then trusted was one
    somebody else wrote.

    A path that is not a regular file this user owns raises, rather than
    being quietly replaced: it is somebody else's file, and deleting it is
    not ours to do.
    """
    import secrets
    import stat as _stat
    path = tool_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        return token

    if sys.platform == "win32":
        return _adopt_tool_token_windows(path, token)

    fd = os.open(str(path), os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if info.st_uid != os.getuid():
            raise OSError(f"{path} is owned by uid {info.st_uid}, not by us")
        os.fchmod(fd, 0o600)
        existing = os.read(fd, 4096).decode("utf-8", "ignore").strip()
        if existing:
            return existing
        # An empty file: ours to fill, and only ours — the fd is already
        # proven to be a regular file we own.
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, token.encode("utf-8"))
        return token
    finally:
        os.close(fd)


def _adopt_tool_token_windows(path: Path, token: str) -> str:
    """Windows has no O_NOFOLLOW, getuid or POSIX modes, so the fd-based
    checks above cannot run. Refuse links and non-regular files via lstat;
    access control is left to the user profile's ACLs, which already keep
    the data directory private to this account."""
    import stat as _stat
    info = os.lstat(path)
    if _stat.S_ISLNK(info.st_mode) or not _stat.S_ISREG(info.st_mode):
        raise OSError(f"{path} is not a regular file")
    fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        existing = os.read(fd, 4096).decode("utf-8", "ignore").strip()
        if existing:
            return existing
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, token.encode("utf-8"))
        return token
    finally:
        os.close(fd)
