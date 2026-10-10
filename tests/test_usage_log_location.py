"""`usage_log.jsonl` goes where everything else goes: `JARVIS_DATA_DIR`.

It was the one file under `data/` that did not go through `data_paths`: the
path was `Path(__file__).parent / "data"`, so the README's recipe for an
isolated instance (`JARVIS_DATA_DIR=/tmp/jarvis-scratch`) appended every
call of the scratch instance to the REAL install's log, and on a redirected
install the file sat outside the private root JARVIS's own tools are walled
from.
"""

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import server as server_module
    importlib.reload(server_module)
    return server_module, tmp_path


def test_the_usage_log_lives_under_the_data_dir(scratch):
    server, data = scratch
    repo_copy = Path(server.__file__).parent / "data" / "usage_log.jsonl"
    before = repo_copy.stat().st_size if repo_copy.exists() else -1

    server._append_usage_entry(12, 34, "api")

    assert (data / "usage_log.jsonl").is_file()
    after = repo_copy.stat().st_size if repo_copy.exists() else -1
    assert after == before, "a scratch instance wrote to the real install"


def test_the_period_summary_reads_the_same_file(scratch):
    server, _data = scratch
    server._append_usage_entry(12, 34, "api")
    server._append_usage_entry(1, 2, "tts")

    totals = server._get_usage_for_period()

    assert totals == {"input_tokens": 13, "output_tokens": 36,
                      "api_calls": 1, "tts_calls": 1}


def test_there_is_no_module_level_path_to_go_stale():
    """A path computed at import is a path computed before a test — or a
    user — sets the environment variable."""
    import server
    assert not hasattr(server, "_USAGE_FILE")
