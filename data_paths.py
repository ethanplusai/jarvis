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
_LOCAL_NAME = "LOCAL.md"


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


def local_persona_path() -> Path:
    """The user's own standing orders, `@`-imported by the shipped CLAUDE.md.

    The persona could not hold a user's additions AND stay upgradable: one
    appended paragraph made its hash stop matching the seed record, and
    from then on every persona change shipped was inert on that install,
    announced by a log line nobody reads. So the user's words get a file of
    their own, beside the persona, that this project seeds once and never
    writes again — and CLAUDE.md can stay ours to replace.
    """
    return brain_home() / _LOCAL_NAME


LOCAL_PERSONA_SEED = (
    "# Your standing orders for JARVIS\n"
    "\n"
    "Anything you write in this file is read into every conversation, exactly\n"
    "like `CLAUDE.md` beside it — and it outranks that file where the two\n"
    "differ. Put your own instructions here, not in `CLAUDE.md`: that one is\n"
    "JARVIS's and is replaced whenever a new version ships, while this file is\n"
    "yours and is never written by JARVIS after this line.\n")


def ensure_local_persona() -> Path:
    """Seed `LOCAL.md` once. Never overwrites: the file is the user's."""
    path = local_persona_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(path, LOCAL_PERSONA_SEED)
    return path


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
    "4057ca69eb8c7535f7af394087d8f0276248f0ec0ef806136b8b3699a6738b94",  # standing orders in LOCAL.md; project_history
    "3f0a449e7d383cba52c63a4541277ecca856d46f2855925a5abae77e5c246076",  # the test count stopped being a number that goes stale
    "f95602951b26d79c0ca7ae7e96d0b815410c8f943b1e410d7f13eb7bb0f1543f",  # the handover is a reply, not a write_journal call
    "03e783a4c1ebc26657bfd9913c63000ad0771d2cf0c2b9cf2bb1befa3c21469c",  # change a record by finding it, not reading it
    "69468d50a48468a6deb9c3dedb78eeb7c1a65e086a4c3cd412ac3971e1c76558",  # approve a document by its kind, not its path
    "f42f9e2034bc6416d4f5751f8e70d4e64723cd94567e3757b9bbc8a5b82a9c64",  # the user on WhatsApp
    "3c0455238cb950346b709ca4d739d42a41ede9206be6ed3be972fba6dd6c4cb5",  # the user on his phone: Telegram too
    "526a8ed9297aeec0918ac855b08e4992317d92109aa50d3dd0079ad0e12f6f80",  # say where a prompt is; a program's is the program's
    "478280e5b37766da6664cde56018b38cdee99783e5d0e0655b62cf72ef0b0d62",  # one card per post, its link on the card
    "024ed292c779aad8998ff8a1d35dddc97655dbe7a156e3ef88386a71393bfe74",  # LinkedIn's official API first; its limits
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
        body = json.loads(seed.read_text(encoding="utf-8"))
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
            # Exactly the bytes that were hashed: UTF-8, LF, whatever the
            # platform's defaults are. Windows would otherwise write CRLF
            # and cp1252, and the file would read as "edited" at the next
            # boot without anyone touching it.
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
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
    where = (f" Your own instructions belong in {_LOCAL_NAME} beside it, which "
             f"JARVIS never writes." if template.name == _PERSONA_NAME else "")
    _write_atomically(seed, json.dumps({
        "sha256": digest,
        "written_at": time.time(),
        "template": str(template),
        "note": (f"The sha256 of the {template.name} JARVIS wrote next to this "
                 f"file. While they match, JARVIS keeps that file up to date "
                 f"with the one it ships. Edit {template.name} and they stop "
                 f"matching, and JARVIS never touches it again.{where}"),
    }, indent=2) + "\n")


# The last live copy of each file we warned about, so a hands-off file does not
# log the same warning on every memory write (ensure_memory_layout runs on all
# of them). Deliberately not a "did we sync yet" flag: the decision itself runs
# every time, so nothing can pass a test by being skipped.
_warned_for: dict[str, str] = {}


def _template_status(template: Path, target: Path, seed: Path,
                     known_hashes: frozenset) -> tuple[str, str, str]:
    """(status, shipped text, shipped hash) — a pure read, nothing written.

    status is "missing" (no live file), "current" (byte-identical to what we
    ship, CRLF aside), "unedited" (an older copy still matching what JARVIS
    wrote, so ours to update) or "edited" (the user's, never touched).
    """
    shipped = template.read_text(encoding="utf-8")
    shipped_hash = _sha256(shipped.encode("utf-8"))
    try:
        # A CRLF checkout (git autocrlf on Windows) or an editor that
        # rewrote line endings is not an edit; the words are the same.
        live = target.read_bytes().replace(b"\r\n", b"\n")
    except FileNotFoundError:
        return "missing", shipped, shipped_hash
    live_hash = _sha256(live)
    if live_hash == shipped_hash:
        return "current", shipped, shipped_hash
    recorded = _recorded_seed_hash(seed)
    if recorded is not None:
        unmodified = live_hash == recorded
    else:
        # First run after this shipped: no record exists, so the bytes are
        # the only evidence. A version this project once shipped is provably
        # untouched; anything else we treat as the user's.
        unmodified = live_hash in known_hashes
    return ("unedited" if unmodified else "edited"), shipped, shipped_hash


def persona_status() -> str:
    """Where the brain's CLAUDE.md stands against the one this release ships:
    "missing", "current", "unedited" or "edited". Reads only — the startup
    checks ask this, and a check that seeds a file is not a check."""
    try:
        status, _shipped, _digest = _template_status(
            persona_template_path(), persona_path(), persona_seed_path(),
            KNOWN_TEMPLATE_HASHES)
    except OSError:                                     # pragma: no cover
        return "missing"
    return status


def _sync_template(template: Path, target: Path, seed: Path,
                   known_hashes: frozenset, what: str, hint: str = "") -> str:
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
        status, shipped, shipped_hash = _template_status(
            template, target, seed, known_hashes)
    except OSError as e:                                # pragma: no cover
        log.warning(f"data_paths: cannot read the {what} ({e})")
        return "kept"

    if status == "missing":
        if _write_atomically(target, shipped):
            _record_seed(seed, template, shipped_hash)
        return "seeded"

    if status == "current":
        # Byte-identical to what we ship, so it is unmodified whatever the
        # record says. Record it (an install upgrading into this code has no
        # record yet) and touch nothing else.
        if _recorded_seed_hash(seed) != shipped_hash:
            _record_seed(seed, template, shipped_hash)
        _warned_for.pop(key, None)
        return "current"

    if status == "unedited":
        why = ("it still matches what JARVIS wrote"
               if _recorded_seed_hash(seed) is not None
               else "it is an older template of ours, unedited")
        if _write_atomically(target, shipped):
            _record_seed(seed, template, shipped_hash)
            log.info(f"data_paths: updated {target} to the {what} shipped with "
                     f"this version ({why})")
            _warned_for.pop(key, None)
            return "updated"
        return "kept"                                   # pragma: no cover

    try:
        live_hash = _sha256(target.read_bytes().replace(b"\r\n", b"\n"))
    except OSError:                                     # pragma: no cover
        live_hash = ""
    if _warned_for.get(key) != live_hash:
        _warned_for[key] = live_hash
        log.warning(
            f"data_paths: {target} has been edited, so the {what} shipped "
            f"with this version was NOT applied. The new one is at "
            f"{template} — merge what you want from it by hand, or delete "
            f"your copy to take it whole.{hint}")
    return "kept"


def sync_persona() -> str:
    """Keep the brain's CLAUDE.md in step with the one this release ships.
    See `_sync_template` for the rule."""
    return _sync_template(
        persona_template_path(), persona_path(), persona_seed_path(),
        KNOWN_TEMPLATE_HASHES, "persona",
        hint=(f" Your own additions belong in {local_persona_path()}, which is "
              f"read into every conversation too and which JARVIS never "
              f"writes — move them there and delete {_PERSONA_NAME} to take "
              f"the new one."))


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
    their templates: the persona, and the connections file. Seed the third,
    `LOCAL.md`, once and never again — that one is the user's.

    See `_sync_template`: an unedited copy is updated to what this release
    ships, an edited one is left alone and warned about.
    """
    sync_persona()
    sync_connections()
    ensure_local_persona()
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


def usage_log_path() -> Path:
    """One line per call the voice path made (see server._append_usage_entry)."""
    return data_dir() / "usage_log.jsonl"


def restrict_to_owner(path) -> bool:
    """Make `path` the owner's alone — and on Windows, mean it.

    Everything private here was protected by a POSIX mode: the token at 0600,
    the archive chmodded after it was written. On Windows a mode is inert and
    a file inherits its folder's ACL; measured live, that granted
    `NT AUTHORITY\\Authenticated Users` Modify on the tool token and the
    database. So on Windows this is the DACL, written whole (`windows_acl`):
    inheritance off, and only this account, SYSTEM and Administrators — by
    SID, not by name, so it holds in any language.

    A directory is restricted with its whole subtree, but never object by
    object. The first version ran `icacls /T`, which hands the directory
    grant `(OI)(CI)F` to every file as well, and icacls silently drops a
    grant with inheritance flags on a file — measured 2026-09-24: every
    file was left with an empty DACL that admitted nobody, the lockout
    probe below fired, and the whole restriction rolled back on every
    start. Restricting the directory alone is enough for everything under
    it that inherits: Windows propagates the new entries down. The few
    objects that do not inherit — a token or an archive restricted earlier,
    something restored from elsewhere — are found and restricted one at a
    time, each with the entries for its kind.

    Returns True when the restriction was applied, False when the path is
    missing or the tool refused; never raises — a permission problem must
    not stop the server starting, and `preflight` reports it instead.
    """
    path = Path(path)
    if not path.exists():
        return False
    if sys.platform == "win32":
        return _restrict_windows(path)
    try:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return True
    except OSError as e:
        log.warning(f"data_paths: could not chmod {path}: {e}")
        return False


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_OWNER_RIGHTS_SID = "S-1-3-4"


def _restrict_windows(path: Path) -> bool:
    sid = _current_sid()
    if not sid:
        return False
    applied: list[Path] = []
    if not _restrict_one(path, sid):
        return False
    applied.append(path)
    if path.is_dir():
        try:
            strays = foreign_entries(path)
        except OSError as e:
            log.warning(f"data_paths: could not read the permissions under {path}: {e}")
            strays = []
        for stray, who in strays:
            # Top-down: once a protected directory is restricted, what
            # inherits from it is covered, and is left inheriting.
            if not _strangers_on(stray, sid):
                continue
            log.info(f"data_paths: {stray} did not inherit and admitted {', '.join(who)}; restricting it")
            if _restrict_one(stray, sid):
                applied.append(stray)
    # Prove the account running THIS process can still get in. A token that
    # genuinely cannot hold access to a file that admits only its account
    # (a restricted token, whose restricting SIDs are exactly the broad
    # entries just removed) would be locked out of JARVIS's own data, which
    # is worse than no restriction — so what this call did is undone, and
    # preflight says so.
    if not _still_accessible(path):
        for done in reversed(applied):
            _reset_one(done)
        log.warning(f"data_paths: restricting {path} locked this account out; rolled back")
        return False
    return True


def _restrict_one(path: Path, sid: str) -> bool:
    """Inheritance off and exactly three entries: this account, SYSTEM,
    Administrators — the DACL written whole, so an entry somebody else was
    given explicitly does not survive, not even one for an account the
    machine can no longer name (which icacls could neither grant nor
    remove)."""
    import windows_acl
    try:
        windows_acl.restrict(path, [sid, _SYSTEM_SID, _ADMINISTRATORS_SID])
    except OSError as e:
        log.warning(f"data_paths: could not restrict {path}: {e}")
        return False
    return True


def _reset_one(path: Path) -> None:
    """Back to inheriting from the folder above. Not recursive: resetting a
    directory reaches what inherits from it on its own."""
    import windows_acl
    try:
        windows_acl.reset(path)
    except OSError as e:
        log.warning(f"data_paths: could not reset {path}: {e}")


def foreign_entries(root) -> list[tuple[Path, list[str]]]:
    """Every object at or under `root` whose ACL admits somebody other than
    this account, SYSTEM and Administrators — with who, by name where the
    machine can resolve the SID and by SID where it cannot. `OWNER RIGHTS`
    counts as the object's owner. Windows only; raises OSError when a
    security descriptor cannot be read."""
    import windows_acl
    root = Path(root)
    sid = _current_sid()
    found: list[tuple[Path, list[str]]] = []
    for path in _walk(root):
        strangers = _strangers_on(path, sid)
        if strangers:
            found.append((path, [windows_acl.principal_name(s) for s in strangers]))
    return found


def _strangers_on(path: Path, sid: str | None) -> list[str]:
    """The SIDs `path` admits that are neither this account, SYSTEM nor
    Administrators. `OWNER RIGHTS` stands for the object's owner and is
    fine when the owner is one of those three."""
    import windows_acl
    allowed = {sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    owner, admitted = windows_acl.security(path)
    return [s for s in admitted
            if s not in allowed and not (s == _OWNER_RIGHTS_SID and owner in allowed)]


def _walk(root: Path):
    yield root
    if root.is_dir():
        for base, dirs, files in os.walk(root):
            for name in dirs + files:
                yield Path(base) / name


def _still_accessible(path: Path) -> bool:
    """Can this process still read `path` (for a directory: the first file
    anywhere under it) after an ACL change? Opens something rather than
    trusting `os.access`, which does not evaluate Windows ACLs."""
    try:
        if path.is_dir():
            for child in _walk(path):
                if child.is_file():
                    with open(child, "rb"):
                        pass
                    break
            return True
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _current_sid() -> str | None:
    """The SID of the account this process runs as, read from its token."""
    try:
        import windows_acl
        return windows_acl.current_user_sid()
    except OSError as e:
        log.warning(f"data_paths: could not read this process's SID: {e}")
        return None


def harden_private_root() -> bool:
    """Restrict the data directory (and so the database, the memory folder,
    the archives) and the tool token to this account. Called once at server
    start, before the preflight check that would otherwise report it."""
    ok = restrict_to_owner(data_dir())
    token = tool_token_path()
    if token.exists():
        ok = restrict_to_owner(token) and ok
    return ok


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
        # 0600 at creation on POSIX; on Windows that mode is inert, so the
        # ACL is restricted to this account as well.
        restrict_to_owner(path)
        return token

    # Windows has no O_NOFOLLOW, no fchmod and no uid to compare. The
    # symlink refusal is done with lstat there instead — two lookups, but a
    # planted link still makes this raise rather than be followed — and the
    # ownership and mode checks are POSIX-only.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow and path.is_symlink():
        raise OSError(f"{path} is a symlink")
    fd = os.open(str(path), os.O_RDWR | nofollow)
    try:
        info = os.fstat(fd)
        if not _stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError(f"{path} is owned by uid {info.st_uid}, not by us")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        # And on Windows, where that mode is inert: the ACL, to this account.
        restrict_to_owner(path)
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
