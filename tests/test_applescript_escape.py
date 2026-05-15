"""Unit tests for applescript_escape() — guards against AppleScript injection."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from actions import applescript_escape


def test_plain_string_unchanged():
    assert applescript_escape("hello") == "hello"


def test_double_quote_escaped():
    assert applescript_escape('say "hi"') == 'say \\"hi\\"'


def test_backslash_escaped():
    assert applescript_escape("path\\file") == "path\\\\file"


def test_backslash_escaped_before_quote():
    # The order matters: a literal \" must become \\\" not \\\\\"
    assert applescript_escape('foo\\"bar') == 'foo\\\\\\"bar'


def test_newline_collapsed_to_space():
    assert applescript_escape("line1\nline2") == "line1 line2"


def test_carriage_return_stripped():
    assert applescript_escape("line1\r\nline2") == "line1 line2"


def test_injection_payload_neutralized():
    # An attacker trying to break out of a do script "..." context.
    # Every literal quote in the output must be backslash-escaped so it
    # cannot terminate the host AppleScript string.
    payload = '"; do shell script "rm -rf ~"; --'
    escaped = applescript_escape(payload)
    for idx, ch in enumerate(escaped):
        if ch == '"':
            assert idx > 0 and escaped[idx - 1] == "\\", (
                f"unescaped quote at index {idx} in {escaped!r}"
            )


def test_empty_string():
    assert applescript_escape("") == ""
