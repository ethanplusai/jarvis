"""Tests for planning heuristics in planner.py."""

from planner import _classify_planning_mode_heuristic, _quick_classify

# ---------------------------------------------------------------------------
# _quick_classify
# ---------------------------------------------------------------------------


class TestQuickClassify:
    """Test keyword-based task type detection."""

    def test_build(self):
        assert _quick_classify("build a website") == "build"

    def test_create(self):
        assert _quick_classify("create a new api") == "build"

    def test_fix(self):
        assert _quick_classify("fix the login bug") == "fix"

    def test_debug(self):
        assert _quick_classify("debug the auth flow") == "fix"

    def test_research(self):
        assert _quick_classify("research python frameworks") == "research"

    def test_refactor(self):
        assert _quick_classify("refactor the database module") == "refactor"

    def test_optimize_maps_to_refactor(self):
        assert _quick_classify("optimize the code") == "refactor"

    def test_simple_fallback(self):
        assert _quick_classify("what time is it") == "simple"

    def test_fix_before_build(self):
        # fix_words checked before build_words
        assert _quick_classify("fix and build the project") == "fix"

    def test_refactor_before_research(self):
        # refactor checked before research in the ordering
        # Actually: fix > refactor > research > build
        assert _quick_classify("restructure and research the codebase") == "refactor"


# ---------------------------------------------------------------------------
# _classify_planning_mode_heuristic
# ---------------------------------------------------------------------------


class TestClassifyPlanningModeHeuristic:
    """Test the fallback heuristic planning classifier.

    Note: the function expects LOWERED text (caller does .lower().strip()).
    """

    def test_simple_question(self):
        result = _classify_planning_mode_heuristic("what time is it")
        assert result.needs_planning is False
        assert result.task_type == "simple"

    def test_fix_with_specifics(self):
        result = _classify_planning_mode_heuristic("fix the bug in server.py line 42 error traceback")
        assert result.needs_planning is False
        assert result.task_type == "fix"

    def test_fix_without_specifics(self):
        result = _classify_planning_mode_heuristic("fix the login")
        assert result.needs_planning is True
        assert result.task_type == "fix"
        assert "target_file" in result.missing_info

    def test_short_build(self):
        result = _classify_planning_mode_heuristic("build website")
        assert result.needs_planning is True
        assert result.task_type == "build"
        assert "project_name" in result.missing_info
        assert "tech_stack" in result.missing_info
        assert "design_requirements" in result.missing_info

    def test_longer_build_fewer_missing(self):
        result = _classify_planning_mode_heuristic("build a real estate aggregator with react and python backend")
        assert result.needs_planning is True
        assert result.task_type == "build"
        # Longer prompt (>=8 words) yields fewer missing_info items
        assert "design_requirements" not in result.missing_info

    def test_research(self):
        result = _classify_planning_mode_heuristic("research how to implement oauth")
        assert result.needs_planning is True
        assert result.task_type == "research"
        assert "scope" in result.missing_info

    def test_refactor(self):
        result = _classify_planning_mode_heuristic("refactor the authentication module")
        assert result.needs_planning is True
        assert result.task_type == "refactor"
        assert "target_file" in result.missing_info

    def test_planning_decision_has_confidence(self):
        result = _classify_planning_mode_heuristic("build something")
        assert 0.0 <= result.confidence <= 1.0

    def test_simple_has_empty_missing_info(self):
        result = _classify_planning_mode_heuristic("hello there")
        assert result.missing_info == []

    def test_fix_specifics_need_enough_words(self):
        # Has ".py" but only 3 words — not > 5, so needs planning
        result = _classify_planning_mode_heuristic("fix server.py bug")
        assert result.needs_planning is True
        assert result.task_type == "fix"
