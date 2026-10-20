import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_data_dir_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("JARVIS_DATA_DIR", raising=False)
    import data_paths
    importlib.reload(data_paths)
    assert data_paths.data_dir().name == "data"


def test_data_dir_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    assert data_paths.data_dir() == tmp_path


def test_db_path_is_inside_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    assert data_paths.db_path() == tmp_path / "jarvis.db"


def test_data_dir_is_created(monkeypatch, tmp_path):
    target = tmp_path / "nested" / "dir"
    monkeypatch.setenv("JARVIS_DATA_DIR", str(target))
    import data_paths
    importlib.reload(data_paths)
    assert data_paths.data_dir().exists()


def test_brain_home_is_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib, data_paths
    importlib.reload(data_paths)
    assert data_paths.brain_home() == tmp_path / "jarvis"


def test_ensure_brain_home_seeds_the_template_and_spares_an_edit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib, data_paths
    importlib.reload(data_paths)
    home = data_paths.ensure_brain_home()
    claude_md = home / "CLAUDE.md"
    assert claude_md.exists()
    assert "JARVIS" in claude_md.read_text()
    claude_md.write_text("user edited")
    data_paths.ensure_brain_home()
    assert claude_md.read_text() == "user edited"   # never overwritten


# --- the persona template: shipped improvements must arrive, and edits must
# --- never be destroyed ---------------------------------------------------
#
# `ensure_brain_home` used to write CLAUDE.md only when the file was missing,
# so a brain home created once never saw another word of guidance again: every
# behaviour fix shipped after an install's first run was inert on that install.
# The rule now: a file that still matches what JARVIS wrote is JARVIS's to
# update; a file that does not is the user's, and is never touched.

import hashlib
import json
import subprocess

import pytest


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    return data_paths


def _template_text() -> str:
    return (Path(__file__).parent.parent / "jarvis_home" / "CLAUDE.md").read_text()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_missing_persona_is_seeded_and_the_seed_is_recorded(monkeypatch, tmp_path):
    dp = _fresh(monkeypatch, tmp_path)
    assert dp.sync_persona() == "seeded"
    assert dp.persona_path().read_text() == _template_text()
    record = json.loads(dp.persona_seed_path().read_text())
    assert record["sha256"] == _sha(_template_text()), (
        "without the record there is no telling an unedited file from an "
        "edited one at the next upgrade")


def test_an_unedited_older_persona_is_brought_up_to_date(monkeypatch, tmp_path):
    """The whole point: guidance shipped today must reach a brain home that
    was created last week."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_persona()
    old = "# JARVIS\n\nAn older shipped persona.\n"
    dp.persona_path().write_text(old)
    dp.persona_seed_path().write_text(json.dumps({"sha256": _sha(old)}))

    assert dp.sync_persona() == "updated"
    assert dp.persona_path().read_text() == _template_text()
    assert json.loads(dp.persona_seed_path().read_text())["sha256"] == \
        _sha(_template_text()), "the new text is now the thing we compare against"


def test_an_edited_persona_is_never_overwritten_and_names_both_files(
        monkeypatch, tmp_path, caplog):
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_persona()
    mine = _template_text() + "\n\nAlways call me Captain.\n"
    dp.persona_path().write_text(mine)

    with caplog.at_level("WARNING"):
        assert dp.sync_persona() == "kept"
    assert dp.persona_path().read_text() == mine, "the user's words survive"
    said = caplog.text
    assert str(dp.persona_path()) in said and str(dp.persona_template_path()) in said, \
        "a warning that names neither file cannot be acted on"


def test_a_persona_already_current_is_left_exactly_alone(monkeypatch, tmp_path):
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_persona()
    before = dp.persona_path().stat().st_mtime_ns
    assert dp.sync_persona() == "current"
    assert dp.persona_path().stat().st_mtime_ns == before, \
        "an up-to-date file must not be rewritten on every startup"


def test_a_current_persona_with_no_record_is_recorded_not_warned_about(
        monkeypatch, tmp_path, caplog):
    """Byte-identical to what we ship IS unmodified, whatever the record says."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_persona()
    dp.persona_seed_path().unlink()
    with caplog.at_level("WARNING"):
        assert dp.sync_persona() == "current"
    assert json.loads(dp.persona_seed_path().read_text())["sha256"] == \
        _sha(_template_text())
    assert "edited" not in caplog.text.lower()


# --- the first run after this ships, when no record exists at all ----------

def test_first_run_updates_a_persona_it_can_prove_is_a_shipped_template(
        monkeypatch, tmp_path):
    """No record, but the bytes are a version this project once shipped — so
    nobody has touched it, and the update is safe."""
    dp = _fresh(monkeypatch, tmp_path)
    old = "# JARVIS\n\nThe persona as it shipped in some earlier release.\n"
    dp.brain_home().mkdir(parents=True, exist_ok=True)
    dp.persona_path().write_text(old)
    monkeypatch.setattr(dp, "KNOWN_TEMPLATE_HASHES", frozenset({_sha(old)}))

    assert dp.sync_persona() == "updated"
    assert dp.persona_path().read_text() == _template_text()
    assert json.loads(dp.persona_seed_path().read_text())["sha256"] == \
        _sha(_template_text())


def test_first_run_keeps_a_persona_it_cannot_recognise(monkeypatch, tmp_path):
    """No record, and bytes we have never shipped: it may well be the user's
    own work and there is no way to tell. Conservative wins — hands off."""
    dp = _fresh(monkeypatch, tmp_path)
    mine = "# JARVIS\n\nRules I wrote myself before the upgrade.\n"
    dp.brain_home().mkdir(parents=True, exist_ok=True)
    dp.persona_path().write_text(mine)

    assert dp.sync_persona() == "kept"
    assert dp.persona_path().read_text() == mine


def test_an_unreadable_record_is_treated_as_no_record(monkeypatch, tmp_path):
    """Corrupt JSON must never be read as "this still matches", which would
    hand the user's edits straight to the overwriter."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_persona()
    mine = "# JARVIS\n\nMy own rules.\n"
    dp.persona_path().write_text(mine)
    dp.persona_seed_path().write_text("{not json at all")

    assert dp.sync_persona() == "kept"
    assert dp.persona_path().read_text() == mine


def test_ensure_brain_home_runs_the_sync(monkeypatch, tmp_path):
    """The startup path is the one that matters; `sync_persona` is only where
    the decision lives."""
    dp = _fresh(monkeypatch, tmp_path)
    dp.sync_persona()
    old = "# JARVIS\n\nolder\n"
    dp.persona_path().write_text(old)
    dp.persona_seed_path().write_text(json.dumps({"sha256": _sha(old)}))
    dp.ensure_brain_home()
    assert dp.persona_path().read_text() == _template_text()


def test_every_template_this_project_has_shipped_is_listed(monkeypatch, tmp_path):
    """The history list is the only thing that tells an unedited old file from
    an edited one on the very first run after an upgrade. A template change
    that lands without its hash silently marks that release's users "edited",
    and they never receive another improvement — so the chore is enforced
    here rather than remembered."""
    dp = _fresh(monkeypatch, tmp_path)
    repo = Path(__file__).resolve().parents[1]
    rel = "jarvis_home/CLAUDE.md"
    try:
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%H", "--", rel],
            capture_output=True, text=True, timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError):      # pragma: no cover
        pytest.skip("no usable git checkout here")
    if not commits:                                    # pragma: no cover
        pytest.skip("no history for the template here")

    missing = {}
    for commit in commits:
        blob = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{rel}"],
                              capture_output=True, timeout=60).stdout
        digest = dp._sha256(blob)
        if digest not in dp.KNOWN_TEMPLATE_HASHES:
            missing[digest] = commit[:8]
    here = dp._sha256(dp.persona_template_path().read_bytes())
    if here not in dp.KNOWN_TEMPLATE_HASHES:
        missing[here] = "the working tree"
    assert not missing, (
        "add these to data_paths.KNOWN_TEMPLATE_HASHES: "
        + ", ".join(f"{d} ({c})" for d, c in missing.items()))
