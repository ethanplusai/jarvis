"""Tests for is_casual_question in work_mode.py."""

from work_mode import is_casual_question


class TestIsCasualQuestion:
    """Test casual vs work-related message detection."""

    # --- Casual patterns ---

    def test_what_time(self):
        assert is_casual_question("what time is it") is True

    def test_how_are_you(self):
        assert is_casual_question("how are you") is True

    def test_good_morning(self):
        assert is_casual_question("good morning") is True

    def test_thanks(self):
        assert is_casual_question("thanks") is True

    def test_hello(self):
        assert is_casual_question("hello") is True

    def test_status_update(self):
        assert is_casual_question("status update") is True

    # --- Short acknowledgments ---

    def test_short_ack_ok(self):
        assert is_casual_question("ok") is True

    def test_short_ack_sure_thing(self):
        assert is_casual_question("sure thing") is True

    def test_short_ack_yeah(self):
        assert is_casual_question("yeah") is True

    # --- Non-casual (work-related) ---

    def test_fix_bug(self):
        assert is_casual_question("fix the bug in server.py") is False

    def test_build_landing_page(self):
        assert is_casual_question("build a landing page") is False

    def test_refactor_auth(self):
        assert is_casual_question("refactor the auth module") is False

    # --- Case insensitive ---

    def test_case_insensitive(self):
        assert is_casual_question("GOOD MORNING") is True

    # --- Edge cases ---

    def test_empty_string(self):
        assert is_casual_question("") is False
