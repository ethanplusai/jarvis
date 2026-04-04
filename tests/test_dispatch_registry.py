"""Tests for DispatchRegistry — SQLite-backed dispatch tracking."""

import time

import pytest


@pytest.fixture
def registry(tmp_path, monkeypatch):
    import dispatch_registry

    monkeypatch.setattr(dispatch_registry, "DB_PATH", tmp_path / "test.db")
    return dispatch_registry.DispatchRegistry()


# ---------- register ----------


def test_register_returns_int(registry):
    did = registry.register("myproject", "/tmp/myproject", "build a thing")
    assert isinstance(did, int)


def test_register_ids_increment(registry):
    id1 = registry.register("proj1", "/tmp/p1", "prompt1")
    id2 = registry.register("proj2", "/tmp/p2", "prompt2")
    assert id2 > id1


def test_register_data_persists(registry):
    did = registry.register("persist-test", "/tmp/persist", "build it")
    result = registry.get_most_recent()
    assert result is not None
    assert result["id"] == did
    assert result["project_name"] == "persist-test"
    assert result["original_prompt"] == "build it"
    assert result["status"] == "pending"


# ---------- update_status ----------


def test_update_status_changes_status(registry):
    did = registry.register("proj", "/tmp/proj", "do stuff")
    registry.update_status(did, "building")
    result = registry.get_most_recent()
    assert result["status"] == "building"


def test_update_status_truncates_response(registry):
    did = registry.register("proj", "/tmp/proj", "do stuff")
    long_response = "x" * 10000
    registry.update_status(did, "completed", response=long_response, summary="done")
    result = registry.get_most_recent()
    assert len(result["claude_response"]) == 5000


def test_update_status_sets_completed_at_for_terminal(registry):
    did = registry.register("proj", "/tmp/proj", "prompt")
    for status in ("completed", "failed", "timeout"):
        registry.update_status(did, status, response="r", summary="s")
        result = registry.get_most_recent()
        assert result["completed_at"] is not None, f"completed_at should be set for '{status}'"


def test_update_status_no_completed_at_for_nonterminal(registry):
    did = registry.register("proj", "/tmp/proj", "prompt")
    registry.update_status(did, "building", response="wip", summary="")
    result = registry.get_most_recent()
    assert result["completed_at"] is None


# ---------- get_most_recent ----------


def test_get_most_recent_empty(registry):
    assert registry.get_most_recent() is None


# ---------- get_active ----------


def test_get_active_returns_active_only(registry):
    id1 = registry.register("active1", "/tmp/a1", "p1")
    id2 = registry.register("active2", "/tmp/a2", "p2")
    id3 = registry.register("done", "/tmp/d", "p3")
    registry.update_status(id1, "building")
    registry.update_status(id2, "planning")
    registry.update_status(id3, "completed", response="ok", summary="ok")

    active = registry.get_active()
    names = {d["project_name"] for d in active}
    assert "active1" in names
    assert "active2" in names
    assert "done" not in names


def test_get_active_ordered_by_updated_desc(registry):
    registry.register("first", "/tmp/1", "p")
    time.sleep(0.05)
    registry.register("second", "/tmp/2", "p")
    active = registry.get_active()
    assert active[0]["project_name"] == "second"


# ---------- get_by_name ----------


def test_get_by_name_partial_match(registry):
    registry.register("my-cool-project", "/tmp/cool", "build")
    result = registry.get_by_name("cool")
    assert result is not None
    assert result["project_name"] == "my-cool-project"


def test_get_by_name_no_match(registry):
    registry.register("alpha", "/tmp/a", "p")
    assert registry.get_by_name("zzz-nonexistent") is None


# ---------- get_recent_for_project ----------


def test_get_recent_for_project_returns_completed_within_window(registry):
    did = registry.register("webapp", "/tmp/webapp", "build")
    registry.update_status(did, "completed", response="done", summary="built it")
    result = registry.get_recent_for_project("webapp", max_age_seconds=300)
    assert result is not None
    assert result["project_name"] == "webapp"


def test_get_recent_for_project_ignores_noncompleted(registry):
    did = registry.register("webapp", "/tmp/webapp", "build")
    registry.update_status(did, "building")
    assert registry.get_recent_for_project("webapp") is None


# ---------- get_recent ----------


def test_get_recent_respects_limit(registry):
    for i in range(10):
        registry.register(f"proj{i}", f"/tmp/{i}", f"prompt{i}")
    assert len(registry.get_recent(limit=3)) == 3


# ---------- format_for_prompt ----------


def test_format_for_prompt_empty(registry):
    assert registry.format_for_prompt() == "No active or recent dispatches."


def test_format_for_prompt_active_and_completed(registry):
    id1 = registry.register("builder", "/tmp/b", "build the thing")
    registry.update_status(id1, "building")
    id2 = registry.register("finished", "/tmp/f", "other thing")
    registry.update_status(id2, "completed", response="ok", summary="shipped it")

    output = registry.format_for_prompt()
    assert "CURRENTLY WORKING ON:" in output
    assert "builder" in output
    assert "RECENTLY COMPLETED:" in output
    assert "finished" in output
