import asyncio
import pytest


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    return server_module


class FakeSpeech:
    def __init__(self):
        self.calls = []

    async def say(self, text, priority=None, **k):
        self.calls.append((text, priority))


def _event(kind="needs_you", **over):
    session = {"session_id": "s", "voice_name": "chitauri", "project": "chitauri",
               "state": "needs_you", "needs": "permission prompt",
               "needs_a_human_hand": True, "title": "Fix the redirect",
               "summary": "Fix the redirect", "last_text": "Shall I proceed?",
               # Every real payload carries one (`session_to_dict`), and the
               # announcement says where the prompt is from it.
               "origin": "terminal", "steerable": True}
    session.update(over.pop("session", {}))
    return {"kind": kind, "at": 1.0, "session": session, **over}


@pytest.mark.asyncio
async def test_a_session_that_needs_you_is_announced_urgently(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    await server._announce_needs_you(_event())

    assert speech.calls, "it must be spoken"
    text, priority = speech.calls[0]
    assert "chitauri" in text
    assert priority == server.Priority.URGENT


@pytest.mark.asyncio
async def test_an_announcement_says_when_you_must_act_yourself(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    await server._announce_needs_you(_event())
    text = speech.calls[0][0].lower()

    assert "permission" in text
    # needs_a_human_hand=True in the fixture: the line must actually say the
    # user has to act themselves, not just name the reason. Checking only
    # "permission" would still pass with that clause deleted entirely.
    assert "keystroke" in text or "yourself" in text


@pytest.mark.asyncio
async def test_completions_are_batched_into_one_sentence(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)

    server._pending_completions.clear()
    server._pending_completions.append("chitauri")
    server._pending_completions.append("hammer")
    await server._announce_batch()

    assert len(speech.calls) == 1, "two finishes, one sentence"
    text = speech.calls[0][0]
    assert "chitauri" in text and "hammer" in text
    assert speech.calls[0][1] == server.Priority.LOW
    assert server._pending_completions == []


@pytest.mark.asyncio
async def test_a_single_completion_reads_naturally(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.append("chitauri")

    await server._announce_batch()

    assert "chitauri" in speech.calls[0][0]
    assert "and" not in speech.calls[0][0].split("chitauri")[0]


@pytest.mark.asyncio
async def test_nothing_is_said_when_there_is_nothing_to_batch(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()

    await server._announce_batch()

    assert speech.calls == []


@pytest.mark.asyncio
async def test_announcing_with_no_speech_configured_does_not_raise(wired, monkeypatch):
    server = wired
    monkeypatch.setattr(server, "speech", None)
    await server._announce_needs_you(_event())      # must not raise


# ---------------------------------------------------------------------------
# Review findings 1 & 2: no leading digit / noun-free count, and no more than
# three names spoken in one breath. Exact strings — these are heard, not read.
# ---------------------------------------------------------------------------

_NAMES_POOL = ["chitauri", "hammer", "mercer", "delta", "echo",
               "foxtrot", "golf", "hotel", "india", "juliet"]


@pytest.mark.asyncio
async def test_a_single_completion_names_no_count(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.append("chitauri")

    await server._announce_batch()

    assert speech.calls[0][0] == "chitauri has finished, sir."


@pytest.mark.asyncio
async def test_two_completions_spell_the_count_and_name_the_noun(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.extend(_NAMES_POOL[:2])

    await server._announce_batch()

    assert (speech.calls[0][0]
            == "Two conversations have finished, sir: chitauri and hammer.")


@pytest.mark.asyncio
async def test_three_completions_are_all_named(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.extend(_NAMES_POOL[:3])

    await server._announce_batch()

    assert (speech.calls[0][0] == "Three conversations have finished, sir: "
            "chitauri, hammer and mercer.")


@pytest.mark.asyncio
async def test_four_completions_cap_at_three_names_plus_one_other(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.extend(_NAMES_POOL[:4])

    await server._announce_batch()

    assert (speech.calls[0][0] == "Four conversations have finished, sir: "
            "chitauri, hammer and mercer, and one other.")
    assert "delta" not in speech.calls[0][0], \
        "the fourth name must be summarised, not spoken"


@pytest.mark.asyncio
async def test_five_completions_cap_at_three_names_plus_two_others(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.extend(_NAMES_POOL[:5])

    await server._announce_batch()

    assert (speech.calls[0][0] == "Five conversations have finished, sir: "
            "chitauri, hammer and mercer, and two others.")


@pytest.mark.asyncio
async def test_ten_completions_use_a_numeral_but_keep_the_noun(wired, monkeypatch):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.extend(_NAMES_POOL[:10])

    await server._announce_batch()

    assert (speech.calls[0][0] == "10 conversations have finished, sir: "
            "chitauri, hammer and mercer, and seven others.")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3, 4, 5, 6, 7, 8, 9])
async def test_no_batch_line_below_ten_begins_with_a_digit(wired, monkeypatch, count):
    server = wired
    speech = FakeSpeech()
    monkeypatch.setattr(server, "speech", speech)
    server._pending_completions.clear()
    server._pending_completions.extend(_NAMES_POOL[:count])

    await server._announce_batch()

    text = speech.calls[0][0]
    assert not text[0].isdigit(), f"leading digit in: {text!r}"


def test_the_event_handler_routes_by_kind(wired, monkeypatch):
    server = wired
    routed = []

    def _capture(coro):
        # Every event also fans out to _broadcast_session_event unconditionally,
        # so `len(routed) >= 1` alone passed even with the needs_you/finished
        # routing deleted entirely. Record which coroutine function was
        # actually spawned so routing is checked directly.
        routed.append(coro.cr_code.co_name)
        coro.close()

    monkeypatch.setattr(server, "_spawn", _capture)

    server._on_session_event(_event("needs_you"))
    server._on_session_event(_event("finished", session={"voice_name": "hammer"}))

    assert routed.count("_announce_needs_you") == 1, routed
    assert routed.count("_announce_batch") == 1, routed
    assert "hammer" in server._pending_completions
