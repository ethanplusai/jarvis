"""
End-to-end pipeline test for the JARVIS intelligence layer.

Exercises the full pipeline: planning mode detection -> prompt assembly ->
QA verification -> success tracking.

Claude Code execution is mocked for fast, repeatable testing.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from planner import PlanningDecision, detect_planning_mode, gather_project_context
from qa import QAResult
from suggestions import suggest_followup
from templates import get_template
from tracking import SuccessTracker


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test artifacts."""
    d = tempfile.mkdtemp(prefix="jarvis_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tracker():
    """Create an in-memory success tracker."""
    t = SuccessTracker(db_path=":memory:")
    yield t
    t.close()


# ── Stage 1: Planning Mode Detection ──────────────────────────────────


@pytest.mark.asyncio
async def test_planning_mode_simple_task():
    """Simple tasks should not need planning."""
    decision = await detect_planning_mode("Create a Python hello world script")
    assert isinstance(decision, PlanningDecision)
    assert decision.task_type in ("build", "simple")
    assert decision.confidence > 0.0


@pytest.mark.asyncio
async def test_planning_mode_complex_task():
    """Complex tasks should need planning."""
    decision = await detect_planning_mode("Build me a landing page")
    assert decision.needs_planning is True
    assert decision.task_type == "build"
    assert len(decision.missing_info) > 0


@pytest.mark.asyncio
async def test_planning_mode_bypass():
    """Bypass mode should skip planning with smart defaults."""
    decision = await detect_planning_mode("Just build something cool", force_bypass=True)
    assert decision.needs_planning is False
    assert len(decision.smart_defaults) > 0


@pytest.mark.asyncio
async def test_planning_mode_fix_with_context():
    """Fix tasks with specific context should not need planning."""
    decision = await detect_planning_mode("Fix the bug in server.py line 42")
    assert decision.needs_planning is False
    assert decision.task_type == "fix"


# ── Stage 2: Context Gathering ────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_gathering(temp_dir):
    """Context gatherer should read project files."""
    # Set up a minimal project
    Path(temp_dir, "requirements.txt").write_text("flask\npytest\n")
    Path(temp_dir, "main.py").write_text("print('hello')\n")

    context = await gather_project_context(temp_dir)

    assert context["name"] == Path(temp_dir).name
    assert context["requirements_txt"] is not None
    assert "flask" in context["requirements_txt"]
    assert len(context["directory_listing"]) >= 2


@pytest.mark.asyncio
async def test_context_gathering_nonexistent():
    """Context gatherer handles missing directories gracefully."""
    context = await gather_project_context("/nonexistent/path/12345")
    assert context["name"] == "12345"
    assert context["claude_md"] is None


# ── Stage 3: Prompt Assembly ──────────────────────────────────────────


def test_template_matching():
    """Template engine finds matching templates."""
    tmpl = get_template("build", "build a landing page")
    assert tmpl is not None
    assert "## Task" in tmpl
    assert "## Acceptance Criteria" in tmpl


def test_template_no_match():
    """Template engine returns None for unknown types gracefully."""
    tmpl = get_template("unknown_type_xyz", "do something weird")
    assert tmpl is None


# ── Stage 4: Full Pipeline (mocked execution) ────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_mocked(temp_dir, tracker):
    """Full pipeline with mocked Claude Code execution."""
    # 1. Planning mode detection
    decision = await detect_planning_mode("Create a Python hello world script")
    assert decision.task_type in ("build", "simple")

    # 2. Context gathering
    context = await gather_project_context(temp_dir)
    assert context["path"] == temp_dir

    # 3. Template matching
    get_template("build", "create a Python script")
    # May or may not find a matching template - that's fine

    # 4. Mock Claude Code execution - simulate it creating hello.py
    hello_path = Path(temp_dir, "hello.py")
    hello_path.write_text('print("Hello, World!")\n')

    # 5. Verify the file exists and runs
    assert hello_path.exists()
    import subprocess

    result = subprocess.run(
        [sys.executable, str(hello_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Hello" in result.stdout

    # 6. Log the task
    tracker.log_task("build", "Create a Python hello world script", True, 0, 2.5)

    stats = tracker.get_success_rate("build")
    assert stats["total"] >= 1
    assert stats["passed"] >= 1
    assert stats["rate"] > 0


# ── Stage 5: QA Verification ─────────────────────────────────────────


def test_qa_result_dataclass():
    """QA result dataclass works correctly."""
    qa = QAResult(passed=True, issues=[], summary="All good")
    d = qa.to_dict()
    assert d["passed"] is True
    assert d["summary"] == "All good"


def test_qa_result_with_issues():
    """QA result captures issues."""
    qa = QAResult(
        passed=False,
        issues=["Missing file", "Import error"],
        summary="2 issues found",
    )
    assert not qa.passed
    assert len(qa.issues) == 2


# ── Stage 6: Suggestions ─────────────────────────────────────────────


def test_suggestions_no_tests(temp_dir):
    """Suggest tests when none exist."""
    Path(temp_dir, "main.py").write_text("print(1)")
    Path(temp_dir, "utils.py").write_text("x=1")
    Path(temp_dir, "config.py").write_text("y=2")

    s = suggest_followup("build", "built a tool", temp_dir)
    assert s is not None
    assert s.action_type == "tests"


def test_suggestions_none_needed(temp_dir):
    """No suggestion when everything looks good."""
    Path(temp_dir, "main.py").write_text("print(1)")
    Path(temp_dir, "README.md").write_text("# Project")
    os.makedirs(Path(temp_dir, "tests"))
    Path(temp_dir, "tests/test_main.py").write_text("pass")

    s = suggest_followup("build", "built a tool", temp_dir)
    assert s is None


# ── Stage 7: Success Tracking ────────────────────────────────────────


def test_tracker_log_and_query(tracker):
    """Tracker logs tasks and computes success rates."""
    tracker.log_task("build", "task 1", True, 0, 5.0)
    tracker.log_task("build", "task 2", True, 0, 3.0)
    tracker.log_task("build", "task 3", False, 2, 10.0)

    stats = tracker.get_success_rate("build")
    assert stats["total"] == 3
    assert stats["passed"] == 2
    assert stats["failed"] == 1
    assert 60.0 < stats["rate"] < 70.0  # ~66.7%


def test_tracker_avg_duration(tracker):
    """Tracker calculates average duration."""
    tracker.log_task("fix", "fix 1", True, 0, 10.0)
    tracker.log_task("fix", "fix 2", True, 0, 20.0)

    avg = tracker.get_avg_duration("fix")
    assert 14.0 < avg < 16.0  # ~15.0


# ── A/B Testing Integration ──────────────────────────────────────────


def test_ab_tester_integration():
    """A/B tester integrates with templates."""
    from ab_testing import ABTester, PromptTemplate

    tester = ABTester(db_path=":memory:")

    tmpl, exp_id = tester.select_template("landing_page")
    assert isinstance(tmpl, PromptTemplate)
    assert exp_id != ""

    tester.record_result(exp_id, tmpl.version, True)

    stats = tester.get_version_stats("landing_page")
    assert len(stats) > 0

    tester.close()
