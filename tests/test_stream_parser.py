# tests/test_stream_parser.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import stream_parser

FIXTURE = Path(__file__).parent / "fixtures" / "stream_success.jsonl"


def _events():
    return [stream_parser.parse_line(l) for l in FIXTURE.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_parse_line_returns_dict():
    assert stream_parser.parse_line('{"type":"assistant"}') == {"type": "assistant"}


def test_parse_line_ignores_blank():
    assert stream_parser.parse_line("   ") is None


def test_parse_line_ignores_non_json():
    assert stream_parser.parse_line("not json at all") is None


def test_event_kind_reads_top_level_type():
    assert stream_parser.event_kind({"type": "assistant"}) == "assistant"


def test_event_kind_defaults_to_unknown():
    assert stream_parser.event_kind({}) == "unknown"


def test_fixture_parses_completely():
    events = _events()
    assert len(events) == 14
    assert all(e is not None for e in events)


def test_extract_init_metadata():
    init = [e for e in _events() if e.get("type") == "system" and e.get("subtype") == "init"][0]
    meta = stream_parser.extract_init_metadata(init)
    assert meta["model"]
    assert meta["cwd"]


def test_extract_result_metrics_from_fixture():
    result = [e for e in _events() if e.get("type") == "result"][0]
    m = stream_parser.extract_result_metrics(result)
    assert m["cost_usd"] > 0
    assert m["input_tokens"] == 2
    assert m["output_tokens"] == 4
    assert m["cache_read_tokens"] == 10143
    assert m["cache_creation_tokens"] == 14092
    assert m["num_turns"] == 1
    assert m["result_text"] == "OK"
    assert m["is_error"] is False


def test_extract_result_metrics_handles_missing_usage():
    m = stream_parser.extract_result_metrics({"type": "result"})
    assert m["cost_usd"] == 0.0
    assert m["input_tokens"] == 0
    assert m["is_error"] is False


def test_extract_result_metrics_flags_error():
    m = stream_parser.extract_result_metrics({"type": "result", "is_error": True})
    assert m["is_error"] is True


def test_summarize_assistant_extracts_text():
    event = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Editing the chart component now"}]}}
    assert stream_parser.summarize_assistant(event) == "Editing the chart component now"


def test_summarize_assistant_names_tool_use():
    event = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/Chart.tsx"}}]}}
    assert stream_parser.summarize_assistant(event) == "Edit: /a/b/Chart.tsx"


def test_summarize_assistant_truncates():
    event = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "x" * 500}]}}
    assert len(stream_parser.summarize_assistant(event)) <= 160


def test_summarize_assistant_empty_content():
    assert stream_parser.summarize_assistant({"type": "assistant", "message": {}}) == ""


def test_extract_result_metrics_handles_non_dict_usage():
    m = stream_parser.extract_result_metrics({"type": "result", "usage": "not-a-dict"})
    assert m["input_tokens"] == 0
    assert m["output_tokens"] == 0
    assert m["cache_read_tokens"] == 0
    assert m["cache_creation_tokens"] == 0


def test_extract_result_metrics_handles_non_numeric_num_turns():
    m = stream_parser.extract_result_metrics({"type": "result", "num_turns": "five"})
    assert m["num_turns"] == 0


def test_extract_result_metrics_handles_list_total_cost_usd():
    m = stream_parser.extract_result_metrics({"type": "result", "total_cost_usd": [1, 2]})
    assert m["cost_usd"] == 0.0


def test_extract_result_metrics_handles_non_string_result():
    m = stream_parser.extract_result_metrics({"type": "result", "result": {"foo": "bar"}})
    assert m["result_text"] == ""


def test_extract_result_metrics_preserves_exact_float_cost():
    m = stream_parser.extract_result_metrics({"type": "result", "total_cost_usd": 0.1461015})
    assert m["cost_usd"] == 0.1461015


def test_summarize_assistant_handles_non_dict_message():
    assert stream_parser.summarize_assistant({"type": "assistant", "message": "not-a-dict"}) == ""


def test_summarize_assistant_handles_non_list_content():
    event = {"type": "assistant", "message": {"content": "not-a-list"}}
    assert stream_parser.summarize_assistant(event) == ""


# --- a per-event payload cap ----------------------------------------------
#
# run_executor stored every stream-json line verbatim into
# `run_events.payload`, and STREAM_LINE_LIMIT is 64 MiB — so one line could
# be multiple MiB, permanently, in jarvis.db. A chatty build emits thousands
# of events.
#
# Truncating the LINE would be wrong: the stored payload is re-parsed (by
# the dashboard, and by `assess_outcome`, which reads tool names out of
# assistant turns to decide whether a run actually did anything). A payload
# that no longer parses drops that evidence, and `changed_anything` going
# False turns a real success into a "no changes" alarm.
#
# So `cap_payload` shrinks the long STRINGS inside the event and re-encodes
# it: structure, types and tool names all survive.


def test_a_small_line_is_returned_untouched():
    line = json.dumps({"type": "assistant", "message": {"content": []}})
    assert stream_parser.cap_payload(line, json.loads(line)) == line


def test_an_enormous_tool_result_is_shrunk_but_still_parses():
    big = "x" * (2 * 1024 * 1024)
    event = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": big}]}}
    line = json.dumps(event)
    capped = stream_parser.cap_payload(line, event)

    assert len(capped) < len(line) / 10
    parsed = json.loads(capped)
    assert parsed["type"] == "user"
    block = parsed["message"]["content"][0]
    assert block["tool_use_id"] == "toolu_1"
    assert block["content"].startswith("xxxx")
    assert "truncated" in block["content"]


def test_tool_names_survive_the_cap():
    """`assess_outcome` reads these to decide whether a run changed anything.
    Losing them turns a real success into a NO_CHANGES alarm."""
    event = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "Write",
         "input": {"file_path": "/tmp/x", "content": "y" * (1024 * 1024)}}]}}
    capped = stream_parser.cap_payload(json.dumps(event), event)
    reparsed = stream_parser.parse_line(capped)
    assert reparsed is not None
    assert stream_parser.changed_anything([reparsed]) is True
    _text, tools = stream_parser.assistant_parts(reparsed)
    assert tools == ["Write"]


def test_a_line_that_is_long_from_sheer_count_still_gets_capped():
    """Nothing to shrink string-by-string — a million tiny values. The
    fallback marker keeps the type and says what happened."""
    event = {"type": "user", "message": {"content": [{"i": i} for i in range(200000)]}}
    line = json.dumps(event)
    assert len(line) > stream_parser.PAYLOAD_MAX_CHARS
    capped = stream_parser.cap_payload(line, event)
    assert len(capped) <= stream_parser.PAYLOAD_MAX_CHARS
    parsed = json.loads(capped)
    assert parsed["type"] == "user"
    assert parsed["jarvis_truncated"] is True
    assert parsed["jarvis_original_chars"] == len(line)


def test_the_cap_is_documented_and_sane():
    # Big enough that a long assistant message or a large diff is stored
    # verbatim; small enough that a chatty build cannot add gigabytes to a
    # permanent database.
    assert 64 * 1024 <= stream_parser.PAYLOAD_MAX_CHARS <= 1024 * 1024
    assert stream_parser.PAYLOAD_STRING_MAX < stream_parser.PAYLOAD_MAX_CHARS
