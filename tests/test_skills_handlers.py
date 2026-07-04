"""Unit tests for the executable skill handlers added alongside the
Personal Assistant catalog — password, unit conversion, markdown→HTML,
reading list, and daily brief. Artifacts are redirected to a temp dir so
data/ stays clean.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import skills

_TMP = Path(tempfile.mkdtemp(prefix="jarvis_test_artifacts_"))
skills.ARTIFACTS_DIR = _TMP


def test_catalog_registers_new_skills():
    slugs = {s[0] for s in skills._CATALOG}
    for slug in ("daily-brief", "reading-list", "password-generator",
                 "unit-converter", "markdown-to-html", "focus-sprint", "decision-helper",
                 "text-stats", "json-formatter", "csv-to-table", "timezone-converter",
                 "date-calculator", "checklist-builder", "fact-check", "regex-builder",
                 "email-reply", "wellness-break"):
        assert slug in slugs, slug
    for slug in skills.EXECUTABLE_SLUGS:
        # every advertised executable slug must exist in the catalog and have a handler
        assert slug in slugs, slug
        assert slug in skills.EXECUTABLE_HANDLERS, slug


def test_password_generator_no_artifact():
    result = skills.run_skill("password-generator", {"length": 24})
    assert "error" not in result
    assert len(result["content"]) == 24
    assert "path" not in result  # secrets must not be written to disk
    # two runs differ
    other = skills.run_skill("password-generator", {"length": 24})
    assert other["content"] != result["content"]


def test_password_length_clamped():
    result = skills.run_skill("password-generator", {"length": 2})
    assert len(result["content"]) == 8


def test_unit_converter_length():
    result = skills.run_skill("unit-converter", {"value": 5, "from": "miles", "to": "km"})
    assert "error" not in result
    assert "8.04672" in result["content"]


def test_unit_converter_temperature():
    result = skills.run_skill("unit-converter", {"value": 100, "from": "celsius", "to": "fahrenheit"})
    assert "212" in result["content"]


def test_unit_converter_rejects_cross_kind():
    result = skills.run_skill("unit-converter", {"value": 1, "from": "kg", "to": "km"})
    assert "error" in result


def test_unit_converter_rejects_garbage():
    result = skills.run_skill("unit-converter", {"value": "lots", "from": "mi", "to": "km"})
    assert "error" in result


def test_markdown_to_html_artifact():
    md = "# Title\n\nSome **bold** text with [a link](https://x.io).\n\n- one\n- two\n\n```\ncode <here>\n```"
    result = skills.run_skill("markdown-to-html", {"markdown": md, "title": "Test Page"})
    assert "error" not in result
    html = result["content"]
    assert "<h1>Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert '<a href="https://x.io">a link</a>' in html
    assert "<li>one</li>" in html
    assert "code &lt;here&gt;" in html  # code blocks stay escaped
    assert Path(result["path"]).exists()
    assert result["path"].endswith(".html")


def test_reading_list_appends():
    first = skills.run_skill("reading-list", {"title": "Deep Work", "url": "https://example.com", "note": "focus"})
    assert "error" not in first
    second = skills.run_skill("reading-list", {"title": "Atomic Habits"})
    content = second["content"]
    assert "Deep Work" in content and "Atomic Habits" in content
    assert Path(second["path"]).name == "reading_list.md"


def test_daily_brief_artifact():
    result = skills.run_skill("daily-brief", {"focus": "ship the release"})
    assert "error" not in result
    assert "Daily Brief" in result["content"]
    assert "ship the release" in result["content"]
    assert Path(result["path"]).exists()


def test_run_skill_unknown_slug():
    result = skills.run_skill("not-a-skill", {})
    assert "error" in result


def test_text_stats_counts():
    text = "JARVIS analyses text. It counts words and sentences. Reading time matters."
    result = skills.run_skill("text-stats", {"text": text})
    assert "error" not in result
    assert "Words: 11" in result["content"]
    assert "Sentences: 3" in result["content"]
    assert "path" not in result  # analysis only, no artifact


def test_text_stats_empty_errors():
    assert "error" in skills.run_skill("text-stats", {"text": "   "})


def test_json_formatter_valid():
    result = skills.run_skill("json-formatter", {"json": '{"b":1,"a":[1,2]}', "sort_keys": "true"})
    assert "error" not in result
    assert result["content"].index('"a"') < result["content"].index('"b"')
    assert Path(result["path"]).suffix == ".json"


def test_json_formatter_invalid_reports_position():
    result = skills.run_skill("json-formatter", {"json": '{"a": }'})
    assert "error" in result
    assert "line 1" in result["error"]


def test_csv_to_table():
    result = skills.run_skill("csv-to-table", {"csv": "name,qty\nbolts,40\nnuts,12"})
    assert "error" not in result
    assert "| name | qty |" in result["content"]
    assert "| bolts | 40 |" in result["content"]
    assert Path(result["path"]).suffix == ".md"


def test_csv_to_table_pads_ragged_rows():
    result = skills.run_skill("csv-to-table", {"csv": "a,b,c\n1,2"})
    assert "ragged" in result["summary"]
    assert result["content"].splitlines()[-1] == "| 1 | 2 |  |"


def test_timezone_converter_dst_aware():
    result = skills.run_skill("timezone-converter",
                              {"time": "15:00", "date": "2026-06-15", "from": "Warsaw", "to": "UTC"})
    assert "error" not in result
    assert "13:00" in result["content"]  # CEST is UTC+2 in June
    assert "Europe/Warsaw" in result["content"]


def test_timezone_converter_handles_pm_and_day_shift():
    result = skills.run_skill("timezone-converter",
                              {"time": "11pm", "date": "2026-06-15", "from": "Tokyo", "to": "UTC"})
    assert "14:00" in result["content"]
    result = skills.run_skill("timezone-converter",
                              {"time": "16:00", "date": "2026-06-15", "from": "UTC", "to": "Tokyo"})
    assert "01:00" in result["content"]
    assert "(next day)" in result["content"]


def test_timezone_converter_unknown_zone_errors():
    assert "error" in skills.run_skill("timezone-converter",
                                       {"time": "10:00", "from": "Atlantis", "to": "UTC"})


def test_date_calculator_between():
    result = skills.run_skill("date-calculator", {"from": "2026-01-01", "to": "2026-12-31"})
    assert "error" not in result
    assert "364 day(s)" in result["content"]
    assert "52 week(s)" in result["content"]


def test_date_calculator_add_days():
    result = skills.run_skill("date-calculator", {"date": "2026-06-09", "add_days": 30})
    assert "2026-07-09" in result["content"]


def test_date_calculator_rejects_garbage():
    assert "error" in skills.run_skill("date-calculator", {"from": "soonish", "to": "later"})


def test_checklist_builder_artifact():
    result = skills.run_skill("checklist-builder",
                              {"title": "Launch Day", "items": ["Backup DB", {"item": "Deploy", "note": "after 9am"}]})
    assert "error" not in result
    assert "- [ ] Backup DB" in result["content"]
    assert "- [ ] Deploy — after 9am" in result["content"]
    assert Path(result["path"]).exists()


# --- manual runner (no pytest required) -------------------------------------

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
