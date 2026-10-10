from concurrent.futures import ThreadPoolExecutor
import pytest
import conversation_store as store


def test_duplicate_delivery_is_claimed_once_and_survives_reinitialization():
    store.init_db()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: store.accept("same-id", "do something"), range(8)))
    assert sum(first for first, row in results) == 1
    store.init_db()
    assert store.accept("same-id", "do something")[0] is False
    with pytest.raises(ValueError, match="different content"):
        store.accept("same-id", "different command")


def test_history_pages_and_assistant_deduplication():
    store.init_db()
    store.accept("a", "hello")
    store.record_assistant("answer", "b")
    store.record_assistant("answer", "b")
    latest = store.list_messages(1)
    assert latest[0]["text"] == "answer"
    assert store.list_messages(1, latest[0]["seq"])[0]["text"] == "hello"
    assert len(store.list_messages()) == 2


def test_every_row_carries_when_it_happened(monkeypatch, tmp_path):
    """The page shows a clock on every message. The server's rows are the
    source for everything it recorded, so `created_at` has to be there, be
    seconds since the epoch, and be the moment of the write."""
    import time
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import conversation_store as cs
    cs.init_db()
    before = time.time()
    cs.accept("m-1", "hello")
    cs.record_assistant("Good evening, sir.", "a-1")
    after = time.time()
    rows = cs.list_messages()
    assert [r["id"] for r in rows] == ["m-1", "a-1"]
    for row in rows:
        assert isinstance(row["created_at"], float)
        assert before <= row["created_at"] <= after
