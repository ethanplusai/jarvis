from contextlib import closing
import run_store


def test_history_cursor_keeps_timestamp_ties_and_old_active_runs(tmp_path):
    run_store.init_db()
    active = run_store.create_run("active", "project", str(tmp_path), "test")
    ids = [run_store.create_run(str(i), "project", str(tmp_path), "test") for i in range(105)]
    with closing(run_store._connect()) as conn, conn:
        conn.execute("UPDATE runs SET created_at=100")
        conn.execute("UPDATE runs SET status='succeeded' WHERE id<>?", (active,))
        conn.execute("UPDATE runs SET created_at=1 WHERE id=?", (active,))
    page = run_store.list_runs(status=["succeeded"], limit=100)
    second = run_store.list_runs(status=["succeeded"], limit=100,
                                 before=page[-1]["created_at"], before_id=page[-1]["id"])
    assert len(page) == 100 and len(second) == 5
    assert {r["id"] for r in page + second} == set(ids)
    assert [r["id"] for r in run_store.list_runs(status=["queued", "running"])] == [active]


def test_token_stats_count_cache_reads_and_writes(tmp_path):
    run_store.init_db()
    run = run_store.create_run("x", "p", str(tmp_path), "test")
    run_store.update_run(run, input_tokens=1, output_tokens=2,
                         cache_read_tokens=100, cache_creation_tokens=20)
    assert run_store.stats()["total_tokens"] == 123
