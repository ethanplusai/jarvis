"""One retention policy for everything JARVIS accumulates.

`prune` used to reach only finished runs. Everything else grew without a
ceiling: conversation rows (529 in a week), journal entries (~10 a day, one
per brain rotation), the usage log (re-read in full on every request), and
the brain's own Claude Code transcripts (33 MB in a week — one file per
rotation, outside the data directory). Now one command, one cutoff, and the
same contract as before: a report by default, `apply` to delete.

The journal keeps its newest few whatever their age — the boot handover
reads the newest entry, and a quiet week must not leave the brain with no
note at all. Transcripts are opt-in: they are the CLI's files, not ours, and
only the brain's own directory is ever touched.
"""

import json
import os
import time
from contextlib import closing
from pathlib import Path

import pytest

import conversation_store
import data_paths
import jarvis_memory
import maintenance
import run_store
import session_watch

DAY = 86400


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_CLAUDE_CONFIG_DIRS", str(tmp_path / "claude"))
    run_store.init_db()
    conversation_store.init_db()


def _age(path: Path, days: float):
    then = time.time() - days * DAY
    os.utime(path, (then, then))


def _old_conversation_row(message_id, days):
    conversation_store.accept(message_id, f"said {days} days ago")
    with closing(conversation_store.connect()) as conn, conn:
        conn.execute("UPDATE conversation SET created_at=? WHERE id=?", (time.time() - days * DAY, message_id))


def _journal(reason, days, stamp_days_ago=None):
    """A journal file whose FILENAME stamp is `days` old (the stamp is what
    orders the journal, never mtime)."""
    from datetime import datetime
    stamp = datetime.fromtimestamp(time.time() - (stamp_days_ago or days) * DAY).strftime(
        jarvis_memory._JOURNAL_STAMP_FMT)
    path = data_paths.journal_dir() / f"{stamp}-{reason}.md"
    data_paths.ensure_memory_layout()
    path.write_text(f"# handover\n\n{reason}\n", encoding="utf-8")
    return path


def _usage_lines(*ages_days):
    path = data_paths.usage_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for days in ages_days:
            fh.write(json.dumps({"ts": time.time() - days * DAY, "type": "api",
                                 "input_tokens": 1, "output_tokens": 1}) + "\n")
    return path


# --- the report --------------------------------------------------------------------

def test_the_report_names_every_store_and_deletes_nothing():
    _old_conversation_row("old", 40)
    _old_conversation_row("new", 1)
    report = maintenance.prune(30)
    assert report["days"] == 30
    assert set(report) >= {"runs", "conversation", "journal", "usage_log", "transcripts"}
    assert report["conversation"] == {"eligible": 1, "deleted": 0}
    assert len(conversation_store.list_messages()) == 2


def test_runs_keep_their_old_contract_under_the_new_key():
    parent = run_store.create_run("parent", "p", ".", "test")
    run_store.create_run("child", "p", ".", "test", resume_from=parent)
    old = run_store.create_run("old", "p", ".", "test")
    for run_id in (parent, old):
        run_store.update_run(run_id, status="succeeded")
    with run_store._connect() as conn:
        conn.execute("UPDATE runs SET created_at=1")
    assert maintenance.prune(1)["runs"] == {"eligible": 1, "deleted": 0}
    assert maintenance.prune(1, apply=True)["runs"]["deleted"] == 1
    assert run_store.get_run(parent) and run_store.get_run(old) is None


# --- each store --------------------------------------------------------------------

def test_conversation_rows_older_than_the_cutoff_go_and_newer_stay():
    _old_conversation_row("old", 40)
    _old_conversation_row("new", 1)
    result = maintenance.prune(30, apply=True)["conversation"]
    assert result == {"eligible": 1, "deleted": 1}
    assert [m["id"] for m in conversation_store.list_messages()] == ["new"]


def test_the_journal_keeps_its_newest_entries_whatever_their_age():
    for i in range(8):
        _journal(f"rotation-{i}", days=60 + i)          # all far older than the cutoff
    _journal("fresh", days=1)
    result = maintenance.prune(30, apply=True)["journal"]
    kept = sorted(p.name for p in data_paths.journal_dir().glob("*.md"))
    assert len(kept) == maintenance.JOURNAL_KEEP_MIN
    assert any("fresh" in k for k in kept), "the newest is always among the kept"
    assert result["deleted"] == 9 - maintenance.JOURNAL_KEEP_MIN
    assert jarvis_memory.latest_journal() is not None


def test_the_journal_is_judged_by_its_filename_stamp_not_mtime():
    old = _journal("old", days=60)
    _age(old, 0)                                        # touched today, written 60 days ago
    for i in range(maintenance.JOURNAL_KEEP_MIN):
        _journal(f"recent-{i}", days=1 + i * 0.01)
    maintenance.prune(30, apply=True)
    assert not old.exists(), "mtime lied; the name did not"


def test_the_usage_log_is_rewritten_with_only_the_recent_lines():
    path = _usage_lines(40, 35, 2, 1)
    result = maintenance.prune(30, apply=True)["usage_log"]
    assert result == {"eligible": 2, "deleted": 2}
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and all(l["ts"] > time.time() - 30 * DAY for l in lines)


def test_a_garbled_usage_line_is_kept_rather_than_guessed_at():
    path = _usage_lines(40)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    maintenance.prune(30, apply=True)
    assert path.read_text(encoding="utf-8").splitlines() == ["{not json"]


def test_the_brains_transcripts_are_opt_in_and_only_its_own(tmp_path):
    root = tmp_path / "claude"
    own = root / "projects" / session_watch.encode_cwd(session_watch.brain_cwd())
    other = root / "projects" / "C--dev-other"
    for d in (own, other):
        d.mkdir(parents=True)
    old = own / "old.jsonl"; old.write_text("{}\n", encoding="utf-8"); _age(old, 45)
    live = own / "live.jsonl"; live.write_text("{}\n", encoding="utf-8")
    theirs = other / "old.jsonl"; theirs.write_text("{}\n", encoding="utf-8"); _age(theirs, 45)

    assert maintenance.prune(30, apply=True)["transcripts"] == {"eligible": 0, "deleted": 0, "opted_in": False}
    assert old.exists()

    result = maintenance.prune(30, apply=True, transcripts=True)["transcripts"]
    assert result == {"eligible": 1, "deleted": 1, "opted_in": True}
    assert not old.exists() and live.exists() and theirs.exists()


def test_transcript_subagent_folders_go_with_their_session(tmp_path):
    root = tmp_path / "claude"
    own = root / "projects" / session_watch.encode_cwd(session_watch.brain_cwd())
    sub = own / "old" / "subagents"
    sub.mkdir(parents=True)
    old = own / "old.jsonl"; old.write_text("{}\n", encoding="utf-8"); _age(old, 45)
    agent = sub / "agent-1.jsonl"; agent.write_text("{}\n", encoding="utf-8"); _age(agent, 45)
    maintenance.prune(30, apply=True, transcripts=True)
    assert not old.exists() and not (own / "old").exists()


# --- the command -------------------------------------------------------------------

def test_the_command_takes_the_flags(monkeypatch, capsys):
    _old_conversation_row("old", 40)
    monkeypatch.setattr("sys.argv", ["maintenance.py", "prune", "--days", "30", "--apply", "--transcripts"])
    maintenance.main()
    out = json.loads(capsys.readouterr().out)
    assert out["conversation"]["deleted"] == 1
    assert out["transcripts"]["opted_in"] is True


def test_the_launcher_rotates_its_logs_and_keeps_a_few():
    start = (Path(__file__).parent.parent / "scripts/start-jarvis.ps1").read_text(encoding="utf-8")
    assert "Rotate-Log" in start or "rotate" in start.lower()
    assert "KEEP_LOGS" in start or "Select-Object -Skip" in start


def test_it_is_documented():
    text = (Path(__file__).parent.parent / "docs/operations.md").read_text(encoding="utf-8")
    assert "--transcripts" in text
    assert "usage log" in text.lower() or "usage_log" in text
