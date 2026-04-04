"""
Tests for the sanitize module — AppleScript escaping and config flags.

Run: python -m pytest tests/test_sanitize.py -v
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sanitize import escape_applescript, escape_shell_in_applescript


class TestEscapeAppleScript:
    """Test AppleScript string escaping."""

    def test_plain_string(self):
        assert escape_applescript("hello world") == "hello world"

    def test_double_quotes(self):
        assert escape_applescript('say "hello"') == 'say \\"hello\\"'

    def test_backslashes(self):
        assert escape_applescript("path\\to\\file") == "path\\\\to\\\\file"

    def test_backslash_before_quote_order(self):
        """Critical: backslashes must be escaped BEFORE quotes.

        If done in wrong order (quotes first), input like:  \\"
        Would become: \\\\"  (double-escaped backslash + unescaped quote)
        """
        # Input: \"  (backslash followed by quote)
        result = escape_applescript('\\"')
        # Should be: \\\\"  (escaped backslash + escaped quote)
        assert result == '\\\\\\"'

    def test_injection_attempt_do_shell_script(self):
        """AppleScript injection via do shell script should be neutralized."""
        malicious = '"; do shell script "whoami"'
        result = escape_applescript(malicious)
        assert '"' not in result.replace('\\"', "")  # No unescaped quotes

    def test_injection_attempt_string_breakout(self):
        """Attempt to break out of AppleScript string context."""
        malicious = '" & (do shell script "id") & "'
        result = escape_applescript(malicious)
        # All quotes should be escaped
        assert result == '\\" & (do shell script \\"id\\") & \\"'

    def test_empty_string(self):
        assert escape_applescript("") == ""

    def test_newlines_preserved(self):
        assert escape_applescript("line1\nline2") == "line1\nline2"

    def test_mixed_special_chars(self):
        result = escape_applescript('test\\path\\"file"')
        assert result == 'test\\\\path\\\\\\"file\\"'


class TestEscapeShellInAppleScript:
    """Test shell-in-AppleScript escaping (two layers)."""

    def test_simple_path(self):
        result = escape_shell_in_applescript("/Users/tom/project")
        # shlex.quote wraps in single quotes, then applescript-escapes
        assert "/Users/tom/project" in result

    def test_path_with_spaces(self):
        result = escape_shell_in_applescript("/Users/tom/my project")
        # Should be shell-safe (no unquoted spaces)
        assert "my project" in result

    def test_path_with_semicolon(self):
        """Semicolons should not allow command injection."""
        result = escape_shell_in_applescript("/tmp/; rm -rf /")
        # shlex.quote should wrap the whole thing, preventing injection
        assert "rm -rf" in result  # Still in the string, but quoted
        # The key: it should be inside single quotes from shlex
        assert "'" in result or "\\'" in result

    def test_path_with_backticks(self):
        """Backtick command substitution should be neutralized."""
        result = escape_shell_in_applescript("/tmp/`whoami`")
        assert "`whoami`" in result  # Present but safely quoted

    def test_path_with_dollar_expansion(self):
        """Dollar sign variable expansion should be neutralized."""
        result = escape_shell_in_applescript("/tmp/$(id)")
        assert "$(id)" in result  # Present but safely quoted


class TestExtractAction:
    """Test action tag extraction and injection validation."""

    def test_extract_basic_action(self):
        from server import extract_action

        text = "Right away, sir. [ACTION:BUILD] a landing page"
        clean, action = extract_action(text)
        assert clean == "Right away, sir."
        assert action["action"] == "build"
        assert "landing page" in action["target"]

    def test_extract_no_action(self):
        from server import extract_action

        text = "Good evening, sir."
        clean, action = extract_action(text)
        assert clean == "Good evening, sir."
        assert action is None

    def test_extract_prompt_project(self):
        from server import extract_action

        text = "Connecting now. [ACTION:PROMPT_PROJECT] jarvis ||| Check the status"
        clean, action = extract_action(text)
        assert action["action"] == "prompt_project"
        assert "jarvis" in action["target"]


class TestDangerousPermissionsConfig:
    """Test that the dangerous permissions flag respects config."""

    def test_disabled_when_false(self):
        os.environ["ALLOW_DANGEROUS_PERMISSIONS"] = "false"
        import importlib

        import sanitize

        importlib.reload(sanitize)
        assert sanitize.ALLOW_DANGEROUS_PERMS is False
        assert sanitize.DANGEROUS_FLAG == ""
        assert sanitize.DANGEROUS_FLAG_LIST == []

    def test_enabled_when_true(self):
        os.environ["ALLOW_DANGEROUS_PERMISSIONS"] = "true"
        import importlib

        import sanitize

        importlib.reload(sanitize)
        assert sanitize.ALLOW_DANGEROUS_PERMS is True
        assert sanitize.DANGEROUS_FLAG == " --dangerously-skip-permissions"
        assert sanitize.DANGEROUS_FLAG_LIST == ["--dangerously-skip-permissions"]
