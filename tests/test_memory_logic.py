"""Tests for pure-logic helpers in memory.py."""

from memory import _sanitize_fts_query, format_plan_for_voice, format_tasks_for_voice

# ---------------------------------------------------------------------------
# _sanitize_fts_query
# ---------------------------------------------------------------------------


class TestSanitizeFtsQuery:
    """Test FTS5 query sanitization."""

    def test_normal_query(self):
        result = _sanitize_fts_query("what is python")
        # "what" (4), "python" (6) pass; "is" (2) filtered
        assert "what" in result
        assert "python" in result
        assert "OR" in result

    def test_apostrophes_removed(self):
        result = _sanitize_fts_query("it's a 'test'")
        assert "'" not in result
        # "its" (3) and "test" (4) should pass
        assert "its" in result
        assert "test" in result

    def test_quotes_removed(self):
        result = _sanitize_fts_query('"hello world"')
        assert '"' not in result
        assert "hello" in result
        assert "world" in result

    def test_hyphens_become_spaces(self):
        result = _sanitize_fts_query("real-time data")
        assert "real" in result
        assert "time" in result
        assert "data" in result
        assert "-" not in result

    def test_short_words_filtered(self):
        # All words <= 2 chars: "I" (1), "am" (2), "ok" (2)
        result = _sanitize_fts_query("I am ok")
        assert result == ""

    def test_max_five_words(self):
        result = _sanitize_fts_query("one two three four five six seven eight")
        parts = result.split(" OR ")
        assert len(parts) <= 5

    def test_empty_string(self):
        assert _sanitize_fts_query("") == ""

    def test_asterisks_removed(self):
        result = _sanitize_fts_query("test* wild*card")
        assert "*" not in result


# ---------------------------------------------------------------------------
# format_tasks_for_voice
# ---------------------------------------------------------------------------


class TestFormatTasksForVoice:
    """Test task list formatting for voice output."""

    def test_empty_list(self):
        assert format_tasks_for_voice([]) == "No tasks on the list, sir."

    def test_single_task_with_due_date(self):
        tasks = [{"title": "Call client", "priority": "high", "due_date": "tomorrow"}]
        result = format_tasks_for_voice(tasks)
        assert "One task: Call client." in result
        assert "Due tomorrow." in result

    def test_single_task_no_due_date(self):
        tasks = [{"title": "Call client", "priority": "high", "due_date": None}]
        result = format_tasks_for_voice(tasks)
        assert "One task: Call client." in result
        assert "Due" not in result

    def test_multiple_tasks_with_high_priority(self):
        tasks = [
            {"title": "Deploy server", "priority": "high", "due_date": None},
            {"title": "Write tests", "priority": "high", "due_date": None},
            {"title": "Update docs", "priority": "low", "due_date": None},
        ]
        result = format_tasks_for_voice(tasks)
        assert "3 open tasks" in result
        assert "2 are high priority" in result

    def test_more_than_three_tasks(self):
        tasks = [{"title": f"Task {i}", "priority": "low", "due_date": None} for i in range(5)]
        result = format_tasks_for_voice(tasks)
        assert "And 2 more." in result


# ---------------------------------------------------------------------------
# format_plan_for_voice
# ---------------------------------------------------------------------------


class TestFormatPlanForVoice:
    """Test day plan formatting combining tasks and events."""

    def test_no_tasks_no_events(self):
        result = format_plan_for_voice([], [])
        assert "Your day looks clear, sir." in result

    def test_events_only(self):
        events = [
            {"title": "Standup", "start": "9:00 AM"},
            {"title": "Lunch", "start": "12:00 PM"},
        ]
        result = format_plan_for_voice([], events)
        assert "2 events" in result
        assert "Standup" in result
        assert "Lunch" in result

    def test_tasks_only(self):
        tasks = [
            {"title": "Fix auth bug", "priority": "high"},
            {"title": "Update docs", "priority": "low"},
        ]
        result = format_plan_for_voice(tasks, [])
        assert "2 tasks" in result
        assert "1 high priority" in result

    def test_both_tasks_and_events(self):
        tasks = [{"title": "Deploy", "priority": "high"}]
        events = [{"title": "Meeting", "start": "10:00 AM"}]
        result = format_plan_for_voice(tasks, events)
        assert "1 events" in result
        assert "1 tasks" in result
        assert "Meeting" in result
        assert "Deploy" in result

    def test_shall_i_adjust(self):
        # All non-empty results end with the prompt
        tasks = [{"title": "Something", "priority": "low"}]
        result = format_plan_for_voice(tasks, [])
        assert "Shall I adjust anything?" in result
