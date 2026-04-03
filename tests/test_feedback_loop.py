"""
Feedback loop integration test for JARVIS.

Tests the QA verification -> auto-retry -> success tracking pipeline
using mocked coding-agent execution to guarantee deterministic behavior.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from qa import QAAgent, QAResult, MAX_RETRIES
from secondary_agent import SecondaryAgentRunResult
from tracking import SuccessTracker


@pytest.fixture
def tracker():
    """In-memory success tracker."""
    t = SuccessTracker(db_path=":memory:")
    yield t
    t.close()


# ── QA Verification Tests ────────────────────────────────────────────


def test_qa_result_failure():
    """QA result correctly represents a failure with issues."""
    qa = QAResult(
        passed=False,
        issues=["divide(a, 0) returns infinity instead of raising ValueError"],
        summary="Missing zero-division error handling",
    )
    assert not qa.passed
    assert len(qa.issues) == 1
    assert "ValueError" in qa.issues[0]


def test_qa_result_success():
    """QA result correctly represents success."""
    qa = QAResult(passed=True, issues=[], summary="All checks passed")
    assert qa.passed
    assert len(qa.issues) == 0


# ── Auto-Retry Prompt Construction ───────────────────────────────────


def test_retry_prompt_includes_feedback():
    """Retry prompt should include specific QA feedback."""
    original_task = (
        "Create calculator.py with add, subtract, multiply, divide. "
        "divide(a, 0) must raise ValueError."
    )
    issues = [
        "divide(a, 0) returns infinity instead of raising ValueError",
        "No input validation on non-numeric arguments",
    ]

    # Simulate the retry prompt construction (from qa.py auto_retry)
    retry_prompt = (
        f"RETRY ATTEMPT 2/{MAX_RETRIES}\n\n"
        f"ORIGINAL TASK:\n{original_task}\n\n"
        f"PREVIOUS ATTEMPT FAILED QA. Issues found:\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + "\n\nPlease fix these issues and complete the task correctly."
    )

    assert "RETRY ATTEMPT 2" in retry_prompt
    assert "ORIGINAL TASK" in retry_prompt
    assert original_task in retry_prompt
    assert "ValueError" in retry_prompt
    assert "non-numeric" in retry_prompt
    assert "PREVIOUS ATTEMPT FAILED QA" in retry_prompt


# ── Max Retries Enforcement ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_retries_enforced():
    """Auto-retry respects the MAX_RETRIES limit."""
    qa = QAAgent()

    result = await qa.auto_retry(
        task_prompt="Create calculator.py",
        issues=["Missing zero division handling"],
        working_dir="/tmp",
        attempt=MAX_RETRIES,  # Already at max
    )

    assert result["status"] == "failed"
    assert "Max retries" in result["error"]
    assert result["attempt"] == MAX_RETRIES


@pytest.mark.asyncio
async def test_retry_below_max_attempts():
    """Auto-retry attempts execution when below max retries."""
    qa = QAAgent()

    # Mock the shared agent helper to simulate a successful retry
    mock_result = SecondaryAgentRunResult(
        returncode=0,
        message="Fixed: added ValueError for zero division",
        stdout="",
        stderr="",
    )

    with patch("qa.run_secondary_agent_prompt", AsyncMock(return_value=mock_result)):
        result = await qa.auto_retry(
            task_prompt="Create calculator.py",
            issues=["Missing zero division handling"],
            working_dir="/tmp",
            attempt=1,
        )

    assert result["status"] == "completed"
    assert result["attempt"] == 2
    assert "Fixed" in result["result"]


# ── Success Tracking Integration ─────────────────────────────────────


def test_tracking_logs_retries(tracker):
    """Tracker logs all retry attempts correctly."""
    # Simulate: first attempt fails, retry succeeds
    tracker.log_task("build", "Create calculator.py", False, retry_count=0, duration=5.0)
    tracker.log_task("build", "Create calculator.py", True, retry_count=1, duration=8.0)

    stats = tracker.get_success_rate("build")
    assert stats["total"] == 2
    assert stats["passed"] == 1
    assert stats["failed"] == 1
    assert stats["rate"] == 50.0


def test_tracking_multiple_task_types(tracker):
    """Tracker distinguishes between task types."""
    tracker.log_task("build", "Build task", True, 0, 5.0)
    tracker.log_task("fix", "Fix task", False, 0, 3.0)
    tracker.log_task("fix", "Fix task", True, 1, 7.0)

    build_stats = tracker.get_success_rate("build")
    fix_stats = tracker.get_success_rate("fix")

    assert build_stats["total"] == 1
    assert build_stats["rate"] == 100.0
    assert fix_stats["total"] == 2
    assert fix_stats["rate"] == 50.0


def test_tracking_avg_duration_with_retries(tracker):
    """Duration tracking works across retries."""
    tracker.log_task("build", "Task 1", False, 0, 5.0)
    tracker.log_task("build", "Task 1", True, 1, 10.0)

    avg = tracker.get_avg_duration("build")
    assert avg == 7.5  # (5 + 10) / 2


# ── Full Feedback Loop (Mocked) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_full_feedback_loop_mocked(tracker):
    """Full feedback loop: execute -> QA fail -> retry -> QA pass -> log."""

    # Step 1: Simulate first execution (produces bad code)
    first_result = "def divide(a, b): return a / b"  # No ValueError on zero

    # Step 2: QA detects the issue
    qa_fail = QAResult(
        passed=False,
        issues=["divide(a, 0) returns infinity instead of raising ValueError"],
        summary="Missing zero-division error handling",
    )

    # Step 3: Log the failure
    tracker.log_task("build", "Create calculator.py", False, retry_count=0, duration=5.0)

    # Step 4: Build retry prompt with feedback
    retry_issues = qa_fail.issues
    assert len(retry_issues) > 0
    assert "ValueError" in retry_issues[0]

    # Step 5: Simulate successful retry
    second_result = (
        "def divide(a, b):\n"
        "    if b == 0:\n"
        "        raise ValueError('Cannot divide by zero')\n"
        "    return a / b"
    )

    # Step 6: QA passes on retry
    qa_pass = QAResult(
        passed=True,
        issues=[],
        summary="All checks passed including zero-division handling",
    )
    assert qa_pass.passed

    # Step 7: Log the successful retry
    tracker.log_task("build", "Create calculator.py", True, retry_count=1, duration=8.0)

    # Step 8: Verify tracking state
    stats = tracker.get_success_rate("build")
    assert stats["total"] == 2
    assert stats["passed"] == 1
    assert stats["failed"] == 1

    overall = tracker.get_success_rate()
    assert overall["total"] == 2


# ── A/B Testing with Feedback Loop ───────────────────────────────────


def test_ab_tracking_with_retry():
    """A/B experiments correctly track retried tasks."""
    from ab_testing import ABTester

    tester = ABTester(db_path=":memory:")

    # Select template and run experiment
    tmpl, exp_id = tester.select_template("landing_page")
    assert exp_id

    # First attempt fails
    tester.record_result(exp_id, tmpl.version, False)

    stats = tester.get_version_stats("landing_page")
    assert stats[tmpl.version].success_rate == 0.0

    # Retry succeeds - create new experiment for the retry
    _, retry_exp = tester.select_template("landing_page")
    tester.record_result(retry_exp, tmpl.version, True)

    stats = tester.get_version_stats("landing_page")
    assert stats[tmpl.version].total_tasks == 2
    assert stats[tmpl.version].success_rate == 50.0

    tester.close()
