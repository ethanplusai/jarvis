"""
Unit tests for pure-logic helpers in server.py.

Run: python -m pytest tests/test_server_logic.py -v
"""

import pytest
from fastapi import HTTPException

from server import (
    apply_speech_corrections,
    detect_action_fast,
    extract_action,
    format_projects_for_prompt,
    require_auth,
    strip_markdown_for_tts,
)

# ---------------------------------------------------------------------------
# 1. extract_action
# ---------------------------------------------------------------------------


class TestExtractAction:
    """Validate [ACTION:X] tag parsing from LLM responses."""

    def test_build_action(self):
        clean, action = extract_action("On it. [ACTION:BUILD] a landing page")
        assert clean == "On it."
        assert action["action"] == "build"
        assert "landing page" in action["target"]

    def test_browse_action(self):
        clean, action = extract_action("Let me check. [ACTION:BROWSE] https://example.com")
        assert clean == "Let me check."
        assert action["action"] == "browse"
        assert "https://example.com" in action["target"]

    def test_research_action(self):
        clean, action = extract_action("Researching now. [ACTION:RESEARCH] quantum computing")
        assert action["action"] == "research"
        assert "quantum computing" in action["target"]

    def test_prompt_project_action(self):
        clean, action = extract_action("Connecting now. [ACTION:PROMPT_PROJECT] jarvis ||| check status")
        assert action["action"] == "prompt_project"
        assert "jarvis" in action["target"]
        assert "|||" in action["target"]
        assert "check status" in action["target"]

    def test_add_task_action(self):
        clean, action = extract_action("Added. [ACTION:ADD_TASK] Deploy v2")
        assert action["action"] == "add_task"
        assert action["target"] == "Deploy v2"

    def test_remember_action(self):
        clean, action = extract_action("Noted, sir. [ACTION:REMEMBER] User prefers dark mode")
        assert action["action"] == "remember"
        assert "dark mode" in action["target"]

    def test_screen_action(self):
        clean, action = extract_action("Scanning. [ACTION:SCREEN] desktop")
        assert action["action"] == "screen"

    def test_open_terminal_action(self):
        clean, action = extract_action("Opening now. [ACTION:OPEN_TERMINAL] claude")
        assert action["action"] == "open_terminal"

    def test_add_note_action(self):
        clean, action = extract_action("Done. [ACTION:ADD_NOTE] grocery list")
        assert action["action"] == "add_note"

    def test_create_note_action(self):
        clean, action = extract_action("Creating. [ACTION:CREATE_NOTE] Meeting notes")
        assert action["action"] == "create_note"

    def test_read_note_action(self):
        clean, action = extract_action("Reading. [ACTION:READ_NOTE] Shopping list")
        assert action["action"] == "read_note"

    def test_complete_task_action(self):
        clean, action = extract_action("Marking done. [ACTION:COMPLETE_TASK] Deploy v2")
        assert action["action"] == "complete_task"

    def test_clean_text_is_everything_before_tag(self):
        clean, action = extract_action("Right away, sir. I'll handle that. [ACTION:BUILD] portfolio site")
        assert clean == "Right away, sir. I'll handle that."
        assert action is not None

    def test_no_action_returns_original_and_none(self):
        text = "Good evening, sir. How may I help?"
        clean, action = extract_action(text)
        assert clean == text
        assert action is None

    def test_malformed_action_tag_returns_none(self):
        text = "Trying. [ACTION:INVALID] something"
        clean, action = extract_action(text)
        assert clean == text
        assert action is None

    def test_empty_target(self):
        clean, action = extract_action("Here. [ACTION:BUILD] ")
        assert action is not None
        assert action["action"] == "build"
        assert action["target"] == ""

    def test_multiline_target(self):
        text = "Building. [ACTION:BUILD] a site\nwith dark theme"
        clean, action = extract_action(text)
        assert action["action"] == "build"
        # DOTALL means .* matches newlines in the target
        assert "dark theme" in action["target"]


# ---------------------------------------------------------------------------
# 2. detect_action_fast
# ---------------------------------------------------------------------------


class TestDetectActionFast:
    """Keyword-based fast action routing for short commands."""

    def test_returns_none_for_long_messages(self):
        long_msg = "tell me a really long story about something that happened yesterday at the park with friends"
        assert detect_action_fast(long_msg) is None

    def test_describe_screen_look(self):
        assert detect_action_fast("look at my screen")["action"] == "describe_screen"

    def test_describe_screen_whats(self):
        assert detect_action_fast("what's on my screen")["action"] == "describe_screen"

    def test_open_terminal_open_claude(self):
        assert detect_action_fast("open claude")["action"] == "open_terminal"

    def test_open_terminal_start_claude(self):
        assert detect_action_fast("start claude")["action"] == "open_terminal"

    def test_check_calendar_schedule(self):
        assert detect_action_fast("what's my schedule")["action"] == "check_calendar"

    def test_check_calendar_meetings(self):
        assert detect_action_fast("any meetings")["action"] == "check_calendar"

    def test_check_mail_email(self):
        assert detect_action_fast("check my email")["action"] == "check_mail"

    def test_check_mail_new_mail(self):
        assert detect_action_fast("any new mail")["action"] == "check_mail"

    def test_check_dispatch_where(self):
        assert detect_action_fast("where are we")["action"] == "check_dispatch"

    def test_check_dispatch_status(self):
        assert detect_action_fast("status update")["action"] == "check_dispatch"

    def test_check_tasks_my_tasks(self):
        assert detect_action_fast("my tasks")["action"] == "check_tasks"

    def test_check_tasks_todo(self):
        assert detect_action_fast("my to do list")["action"] == "check_tasks"

    def test_check_usage_cost(self):
        assert detect_action_fast("how much have you cost")["action"] == "check_usage"

    def test_check_usage_token(self):
        assert detect_action_fast("token usage")["action"] == "check_usage"

    def test_none_for_casual_conversation(self):
        assert detect_action_fast("hello how are you") is None

    def test_case_insensitive(self):
        assert detect_action_fast("WHAT'S MY SCHEDULE")["action"] == "check_calendar"

    def test_none_for_ambiguous_message(self):
        assert detect_action_fast("good morning jarvis") is None


# ---------------------------------------------------------------------------
# 3. apply_speech_corrections
# ---------------------------------------------------------------------------


class TestApplySpeechCorrections:
    """STT error correction before processing."""

    def test_cloud_code_to_claude_code(self):
        assert "Claude Code" in apply_speech_corrections("open cloud code")

    def test_clock_code_to_claude_code(self):
        assert "Claude Code" in apply_speech_corrections("run clock code")

    def test_quad_code_to_claude_code(self):
        assert "Claude Code" in apply_speech_corrections("start quad code")

    def test_travis_to_jarvis(self):
        assert "JARVIS" in apply_speech_corrections("hey travis")

    def test_jarves_to_jarvis(self):
        assert "JARVIS" in apply_speech_corrections("hello jarves")

    def test_cloud_alone_to_claude(self):
        result = apply_speech_corrections("ask cloud about it")
        assert "Claude" in result
        # Should not become "Claude Code" without "code" following
        assert "Claude Code" not in result

    def test_unchanged_text_passes_through(self):
        text = "what is the weather today"
        assert apply_speech_corrections(text) == text

    def test_multiple_corrections(self):
        result = apply_speech_corrections("hey travis open cloud code")
        assert "JARVIS" in result
        assert "Claude Code" in result

    def test_case_insensitive_cloud(self):
        result = apply_speech_corrections("CLOUD CODE is great")
        assert "Claude Code" in result


# ---------------------------------------------------------------------------
# 4. strip_markdown_for_tts
# ---------------------------------------------------------------------------


class TestStripMarkdownForTts:
    """Markdown removal for clean TTS output."""

    def test_removes_code_blocks(self):
        text = "Here is code:\n```python\nprint('hi')\n```\nDone."
        result = strip_markdown_for_tts(text)
        assert "```" not in result
        assert "print" not in result

    def test_removes_backticks(self):
        result = strip_markdown_for_tts("Use the `grep` command")
        assert "`" not in result
        assert "grep" in result

    def test_removes_bold(self):
        result = strip_markdown_for_tts("This is **important** text")
        assert "**" not in result
        assert "important" in result

    def test_removes_italic(self):
        result = strip_markdown_for_tts("This is *italic* text")
        assert "*" not in result
        assert "italic" in result

    def test_strips_headers(self):
        result = strip_markdown_for_tts("## Section Title\nContent here")
        assert "#" not in result
        assert "Section Title" in result

    def test_converts_links(self):
        result = strip_markdown_for_tts("Visit [Google](https://google.com) now")
        assert "Google" in result
        assert "https://google.com" not in result
        assert "[" not in result

    def test_strips_bullet_points(self):
        result = strip_markdown_for_tts("- First item\n- Second item")
        assert "First item" in result
        assert result.strip().startswith("First")

    def test_strips_numbered_lists(self):
        result = strip_markdown_for_tts("1. First\n2. Second")
        assert "First" in result
        assert "1." not in result

    def test_strips_banned_phrases(self):
        result = strip_markdown_for_tts("Absolutely, I can help with that.")
        assert "absolutely" not in result.lower()

    def test_strips_my_apologies(self):
        result = strip_markdown_for_tts("My apologies, that was wrong.")
        assert "my apologies" not in result.lower()

    def test_strips_great_question(self):
        result = strip_markdown_for_tts("Great question! Here's the answer.")
        assert "great question" not in result.lower()

    def test_plain_text_passes_through(self):
        text = "The weather is nice today."
        result = strip_markdown_for_tts(text)
        assert result.strip() == text


# ---------------------------------------------------------------------------
# 5. format_projects_for_prompt
# ---------------------------------------------------------------------------


class TestFormatProjectsForPrompt:
    """Project list formatting for LLM prompt context."""

    def test_empty_list(self):
        assert format_projects_for_prompt([]) == "No projects found on Desktop."

    def test_single_project(self):
        projects = [{"name": "myapp", "branch": "main", "path": "/Users/tom/Desktop/myapp"}]
        result = format_projects_for_prompt(projects)
        assert "- myapp (main) @ /Users/tom/Desktop/myapp" in result

    def test_multiple_projects(self):
        projects = [
            {"name": "alpha", "branch": "main", "path": "/a"},
            {"name": "beta", "branch": "dev", "path": "/b"},
        ]
        result = format_projects_for_prompt(projects)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "alpha (main) @ /a" in lines[0]
        assert "beta (dev) @ /b" in lines[1]


# ---------------------------------------------------------------------------
# 6. require_auth (async)
# ---------------------------------------------------------------------------


class TestRequireAuth:
    """Bearer token validation for protected endpoints."""

    @pytest.mark.asyncio
    async def test_no_auth_when_token_empty(self):
        import server

        server._AUTH_TOKEN = ""
        result = await require_auth(authorization=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_auth_when_token_empty_with_header(self):
        import server

        server._AUTH_TOKEN = ""
        result = await require_auth(authorization="Bearer anything")
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self):
        import server

        server._AUTH_TOKEN = "test-secret-123"
        result = await require_auth(authorization="Bearer test-secret-123")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_bearer_token_raises_401(self):
        import server

        server._AUTH_TOKEN = "test-secret-123"
        with pytest.raises(HTTPException) as exc_info:
            await require_auth(authorization="Bearer wrong-token")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_authorization_raises_401(self):
        import server

        server._AUTH_TOKEN = "test-secret-123"
        with pytest.raises(HTTPException) as exc_info:
            await require_auth(authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_malformed_authorization_raises_401(self):
        import server

        server._AUTH_TOKEN = "test-secret-123"
        with pytest.raises(HTTPException) as exc_info:
            await require_auth(authorization="Basic dXNlcjpwYXNz")
        assert exc_info.value.status_code == 401


class TestRemoteControlEndpoints:
    """Test that restart/fix-self endpoints respect ALLOW_REMOTE_CONTROL."""

    def test_restart_blocked_when_remote_control_disabled(self, monkeypatch):
        import server
        from api import control as api_control

        monkeypatch.setattr(server, "_AUTH_TOKEN", "")
        monkeypatch.setattr(api_control, "ALLOW_REMOTE_CONTROL", False)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        resp = client.post("/api/restart")
        assert resp.status_code == 403
        assert "Remote control disabled" in resp.json()["error"]

    def test_fix_self_blocked_when_remote_control_disabled(self, monkeypatch):
        import server
        from api import control as api_control

        monkeypatch.setattr(server, "_AUTH_TOKEN", "")
        monkeypatch.setattr(api_control, "ALLOW_REMOTE_CONTROL", False)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        resp = client.post("/api/fix-self")
        assert resp.status_code == 403
        assert "Remote control disabled" in resp.json()["error"]


class TestRateLimiting:
    """Regression: the default 60/minute limit must actually enforce.

    History: Limiter was instantiated and the 429 exception handler was
    registered, but SlowAPIMiddleware was never added to the app, so
    limits were silently ignored. Caught by live smoke; this test
    prevents the same bug coming back.
    """

    def test_slowapi_middleware_is_registered(self):
        from slowapi.middleware import SlowAPIMiddleware

        import server

        middleware_classes = [m.cls for m in server.app.user_middleware]
        assert SlowAPIMiddleware in middleware_classes, (
            "SlowAPIMiddleware must be attached via app.add_middleware(SlowAPIMiddleware); "
            "otherwise Limiter(default_limits=...) is silently ignored."
        )

    def test_default_limit_returns_429_after_threshold(self, monkeypatch):
        """Fire 65 requests at /api/health (unauthenticated) and confirm we hit 429.

        Uses a fresh Limiter with a very low limit so the test finishes in
        milliseconds rather than needing a whole minute to rotate the window.
        """
        from slowapi import Limiter
        from slowapi.util import get_remote_address

        import server

        # Swap in a 5/minute limit so we trigger 429 after only 5 requests.
        test_limiter = Limiter(key_func=get_remote_address, default_limits=["5/minute"])
        monkeypatch.setattr(server.app.state, "limiter", test_limiter)

        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        codes = [client.get("/api/health").status_code for _ in range(8)]
        assert 429 in codes, f"Expected at least one 429 in {codes}"
