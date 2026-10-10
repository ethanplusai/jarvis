"""Verified data archives, offline restore, export, opt-in retention, and the
one memory repair: `reindex`.

Usage: python maintenance.py {backup,verify,restore,export,prune,reindex} --help
Archives contain private memory and configuration; keep them private. They
do NOT contain `jarvis/tool-token` — the bearer token that admits a caller to
the memory writers and every mutating route has no business in the same
unencrypted zip as the memory it writes; a restored install mints a new one
on its next start.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile

import conversation_store
import data_paths
import jarvis_memory
import procs
import run_store

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_EXCLUDED = {"backups", "runtime.pid", "restore-staging"}
# Files (by archive name) that are never written INTO an archive and never
# written OUT of one. `_safe_name` still admits them, so an archive made
# before this rule still verifies; `_restore` just does not extract them.
_EXCLUDED_FILES = frozenset({"jarvis/tool-token"})
_runtime_lock = None


def _acquire_lock():
    """Keep the lock outside the data tree so restore can rename that tree."""
    root = data_paths.data_dir().resolve()
    path = root.with_name(root.name + ".runtime.lock")
    handle = path.open(mode="a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except BaseException:
        handle.close()
        raise RuntimeError("Another JARVIS instance or restore owns this data directory") from None


def _release_lock(handle):
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def register_runtime():
    global _runtime_lock
    if _runtime_lock is not None:
        return
    handle = _acquire_lock()
    try:
        _register_runtime()
        _runtime_lock = handle
    except BaseException:
        _release_lock(handle)
        raise


def _register_runtime():
    path = data_paths.data_dir() / "runtime.pid"
    if path.exists():
        try:
            pid = int(path.read_text(encoding="utf-8"))
            if pid != os.getpid() and procs.pid_alive(pid):
                raise RuntimeError("Another JARVIS instance owns this data directory")
        except ValueError:
            pass
    path.write_text(str(os.getpid()), encoding="utf-8")


def unregister_runtime():
    global _runtime_lock
    path = data_paths.data_dir() / "runtime.pid"
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    finally:
        _release_lock(_runtime_lock)
        _runtime_lock = None


def require_offline(doing: str = "restoring its data"):
    path = data_paths.data_dir() / "runtime.pid"
    if path.exists():
        try:
            if procs.pid_alive(int(path.read_text(encoding="utf-8"))):
                raise ValueError(f"Stop JARVIS before {doing}")
        except (TypeError, ValueError) as error:
            if "Stop JARVIS" in str(error):
                raise
            raise ValueError("Cannot verify that JARVIS is stopped") from error


def _safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or ":" in name or ".." in path.parts:
        raise ValueError("Unsafe archive path")
    if any(part.endswith((".", " ")) or part.upper().split(".")[0] in
           {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))} for part in path.parts):
        raise ValueError("Unsafe Windows archive path")
    if path.parts[0] in _EXCLUDED:
        raise ValueError("Runtime files cannot be restored")
    return path


def backup(destination: Path) -> dict:
    root = data_paths.data_dir().resolve()
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("Backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"version": 1, "created_at": time.time(), "files": {}}
    temporary = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-backup-") as staging, \
                zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(root.rglob("*")):
                relative = source.relative_to(root)
                if relative.parts[0] in _EXCLUDED or source in (destination, temporary):
                    continue
                if source.is_symlink():
                    raise ValueError("Backup refuses symbolic links")
                if not source.is_file() or source.name.endswith(("-wal", "-shm")):
                    continue
                if not source.resolve().is_relative_to(root):
                    raise ValueError("Backup path escaped data directory")
                name = relative.as_posix()
                if name in _EXCLUDED_FILES:
                    continue
                _safe_name(name)
                if source.suffix == ".db":
                    snapshot = Path(staging) / uuid.uuid4().hex
                    with closing(sqlite3.connect(str(source))) as live, \
                            closing(sqlite3.connect(str(snapshot))) as target:
                        live.backup(target)
                    content = snapshot.read_bytes()
                else:
                    content = source.read_bytes()
                manifest["files"][name] = hashlib.sha256(content).hexdigest()
                archive.writestr(name, content)
            archive.writestr("manifest.json", json.dumps(manifest))
        verify(temporary)
        temporary.replace(destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        # The archive holds the memory, the database and the connections
        # file: on Windows a mode is inert, so the ACL is restricted too.
        data_paths.restrict_to_owner(destination)
        return {"name": destination.name, "files": len(manifest["files"]),
                "bytes": destination.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)


def verify(archive_path: Path) -> dict:
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive exceeds the size limit")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive expands beyond the size limit")
        names = [item.filename for item in entries]
        if len(names) != len(set(n.casefold() for n in names)):
            raise ValueError("Duplicate archive paths")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Unsupported backup manifest")
        if set(names) != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("Archive differs from its manifest")
        for name, digest in manifest["files"].items():
            _safe_name(name)
            info = archive.getinfo(name)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive contains a symbolic link")
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError(f"Checksum mismatch: {name}")
        if "jarvis.db" not in manifest["files"]:
            raise ValueError("Backup does not contain the JARVIS database")
        return manifest


def restore(archive_path: Path) -> dict:
    require_offline()
    handle = _acquire_lock()
    try:
        return _restore(archive_path)
    finally:
        _release_lock(handle)


def _restore(archive_path: Path) -> dict:
    require_offline()
    manifest = verify(archive_path)
    root = data_paths.data_dir().resolve()
    stage = Path(tempfile.mkdtemp(prefix=".jarvis-restore-", dir=root.parent))
    previous = root.with_name(root.name + ".before-restore-" + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for name in manifest["files"]:
                if name in _EXCLUDED_FILES:
                    continue
                target = stage.joinpath(*_safe_name(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        for database in stage.rglob("*.db"):
            with closing(sqlite3.connect(str(database))) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restored database failed integrity verification")
                if database.name == "jarvis.db":
                    conn.execute("SELECT id, status FROM runs LIMIT 1")
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_actions'").fetchone():
                        # Restoring old pending approvals must not resurrect an
                        # action that may have executed after that backup.
                        # 'approved' is where a connector action rests ARMED
                        # until the PreToolUse gate spends it; restored, it
                        # would authorise a second send of a post that may
                        # already have gone out. Business actions never rest
                        # there (pending -> executing), so this is theirs alone.
                        unsettled = "state IN ('pending','approved','executing')"
                        conn.execute("INSERT INTO business_audit(action_id,event,at) "
                                     f"SELECT id,'restore_invalidated',? FROM business_actions WHERE {unsettled}",
                                     (time.time(),))
                        conn.execute("UPDATE business_actions SET state='unknown',updated=?,result=? "
                                     f"WHERE {unsettled}",
                                     (time.time(), json.dumps({"message": "Restored from backup. Check provider records; previous approvals are invalid."})))
                        conn.commit()
        require_offline()
        root.rename(previous)
        try:
            stage.rename(root)
        except BaseException:
            previous.rename(root)
            raise
        rollback = root / "backups" / previous.name
        try:
            rollback.parent.mkdir(parents=True, exist_ok=True)
            previous.rename(rollback)
        except BaseException:
            root.rename(stage)
            previous.rename(root)
            raise
        return {"restored": True, "previous_data": str(rollback), "files": len(manifest["files"])}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def export_runs(destination: Path):
    with closing(run_store._connect()) as conn, destination.open("x", encoding="utf-8") as output:
        for row in conn.execute("SELECT * FROM runs ORDER BY created_at,id"):
            output.write(json.dumps({"type": "run", **dict(row)}, ensure_ascii=False) + "\n")
        for row in conn.execute("SELECT * FROM run_events ORDER BY run_id,seq"):
            output.write(json.dumps({"type": "event", **dict(row)}, ensure_ascii=False) + "\n")


# The journal keeps this many newest entries whatever their age: the boot
# handover reads the newest one, and a quiet fortnight must not leave the
# brain with no note at all.
JOURNAL_KEEP_MIN = 5


def prune(days: int, apply=False, transcripts=False):
    """One retention policy for everything JARVIS accumulates, one cutoff.

    A report by default; `apply` deletes. Finished runs (never a resume
    parent), conversation rows, journal entries (all but the newest
    JOURNAL_KEEP_MIN), lines of the usage log — and, only with
    `transcripts=True`, the brain's own Claude Code transcripts: they are
    the CLI's files, one per rotation, and only the brain's own directory
    under each config root is ever touched.
    """
    if days < 1:
        raise ValueError("Retention must be at least one day")
    cutoff = time.time() - days * 86400
    return {
        "days": days,
        "runs": _prune_runs(cutoff, apply),
        "conversation": _prune_conversation(cutoff, apply),
        "journal": _prune_journal(cutoff, apply),
        "usage_log": _prune_usage_log(cutoff, apply),
        "transcripts": _prune_transcripts(cutoff, apply, transcripts),
    }


def _prune_runs(cutoff: float, apply: bool) -> dict:
    run_store.init_db()
    with closing(run_store._connect()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # A resumed run retains its complete ancestry, even if terminal.
        rows = conn.execute("""SELECT id FROM runs WHERE status IN ('succeeded','failed','timed_out','cancelled')
            AND created_at < ? AND id NOT IN (SELECT resume_from FROM runs WHERE resume_from IS NOT NULL)""",
            (cutoff,)).fetchall()
        ids = [row[0] for row in rows]
        if apply:
            for run_id in ids:
                conn.execute("DELETE FROM run_events WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        return {"eligible": len(ids), "deleted": len(ids) if apply else 0}


def _prune_conversation(cutoff: float, apply: bool) -> dict:
    conversation_store.init_db()
    with closing(conversation_store.connect()) as conn, conn:
        eligible = conn.execute("SELECT COUNT(*) FROM conversation WHERE created_at < ?",
                                (cutoff,)).fetchone()[0]
        deleted = 0
        if apply and eligible:
            deleted = conn.execute("DELETE FROM conversation WHERE created_at < ?", (cutoff,)).rowcount
        return {"eligible": eligible, "deleted": deleted}


def _prune_journal(cutoff: float, apply: bool) -> dict:
    # Judged by the stamp in the filename, never mtime: correcting a typo in
    # an old entry must not make it young (the same rule the dashboard keeps).
    entries = jarvis_memory.journal_entries()               # oldest first
    candidates = entries[:-JOURNAL_KEEP_MIN] if len(entries) > JOURNAL_KEEP_MIN else []
    eligible = [path for stamp, _reason, path in candidates
                if jarvis_memory._stamp_to_epoch(stamp) < cutoff]
    deleted = 0
    if apply:
        for path in eligible:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                pass
    return {"eligible": len(eligible), "deleted": deleted}


def _prune_usage_log(cutoff: float, apply: bool) -> dict:
    path = data_paths.usage_log_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"eligible": 0, "deleted": 0}
    keep, eligible = [], 0
    for line in lines:
        try:
            old = float(json.loads(line)["ts"]) < cutoff
        except (ValueError, KeyError, TypeError):
            old = False                     # a line we cannot read is not ours to drop
        if old:
            eligible += 1
        else:
            keep.append(line)
    if apply and eligible:
        temporary = path.with_name(path.name + ".pruning")
        temporary.write_text("".join(f"{l}\n" for l in keep), encoding="utf-8")
        temporary.replace(path)
    return {"eligible": eligible, "deleted": eligible if apply else 0}


def _prune_transcripts(cutoff: float, apply: bool, opted_in: bool) -> dict:
    if not opted_in:
        return {"eligible": 0, "deleted": 0, "opted_in": False}
    import session_watch
    encoded = session_watch.encode_cwd(session_watch.brain_cwd())
    eligible, deleted = 0, 0
    for root in session_watch.config_roots():
        own = Path(root) / "projects" / encoded          # the brain's, and only the brain's
        try:
            files = sorted(own.glob("*.jsonl"))
        except OSError:
            continue
        for transcript in files:
            try:
                if transcript.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            eligible += 1
            if apply:
                try:
                    transcript.unlink()
                    shutil.rmtree(own / transcript.stem, ignore_errors=True)   # its subagents
                    deleted += 1
                except OSError:
                    pass
    return {"eligible": eligible, "deleted": deleted, "opted_in": True}


def reindex() -> dict:
    """Give every note in `memory/` that MEMORY.md does not name a line.

    Offline, like `restore`: `add_to_index` is a read-modify-write of a file
    the running server also writes, and two writers can lose a line. With
    JARVIS running, the dashboard's Memory tab does the same thing
    in-process.
    """
    require_offline("repairing its memory index")
    return jarvis_memory.reindex()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("backup", "verify", "restore", "export"):
        commands.add_parser(command).add_argument("path", type=Path)
    retention = commands.add_parser("prune", help="report (or with --apply, delete) finished "
                                    "runs, conversation rows, journal entries and usage-log lines "
                                    "older than --days")
    retention.add_argument("--days", type=int, required=True)
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--transcripts", action="store_true",
                           help="also the brain's own Claude Code transcripts older than --days")
    commands.add_parser("reindex", help="add an index line for every memory "
                        "file MEMORY.md does not name (JARVIS stopped)")
    args = parser.parse_args()
    if args.command == "prune":
        result = prune(args.days, args.apply, args.transcripts)
    elif args.command == "reindex":
        result = reindex()
    else:
        result = {"backup": backup, "verify": verify, "restore": restore,
                  "export": export_runs}[args.command](args.path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
