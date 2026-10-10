"""Stopping him with a keystroke rather than a word.

Interrupting by voice is what the echo machinery exists to make safe, and it
is only ever partly safe: over a speaker his own voice returns garbled, and a
mis-hear that happens to look like "stop" cuts him off at random. A keystroke
cannot be misheard, so the UI carries the one interruption that must always
work.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_hush_stops_him_and_drops_what_was_left():
    """"Be quiet" is not "hold that thought": whatever he had left to say is
    dropped rather than queued up to arrive later.

    Asserted against the server's own branch, not a stand-in: the handler
    lives inline in the websocket loop and there is nothing else to call.
    """
    src = (Path(__file__).parent.parent / "server.py").read_text(encoding="utf-8")
    branch = src[src.index('if kind == "hush":'):]
    branch = branch[:branch.index("if kind == \"interim\":")]
    assert "barge_in(keep_unread=False" in branch, branch
    assert "continue" in branch, "must not fall through to the other handlers"


def test_hush_is_not_reachable_as_a_spoken_word():
    """A spoken interrupt is the thing being avoided; it must not have been
    quietly added to the cancel words as well."""
    speech_src = (Path(__file__).parent.parent / "speech.py").read_text(encoding="utf-8")
    cancel = speech_src[speech_src.index("CANCEL_WORDS = "):]
    cancel = cancel[:cancel.index("\n")]
    assert "hush" not in cancel, cancel


def test_the_client_sends_a_key_press_not_a_word():
    """A spoken interrupt is exactly what we are avoiding here."""
    main = (Path(__file__).parent.parent / "frontend/src/main.ts").read_text(encoding="utf-8")
    assert '"hush"' in main
    assert 'e.key === "Escape"' in main


def test_the_button_only_exists_while_he_is_speaking():
    main = (Path(__file__).parent.parent / "frontend/src/main.ts").read_text(encoding="utf-8")
    assert 'hidden = newState !== "speaking"' in main


def test_the_client_silences_itself_without_waiting_for_the_server():
    """The round trip is real; silence has to be immediate or the button
    feels broken."""
    main = (Path(__file__).parent.parent / "frontend/src/main.ts").read_text(encoding="utf-8")
    hush = main[main.index("function hush()"):]
    assert hush.index("audioPlayer.stop()") < hush.index('socket.send({ type: "hush" })')
