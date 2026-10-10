import hashlib
import json
from pathlib import Path
import zipfile

import pytest

import data_paths
import maintenance
import run_store


def test_backup_verify_restore_preserves_database_and_memory(tmp_path):
    run_store.init_db()
    run_id = run_store.create_run("original", "p", str(tmp_path), "test")
    root = data_paths.data_dir()
    note = root / "jarvis" / "memory" / "note.md"
    note.parent.mkdir(parents=True)
    note.write_text("original memory", encoding="utf-8")
    archive = tmp_path / "backup.zip"
    maintenance.backup(archive)
    assert "jarvis.db" in maintenance.verify(archive)["files"]
    note.write_text("changed", encoding="utf-8")
    run_store.update_run(run_id, summary="changed")
    result = maintenance.restore(archive)
    assert note.read_text(encoding="utf-8") == "original memory"
    assert run_store.get_run(run_id)["summary"] == ""
    assert Path(result["previous_data"]).is_relative_to(root)
    assert (Path(result["previous_data"]) / "jarvis/memory/note.md").read_text(encoding="utf-8") == "changed"


def test_restore_refuses_a_live_instance(tmp_path):
    maintenance.register_runtime()
    with pytest.raises(ValueError, match="Stop JARVIS"):
        maintenance.restore(tmp_path / "missing.zip")
    maintenance.unregister_runtime()


def test_runtime_and_restore_exclusion_is_held_by_os_lock():
    first = maintenance._acquire_lock()
    try:
        with pytest.raises(RuntimeError, match="owns this data directory"):
            maintenance._acquire_lock()
    finally:
        maintenance._release_lock(first)
    second = maintenance._acquire_lock()
    maintenance._release_lock(second)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/outside", "a\\outside", "CON.txt", "a/../b"])
def test_archive_paths_are_checked_before_restore(tmp_path, name):
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(name, "bad")
        z.writestr("manifest.json", json.dumps({"version": 1, "files": {
            name: hashlib.sha256(b"bad").hexdigest()}}))
    with pytest.raises(ValueError, match="Unsafe|differs from its manifest"):
        maintenance.verify(archive)
    assert not (tmp_path.parent / "escape").exists()


def test_checksum_tampering_is_refused(tmp_path):
    archive = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("jarvis.db", "tampered")
        z.writestr("manifest.json", json.dumps({"version": 1, "files": {"jarvis.db": "wrong"}}))
    with pytest.raises(ValueError, match="Checksum"):
        maintenance.verify(archive)


def test_retention_is_opt_in_and_preserves_active_and_resume_parents(tmp_path):
    run_store.init_db()
    parent = run_store.create_run("parent", "p", ".", "test")
    child = run_store.create_run("child", "p", ".", "test", resume_from=parent)
    old = run_store.create_run("old", "p", ".", "test")
    for run_id in (parent, old):
        run_store.update_run(run_id, status="succeeded")
    with run_store._connect() as conn:
        conn.execute("UPDATE runs SET created_at=1")
    run_store.append_event(old, 1, "text", "{}")
    report = maintenance.prune(1)
    assert report["days"] == 1 and report["runs"] == {"eligible": 1, "deleted": 0}
    assert run_store.get_run(old)
    assert maintenance.prune(1, apply=True)["runs"]["deleted"] == 1
    assert run_store.get_run(parent) and run_store.get_run(child)
    assert run_store.get_run(old) is None
    assert run_store.count_events(old) == 0


def test_export_contains_runs_and_events(tmp_path):
    run_store.init_db()
    run_id = run_store.create_run("hello", "p", ".", "test")
    run_store.append_event(run_id, 1, "text", "{}")
    output = tmp_path / "export.jsonl"
    maintenance.export_runs(output)
    assert [json.loads(line)["type"] for line in output.read_text(encoding="utf-8").splitlines()] == ["run", "event"]


# --- the archive is memory, not the key to writing it -----------------------

def test_the_tool_token_is_not_in_the_archive(tmp_path):
    """`jarvis/tool-token` admits a caller to `remember` and every mutating
    route. It sat in the same unencrypted zip as MEMORY.md and every note.
    A restored install mints a fresh one on its next start."""
    run_store.init_db()
    data_paths.ensure_tool_token()
    archive = tmp_path / "backup.zip"

    maintenance.backup(archive)

    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    assert "jarvis/tool-token" not in names
    assert "jarvis.db" in names
    maintenance.verify(archive)


def test_restoring_an_older_archive_that_carries_a_token_drops_it(tmp_path, monkeypatch):
    run_store.init_db()
    data_paths.ensure_tool_token()
    archive = tmp_path / "old.zip"
    rule = maintenance._EXCLUDED_FILES
    monkeypatch.setattr(maintenance, "_EXCLUDED_FILES", frozenset())
    maintenance.backup(archive)
    with zipfile.ZipFile(archive) as z:
        assert "jarvis/tool-token" in z.namelist(), "precondition: an old-style archive"
    # Put the rule back by hand: `monkeypatch.undo()` would also undo the
    # data-dir isolation and point `restore` at the live install.
    monkeypatch.setattr(maintenance, "_EXCLUDED_FILES", rule)

    maintenance.restore(archive)

    assert not data_paths.tool_token_path().exists()


# --- the index can be repaired from the command line, offline ---------------

def test_reindex_fills_the_index_offline(tmp_path):
    import jarvis_memory
    jarvis_memory.write_memory("StarNet station", "how to work it")

    result = maintenance.reindex()

    assert result["indexed"] == ["starnet-station"]
    assert jarvis_memory.unindexed_memories() == []


def test_reindex_refuses_a_live_instance(tmp_path):
    maintenance.register_runtime()
    with pytest.raises(ValueError, match="Stop JARVIS"):
        maintenance.reindex()


def test_reindex_is_a_command(tmp_path, monkeypatch, capsys):
    import jarvis_memory
    jarvis_memory.write_memory("StarNet station", "how to work it")
    monkeypatch.setattr("sys.argv", ["maintenance.py", "reindex"])

    maintenance.main()

    assert json.loads(capsys.readouterr().out)["indexed"] == ["starnet-station"]
