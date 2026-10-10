"""usage_store: the last thing the CLI told us about the subscription limits.

The rule every test here defends: NEVER render a number we do not have. A
window nobody has observed reads "unknown", not 0%, and an observation from
four hours ago is labelled as four hours old rather than passed off as now.
"""

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import usage_store
    importlib.reload(usage_store)
    return usage_store


# The event measured live on 2026-09-02. utilization is a
# FRACTION: 0.78 is 78%, which is what the log line printed.
LIVE_EVENT = {
    "status": "allowed_warning",
    "resetsAt": 1788184800,
    "rateLimitType": "seven_day",
    "utilization": 0.78,
    "isUsingOverage": False,
    "surpassedThreshold": 0.75,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.55, "resetsAt": 1788117000},
        "seven_day": {"utilization": 0.78, "resetsAt": 1788184800},
    },
}


def _window(snap, key):
    return next(w for w in snap["windows"] if w["key"] == key)


# ── nothing observed yet ────────────────────────────────────────────────────

def test_no_observation_is_not_zero(store):
    snap = store.snapshot()
    assert snap["measured"] is False
    assert snap["observed_at"] is None
    # Both known windows are still listed — so the UI can say "not measured"
    # about a named window rather than silently omitting it.
    assert [w["key"] for w in snap["windows"]] == ["five_hour", "seven_day"]
    for w in snap["windows"]:
        assert w["utilization"] is None
        assert w["resets_at"] is None
        assert w["observed_at"] is None


def test_latest_is_none_before_anything_is_recorded(store):
    assert store.latest() is None


# ── recording a real event ──────────────────────────────────────────────────

def test_records_both_windows_as_percentages(store):
    now = 1788100000.0
    store.record(LIVE_EVENT, now=now)
    snap = store.snapshot(now=now + 60)

    assert snap["measured"] is True
    assert snap["status"] == "allowed_warning"
    assert snap["observed_at"] == now
    assert snap["age_sec"] == pytest.approx(60)
    assert snap["stale"] is False

    five = _window(snap, "five_hour")
    assert five["utilization"] == 55.0          # 0.55 of the window, not 0.55%
    assert five["resets_at"] == 1788117000
    assert five["expired"] is False
    assert five["stale"] is False

    seven = _window(snap, "seven_day")
    assert seven["utilization"] == 78.0
    assert seven["resets_at"] == 1788184800


def test_a_percentage_encoding_is_not_doubled(store):
    """A fraction can only be 0..1, so anything above 1 is already a percent.
    Guard against the CLI switching encodings and us reporting 5500%."""
    store.record({"unifiedWindows": {"five_hour": {"utilization": 55}}}, now=1000.0)
    assert _window(store.snapshot(now=1000.0), "five_hour")["utilization"] == 55.0


def test_a_genuine_zero_survives(store):
    """0% observed is a real reading and must not be flattened into "unknown"."""
    store.record({"unifiedWindows": {"five_hour": {"utilization": 0}}}, now=1000.0)
    five = _window(store.snapshot(now=1000.0), "five_hour")
    assert five["utilization"] == 0.0
    assert five["observed_at"] == 1000.0


def test_it_survives_a_process_restart(store, monkeypatch):
    store.record(LIVE_EVENT, now=1788100000.0)
    importlib.reload(store)                       # a new JARVIS process
    assert _window(store.snapshot(now=1788100000.0), "seven_day")["utilization"] == 78.0


# ── staleness ───────────────────────────────────────────────────────────────

def test_an_old_observation_is_marked_stale(store):
    now = 1788100000.0
    store.record(LIVE_EVENT, now=now)
    snap = store.snapshot(now=now + 4 * 3600)
    assert snap["stale"] is True
    assert snap["age_sec"] == pytest.approx(4 * 3600)
    assert snap["stale_after_sec"] == store.STALE_AFTER_SEC
    # The numbers are still there — stale means "old", not "gone".
    assert _window(snap, "seven_day")["utilization"] == 78.0
    assert _window(snap, "seven_day")["stale"] is True


def test_a_fresh_observation_is_not_stale(store):
    now = 1788100000.0
    store.record(LIVE_EVENT, now=now)
    assert store.snapshot(now=now + store.STALE_AFTER_SEC - 1)["stale"] is False


def test_a_window_past_its_reset_is_no_longer_a_measurement(store):
    """Once the window has rolled over, the utilisation we hold describes a
    window that no longer exists. Say so rather than show a full gauge."""
    now = 1788100000.0
    store.record(LIVE_EVENT, now=now)
    snap = store.snapshot(now=1788184801)          # one second past seven_day's reset
    assert _window(snap, "seven_day")["expired"] is True
    assert _window(snap, "five_hour")["expired"] is True


# ── merging ─────────────────────────────────────────────────────────────────

def test_a_partial_event_does_not_erase_the_other_window(store):
    """A rejection names one window. That must not wipe what we know about the
    other one — but the survivor keeps its own, older, timestamp."""
    store.record(LIVE_EVENT, now=1000.0)
    store.record({"status": "rejected", "rateLimitType": "five_hour",
                  "resetsAt": 9999999999}, now=5000.0)
    snap = store.snapshot(now=5000.0)

    five = _window(snap, "five_hour")
    assert five["observed_at"] == 5000.0
    assert five["status"] == "rejected"
    assert five["resets_at"] == 9999999999

    seven = _window(snap, "seven_day")
    assert seven["utilization"] == 78.0
    assert seven["observed_at"] == 1000.0          # not re-stamped as fresh


def test_a_newer_reading_replaces_an_older_one(store):
    store.record({"unifiedWindows": {"five_hour": {"utilization": 0.10}}}, now=1000.0)
    store.record({"unifiedWindows": {"five_hour": {"utilization": 0.90}}}, now=2000.0)
    five = _window(store.snapshot(now=2000.0), "five_hour")
    assert five["utilization"] == 90.0 and five["observed_at"] == 2000.0


# ── junk in, nothing invented out ───────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {},
    {"unifiedWindows": "not a dict"},
    {"unifiedWindows": {"five_hour": "nope"}},
    {"unifiedWindows": {"five_hour": {"utilization": "62%"}}},
    {"unifiedWindows": {"five_hour": {"utilization": None, "resetsAt": "soon"}}},
    {"utilization": float("nan")},
    {"utilization": -1},
])
def test_junk_never_becomes_a_number(store, payload):
    store.record(payload, now=1000.0)
    snap = store.snapshot(now=1000.0)
    for w in snap["windows"]:
        assert w["utilization"] is None


def test_a_corrupt_file_reads_as_no_data(store):
    import data_paths
    data_paths.usage_path().write_text("{not json at all", encoding="utf-8")
    assert store.latest() is None
    assert store.snapshot()["measured"] is False


def test_an_unwritable_location_does_not_raise(store, monkeypatch, tmp_path):
    import data_paths
    monkeypatch.setattr(data_paths, "usage_path",
                        lambda: tmp_path / "no" / "such" / "dir" / "usage.json")
    store.record(LIVE_EVENT, now=1000.0)           # must not raise into the brain
    assert store.snapshot()["measured"] is False


def test_the_file_is_json_a_human_can_read(store):
    import data_paths
    store.record(LIVE_EVENT, now=1000.0)
    body = json.loads(data_paths.usage_path().read_text(encoding="utf-8"))
    assert body["windows"]["five_hour"]["utilization"] == 55.0


def test_now_defaults_to_the_wall_clock(store):
    store.record(LIVE_EVENT)
    assert store.snapshot()["age_sec"] < 5
    assert abs(store.latest()["observed_at"] - time.time()) < 5


def test_unknown_windows_are_kept_not_dropped(store):
    """The CLI may add windows (it already has seven_day_opus in some builds).
    An unrecognised key is passed through rather than silently discarded."""
    store.record({"unifiedWindows": {"seven_day_opus": {"utilization": 0.4}}}, now=1000.0)
    snap = store.snapshot(now=1000.0)
    assert _window(snap, "seven_day_opus")["utilization"] == 40.0
    assert [w["key"] for w in snap["windows"]][:2] == ["five_hour", "seven_day"]
