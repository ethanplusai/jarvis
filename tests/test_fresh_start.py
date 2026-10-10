"""The user can throw away a tainted generation.

A memory writer is gated on the whole generation's taint, not the turn's,
because what it writes becomes trusted text in every later generation. The
refusal told the user to say it again "in a fresh conversation" — and there
was no way for him to start one. Live, on 2026-09-06, that cost him: he showed
JARVIS a website on his second screen, then spent four turns trying to have a
fact saved and was refused every time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import server


def test_the_phrases_a_person_would_actually_say():
    for said in ("start fresh", "Jarvis start fresh",
                 "let's start a fresh conversation",
                 "clear your head", "clear your context",
                 "start over", "new conversation",
                 "forget this conversation"):
        assert server._is_fresh_start(said), said


def test_ordinary_speech_does_not_wipe_his_context():
    """This throws away everything he knows, so it must be hard to say by
    accident."""
    for said in ("what's running right now",
                 "start the build",
                 "start phase six",
                 "did you clear the error",
                 "tell the session to start",
                 "a fresh set of eyes on this",
                 "start a run in chitauri"):
        assert not server._is_fresh_start(said), said


def test_punctuation_and_case_do_not_matter():
    assert server._is_fresh_start("Start Fresh.")
    assert server._is_fresh_start("okay — start fresh, please")


def test_the_line_he_hears_says_it_is_gone():
    assert "Cleared" in server.FRESH_START_LINE


def test_the_line_he_hears_does_not_claim_the_transcript_is_gone():
    """`_start_fresh` rotates the generation. The conversation rows in SQLite
    and the CLI's own transcript of that generation stay on disk, and the
    phrases that trigger it include "forget this conversation" — so the
    spoken line must not promise a deletion nothing performs."""
    line = server.FRESH_START_LINE
    assert "nothing of that conversation left" not in line
    assert "transcript" in line.lower()
    assert line.count(".") <= 2, "two sentences is the ceiling for a spoken line"
