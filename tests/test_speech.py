import asyncio
import base64
import logging
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))


def _split_all(text, **kw):
    from speech import SentenceSplitter
    s = SentenceSplitter(**kw)
    out = []
    for i in range(0, len(text), 7):          # feed in awkward 7-char deltas
        out += s.feed(text[i:i + 7])
    tail = s.flush()
    if tail:
        out.append(tail)
    return out


@pytest.mark.parametrize("text,expected", [
    ("Good evening, sir. All systems nominal.", ["Good evening, sir.", "All systems nominal."]),
    ("Really?! Yes. Quite.", ["Really?!", "Yes.", "Quite."]),
    ("Mr. Rogers is here. Dr. Who left.", ["Mr. Rogers is here.", "Dr. Who left."]),
    ("It costs 3.5 dollars. Edit server.py first.", ["It costs 3.5 dollars.", "Edit server.py first."]),
    ("Use e.g. the second one. Fine.", ["Use e.g. the second one.", "Fine."]),
    ("J. K. Rowling wrote it. Indeed.", ["J. K. Rowling wrote it.", "Indeed."]),
    ('He said "Go." Then left.', ['He said "Go."', "Then left."]),
    ("Well... I suppose. Yes.", ["Well...", "I suppose.", "Yes."]),
    ("No boundary at all", ["No boundary at all"]),
    ("Ends with a question?", ["Ends with a question?"]),
])
def test_splitter_sentences(text, expected):
    assert _split_all(text) == expected


def test_boundary_waits_for_following_whitespace():
    from speech import SentenceSplitter
    s = SentenceSplitter()
    assert s.feed("It is 3.") == []          # could be "3.5"
    assert s.feed("5 dollars. ") == ["It is 3.5 dollars."]


def test_first_chunk_clause_cut_only_once():
    from speech import SentenceSplitter
    s = SentenceSplitter(first_chunk_max=40)
    long = "This is a rather long opening clause, and it keeps going, and going without any full stop for a while yes"
    out = s.feed(long)
    assert out == ["This is a rather long opening clause, and it keeps going,"]
    assert s.emitted == 1
    # after the first chunk no more clause cuts: the rest waits for a sentence end
    assert s.feed(" and more clauses, with commas, everywhere, still no cut") == []
    assert s.flush() == "and going without any full stop for a while yes and more clauses, with commas, everywhere, still no cut"


def test_first_chunk_without_clause_break_waits():
    from speech import SentenceSplitter
    s = SentenceSplitter(first_chunk_max=20)
    assert s.feed("x" * 50) == []
    assert s.flush() == "x" * 50


def test_list_markers_are_not_sentence_ends():
    assert _split_all("1. Check the logs. 2. Restart it.") == ["1. Check the logs.", "2. Restart it."]


class Harness:
    """A scheduler wired to a fake synthesizer and a recording transport."""

    def __init__(self, **kw):
        from speech import SpeechScheduler
        self.msgs = []
        self.fail_texts = set()
        self.synth_calls = []          # every text actually sent to TTS
        self.sched = SpeechScheduler(self.synth, self.emit, pause_after=0.2, stale_after=0.6,
                                     echo_window=0.5, batch_interval=0.3, batch_settle=0.05, **kw)

    async def synth(self, text):
        self.synth_calls.append(text)
        await asyncio.sleep(0.01)
        if text in self.fail_texts:
            return None
        return b"A:" + text.encode()

    async def emit(self, msg):
        self.msgs.append(msg)
        # Behave like the browser: `drop_queued` discards every chunk that has not
        # started playing (all unacked audio except the first); `stop` discards all.
        if msg["type"] in ("drop_queued", "stop"):
            pending = [m for m in self.msgs if m["type"] == "audio"
                       and not m.get("_acked") and not m.get("_dropped")]
            keep = 0 if msg["type"] == "stop" else 1
            for m in pending[keep:]:
                m["_dropped"] = True

    def audio(self):
        return [(m["utt"], m["idx"], m["text"]) for m in self.msgs if m["type"] == "audio"]

    def types(self):
        return [m["type"] for m in self.msgs]

    async def ack(self, utt_id, idx):
        """The client finished playing one chunk."""
        for m in self.msgs:
            if m["type"] == "audio" and m["utt"] == utt_id and m["idx"] == idx and not m.get("_dropped"):
                m["_acked"] = True
        await self.sched.played(utt_id, idx)

    async def ack_all(self, rounds=10):
        """Ack every audio message the client would have played, in order."""
        for _ in range(rounds):
            await asyncio.sleep(0.03)
            for m in list(self.msgs):
                if m["type"] == "audio" and not m.get("_acked") and not m.get("_dropped"):
                    await self.ack(m["utt"], m["idx"])
                    await asyncio.sleep(0.03)


@pytest_asyncio.fixture
async def h():
    harness = Harness()
    await harness.sched.start()
    yield harness
    await harness.sched.stop()



async def _until(predicate, timeout=5.0, poll=0.01):
    """Wait for a condition instead of for a clock.

    The scheduler's own timings are what these tests are about, but the
    machine's are not: `asyncio.sleep(0.15); assert u.sent == 1` asserts
    that the loop kept up, and in a full 2,700-test run it sometimes has
    not. Every such failure here was "not yet", never "wrong" — so the wait
    is generous and the assertion is unchanged.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return predicate()

@pytest.mark.asyncio
async def test_turn_streams_in_order_with_prefetch_and_status(h):
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "One. Two. Three. Fo")
    s.feed(u, "ur.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    # prefetch=2: two chunks sent before any ack
    assert h.audio() == [(u.id, 0, "One."), (u.id, 1, "Two.")]
    assert h.types()[0] == "status" and h.msgs[0]["state"] == "speaking"
    assert base64.b64decode(h.msgs[1]["data"]) == b"A:One."
    await h.ack_all()
    assert [t for _, _, t in h.audio()] == ["One.", "Two.", "Three.", "Four."]
    assert h.msgs[-1] == {"type": "status", "state": "idle"}
    assert u.done and s.is_speaking is False


@pytest.mark.asyncio
async def test_normal_say_queues_behind_turn_without_preempting(h):
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "First sentence. Second sentence.")
    await s.end_turn(u)
    await asyncio.sleep(0.05)
    v = await s.say("Queued line.")
    await asyncio.sleep(0.05)
    assert "drop_queued" not in h.types()
    await h.ack_all()
    assert [t for _, _, t in h.audio()] == ["First sentence.", "Second sentence.", "Queued line."]


@pytest.mark.asyncio
async def test_urgent_preempts_at_boundary_then_resumes_with_bridge(h):
    from speech import Priority
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Beta two. Gamma three. Delta four.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    await h.ack(u.id, 0)                      # chunk 0 heard, chunk 1 is playing
    await asyncio.sleep(0.05)
    urgent = await s.say("Hammer needs you.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.1)
    assert "drop_queued" in h.types()
    await h.ack_all()
    texts = [t for _, _, t in h.audio()]
    assert texts[:2] == ["Alpha one.", "Beta two."]
    assert "Hammer needs you." in texts
    i = texts.index("Hammer needs you.")
    assert texts[i + 1] == "As I was saying —"
    assert texts[i + 2:] == ["Gamma three.", "Delta four."]
    assert u.done and not u.cancelled and urgent.done


@pytest.mark.asyncio
async def test_resume_is_dropped_when_user_spoke_meanwhile(h):
    from speech import Priority
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Beta two. Gamma three.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    await h.ack(u.id, 0)
    await s.say("Urgent thing.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.05)
    await s.user_final("okay thanks for that")   # two+ non-echo words -> barge-in
    await asyncio.sleep(0.05)
    assert "stop" in h.types()
    assert u.cancelled


@pytest.mark.asyncio
async def test_resume_is_dropped_when_stale(h):
    from speech import Priority
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Beta two. Gamma three.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    await h.ack(u.id, 0)
    urgent = await s.say("Urgent thing.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.05)
    await h.ack(u.id, 1)                      # the playing chunk finishes
    await asyncio.sleep(0.7)                     # longer than stale_after
    await h.ack_all()
    texts = [t for _, _, t in h.audio()]
    assert "Urgent thing." in texts and "Gamma three." not in texts
    assert u.cancelled and urgent.done


@pytest.mark.asyncio
async def test_barge_in_marks_unheard_urgent_unread_and_reraises_at_pause(h):
    from speech import Priority
    s = h.sched
    urgent = await s.say("Chitauri is waiting on you.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.1)
    assert h.audio()[0][2] == "Chitauri is waiting on you."
    assert await s.user_final("hang on a second there") == "speech"
    assert h.types()[-2:] == ["stop", "status"] and h.msgs[-1]["state"] == "idle"
    assert urgent.cancelled
    await asyncio.sleep(0.35)                    # pause_after elapses
    await h.ack_all()
    texts = [t for _, _, t in h.audio()]
    assert texts[-1].startswith("Before I forget — Chitauri is waiting on you.")


@pytest.mark.asyncio
async def test_no_proactive_speech_while_user_is_talking(h):
    from speech import Priority
    s = h.sched
    await s.user_interim("so I was thinking that we")
    u = await s.say("A session finished.", priority=Priority.URGENT)
    await asyncio.sleep(0.1)
    assert h.audio() == []                       # held while the user talks
    await asyncio.sleep(0.25)                    # silence >= pause_after
    await h.ack_all(rounds=3)
    assert h.audio()[0][2] == "A session finished." and u.done


@pytest.mark.asyncio
async def test_turn_reply_starts_even_though_user_just_spoke(h):
    s = h.sched
    await s.user_final("what time is it")
    u = s.begin_turn()
    s.feed(u, "Half past nine, sir.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    assert h.audio() == [(u.id, 0, "Half past nine, sir.")]


@pytest.mark.asyncio
async def test_low_items_are_batched_into_one_utterance(h):
    from speech import Priority
    s = h.sched
    # The shared harness settles a batch after 50ms. Under a loaded full
    # run the event loop can stall longer than that between the two `say`
    # calls, the first flushes alone, and this failed for a reason that had
    # nothing to do with batching. Ten times the room, and a wait to match.
    s.batch_settle = 0.5
    await s.say("Chitauri finished.", priority=Priority.LOW)
    await s.say("Hammer finished.", priority=Priority.LOW)
    await asyncio.sleep(0.7)
    await h.ack_all(rounds=4)
    texts = [t for _, _, t in h.audio()]
    assert texts == ["Chitauri finished.", "Hammer finished."]
    assert len({u for u, _, _ in h.audio()}) == 1     # one utterance


@pytest.mark.asyncio
async def test_echo_classification(h):
    s = h.sched
    u = await s.say("All systems nominal, sir.")
    await asyncio.sleep(0.1)
    assert s.classify("all systems nominal sir") == "echo"
    # Two matching words are an echo only while the audio is actually coming
    # out of the speaker -- and it IS. The chunk has been sent and nothing
    # has acked it, and the ack is sent from `source.onended`, so unacked
    # means PLAYING. (This comment used to say the opposite, and so did
    # `_since_last_ack`.)
    assert s.classify("systems nominal") == "echo"
    assert s.classify("yes please do that now") == "speech"
    assert s.classify("nominal but also restart the hammer session") == "speech"
    assert s.classify("") == "echo"
    assert await s.user_final("all systems nominal sir") == "echo"
    assert s.is_speaking                          # echo did not barge in
    await h.ack_all(rounds=3)
    await asyncio.sleep(0.6)                      # echo window expires after ack
    assert s.classify("all systems nominal sir") == "speech"


@pytest.mark.asyncio
async def test_cancel_word_only_inside_cancel_window(h):
    s = h.sched
    assert s.classify("wait") == "speech"
    task = asyncio.create_task(s.open_cancel_window(1.0))
    await asyncio.sleep(0.02)
    assert await s.user_final("wait") == "cancel"
    assert await task is True
    task = asyncio.create_task(s.open_cancel_window(0.1))
    assert await task is False


@pytest.mark.asyncio
async def test_single_word_during_speech_does_not_barge_in_but_cancel_word_does(h):
    s = h.sched
    await s.say("Telling chitauri to proceed with the migration now.")
    await asyncio.sleep(0.1)
    assert await s.user_final("yes") == "speech"
    assert s.is_speaking
    assert await s.user_final("stop") == "speech"
    assert not s.is_speaking and "stop" in h.types()


@pytest.mark.parametrize("text,expected", [
    ("say that again", "replay"),
    ("say it again", "replay"),
    ("repeat", "replay"),
    ("repeat that", "replay"),
    ("sorry Jarvis what did you say", "replay"),
    ("say that again I'm sorry", "replay"),
    ("come again", "replay"),
    ("one more time", "replay"),
    ("pardon", "replay"),
    ("pardon me", "replay"),
    # deliberately NOT a replay: a real question, however related
    ("what did you mean by that", "speech"),
    ("what do you mean", "speech"),
    ("can you explain that again", "speech"),
    ("say more about that", "speech"),
    ("repeat after me", "speech"),
])
def test_replay_classification(h, text, expected):
    assert h.sched.classify(text) == expected


@pytest.mark.asyncio
async def test_replay_resends_identical_audio_with_no_new_synthesis(h):
    """The whole point: replaying is free. No TTS call, no brain turn (the
    scheduler never talks to the brain at all -- that's server.py's job, and
    it is asserted separately in test_voice_ws.py)."""
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Half past nine, sir.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    await h.ack_all()
    calls_before = len(h.synth_calls)

    replayed = await s.replay_last()
    await asyncio.sleep(0.1)
    await h.ack_all()

    assert replayed is True
    assert len(h.synth_calls) == calls_before, "no new synthesis for a replay"
    texts = [t for _, _, t in h.audio()]
    assert texts.count("Half past nine, sir.") == 2
    # and the bytes are byte-identical, not merely the same text re-synthesized
    audio_msgs = [m for m in h.msgs if m["type"] == "audio" and m["text"] == "Half past nine, sir."]
    assert len(audio_msgs) == 2
    assert audio_msgs[0]["data"] == audio_msgs[1]["data"]


def test_last_spoken_is_exempt_from_history_eviction():
    """UTTERANCE_HISTORY (64) caps how many finished utterances are kept, and
    eviction frees their audio. What "say that again" would replay must
    survive that even if 64+ other utterances happen first."""
    from speech import SpeechScheduler, Priority, Utterance, Chunk, UTTERANCE_HISTORY

    async def synth(t):
        return b"x"

    async def emit(m):
        pass

    s = SpeechScheduler(synth, emit)
    keep = Utterance(id=1, priority=Priority.NORMAL, kind="say", created=0.0, closed=True)
    keep.chunks = [Chunk("kept", audio=b"bytes", ready=True)]
    keep.played = 0
    keep.sent = 0
    s.utterances[1] = keep
    s._last_spoken = keep
    for i in range(2, UTTERANCE_HISTORY + 10):
        old = Utterance(id=i, priority=Priority.NORMAL, kind="say", created=0.0, closed=True)
        old.chunks = [Chunk("old", audio=b"bytes", ready=True)]
        old.played = 0
        old.sent = 0
        s.utterances[i] = old

    s._evict_history()

    assert keep.chunks[0].audio == b"bytes", "the replayable utterance's audio must not be freed"
    assert 1 in s.utterances


@pytest.mark.asyncio
async def test_replay_with_nothing_said_yet_returns_false(h):
    assert await h.sched.replay_last() is False


@pytest.mark.asyncio
async def test_replay_resynthesizes_the_same_words_when_audio_is_gone(h):
    """The audio can go away (synth failure, or eviction after a long
    session). The words JARVIS actually said must still come back -- not a
    fresh, possibly different, brain-generated answer."""
    s = h.sched
    u = await s.say("The build is green.")
    await asyncio.sleep(0.1)
    await h.ack_all()
    for c in u.chunks:
        c.audio = None                # simulate audio no longer held

    replayed = await s.replay_last()
    await asyncio.sleep(0.1)
    await h.ack_all()

    assert replayed is True
    assert "The build is green." in h.synth_calls  # re-synthesized, not silently dropped
    texts = [t for _, _, t in h.audio()]
    assert texts.count("The build is green.") == 2


@pytest.mark.asyncio
async def test_replay_via_user_final_barges_in_current_speech(h):
    """"Say that again" while JARVIS is mid-sentence on something else stops
    that utterance immediately rather than queuing the replay behind it."""
    s = h.sched
    old = await s.say("The first thing I said.")
    await asyncio.sleep(0.1)
    await h.ack_all()

    new = s.begin_turn()
    s.feed(new, "Alpha one. Beta two. Gamma three.")
    await s.end_turn(new)
    await asyncio.sleep(0.1)
    assert s.is_speaking

    verdict = await s.user_final("say that again")
    assert verdict == "replay"
    assert "stop" in h.types()
    assert new.cancelled

    replayed = await s.replay_last()
    await asyncio.sleep(0.1)
    await h.ack_all()
    assert replayed is True
    # the REPLAYED utterance is the one that had already finished, not the
    # one just interrupted
    texts = [t for _, _, t in h.audio()]
    assert texts.count("The first thing I said.") == 2
    assert "Alpha one." in texts and texts.count("Alpha one.") == 1


@pytest.mark.asyncio
async def test_synth_failure_emits_text_and_continues(h):
    s = h.sched
    h.fail_texts.add("Second one.")
    u = s.begin_turn()
    s.feed(u, "First one. Second one. Third one.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    await h.ack_all()
    assert [t for _, _, t in h.audio()] == ["First one.", "Third one."]
    assert {"type": "text", "text": "Second one."} in h.msgs
    assert u.done


@pytest.mark.asyncio
async def test_feed_after_barge_in_is_ignored(h):
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. ")
    await asyncio.sleep(0.1)
    await s.barge_in()
    s.feed(u, "Beta two. Gamma three.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    assert [t for _, _, t in h.audio()] == ["Alpha one."]
    assert u.cancelled


@pytest.mark.asyncio
async def test_prepare_hook_is_applied_before_synthesis(h):
    seen = []

    async def synth(text):
        seen.append(text)
        return b"x"

    from speech import SpeechScheduler
    s = SpeechScheduler(synth, h.emit, prepare=lambda t: t.replace("**", ""))
    await s.start()
    await s.say("**Bold** claim.")
    await asyncio.sleep(0.1)
    await s.stop()
    assert seen == ["Bold claim."]


class SlowHarness(Harness):
    """Synthesis takes real time and the transport really awaits."""

    def __init__(self, latency=0.15, **kw):
        super().__init__(**kw)
        self.latency = latency

    async def synth(self, text):
        await asyncio.sleep(self.latency)
        if text in self.fail_texts:
            return None
        return b"A:" + text.encode()

    async def emit(self, msg):
        await asyncio.sleep(0.02)          # a socket send yields
        await super().emit(msg)


@pytest_asyncio.fixture
async def slow():
    harness = SlowHarness()
    await harness.sched.start()
    yield harness
    await harness.sched.stop()


def _audio_matches_text(h):
    for m in h.msgs:
        if m["type"] == "audio":
            assert base64.b64decode(m["data"]) == b"A:" + m["text"].encode(), (m["idx"], m["text"])


@pytest.mark.asyncio
async def test_resume_with_slow_synthesis_speaks_the_right_audio_and_reaches_idle(slow):
    """A streaming turn: later chunks are still being synthesised when the
    preemption and the bridge insertion happen. Every audio chunk must carry the
    audio for its own text, and the turn must finish."""
    from speech import Priority
    s = slow.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Bee two. Cee three. ")
    await asyncio.sleep(0.4)                     # those three are synthesised; 0 and 1 sent
    await slow.ack(u.id, 0)
    await asyncio.sleep(0.05)                    # chunk 2 sent ahead
    s.feed(u, "Dee four. Eee five.")             # synthesis for 3 and 4 now in flight (0.15 s)
    await s.end_turn(u)
    urgent = await s.say("Hammer needs you.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.02)                    # preempt + resume happen while 3/4 still synthesising
    await slow.ack(u.id, 1)
    await slow.ack_all(rounds=30)
    _audio_matches_text(slow)                    # never the wrong audio for a chunk's text
    texts = [t for _, _, t in slow.audio()]
    # Depending on whether the preemption lands before or after chunk 1's ack,
    # "Cee three." is either dropped-and-resent after the bridge or played
    # before the urgent item. Either way: spoken exactly once, order preserved,
    # bridge first after the interruption, and the turn finishes.
    after = texts[texts.index("Hammer needs you.") + 1:]
    assert after[0] == "As I was saying —"
    tail = ["Cee three.", "Dee four.", "Eee five."]
    assert after[1:] == [t for t in tail if t in after]
    assert all(texts.count(t) == 1 for t in tail)
    assert u.done and urgent.done
    assert slow.msgs[-1] == {"type": "status", "state": "idle"}


@pytest.mark.asyncio
async def test_dropped_chunks_do_not_pin_speaking_or_poison_echo(slow):
    from speech import Priority
    s = slow.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Bee two. Gamma three. Delta four.")
    await s.end_turn(u)
    await asyncio.sleep(0.4)
    await slow.ack(u.id, 0)
    await asyncio.sleep(0.2)                     # chunk 2 gets sent ahead (prefetch)
    urgent = await s.say("Urgent thing.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.05)
    await slow.ack(u.id, 1)
    await asyncio.sleep(0.7)                     # paused remainder goes stale
    await slow.ack_all(rounds=15)
    assert urgent.done and u.cancelled
    assert slow.msgs[-1] == {"type": "status", "state": "idle"}
    assert not s.is_speaking
    await asyncio.sleep(0.6)                     # echo window (0.5) passes
    assert s.classify("gamma three") == "speech"     # the dropped chunk was never heard: not echo forever
    assert await s.user_final("hello again there") == "speech"
    assert "stop" not in slow.types()[-2:]            # no spurious barge-in when nothing plays


@pytest.mark.asyncio
async def test_no_audio_is_emitted_after_stop():
    """A barge-in that lands while the send loop is suspended inside a transport
    await must not let the next chunk out after `stop`."""
    from speech import SpeechScheduler
    gate = asyncio.Event()
    msgs = []

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        # The harshest transport model: the send yields BEFORE the frame is
        # committed, so ordering is only preserved if sends are serialized.
        if msg["type"] == "audio" and msg["idx"] == 0:
            await gate.wait()
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, echo_window=0.5)
    await s.start()
    try:
        u = s.begin_turn()
        s.feed(u, "Alpha one. Beta two. Gamma three.")
        await s.end_turn(u)
        await asyncio.sleep(0.1)                 # send loop is parked inside emit(audio 0)
        # The barge-in's `stop` must queue behind the frame already being sent,
        # so barge_in() itself blocks until the transport frees up.
        stopping = asyncio.create_task(s.barge_in())
        await asyncio.sleep(0.05)
        assert not stopping.done()
        gate.set()
        await stopping
        await asyncio.sleep(0.2)
        types = [m["type"] for m in msgs]
        assert "stop" in types
        assert "audio" not in types[types.index("stop") + 1:], types
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_batched_low_say_becomes_done_when_spoken(h):
    from speech import Priority
    s = h.sched
    low = await s.say("Chitauri finished.", priority=Priority.LOW)
    assert not low.done
    await asyncio.sleep(0.1)
    await h.ack_all(rounds=4)
    assert await s.wait_for(low, 1.0) is True
    assert low.done and not low.was_cancelled


@pytest.mark.asyncio
async def test_three_synth_failures_report_once(h):
    s = h.sched
    h.fail_texts.update({"One.", "Two.", "Three.", "Four."})
    u = await s.say("One. Two. Three. Four.")
    await asyncio.sleep(0.2)
    await h.ack_all(rounds=3)
    assert h.msgs.count({"type": "text", "text": "My voice is failing, sir."}) == 1
    assert u.done


@pytest.mark.asyncio
async def test_history_is_bounded(h):
    from speech import UTTERANCE_HISTORY
    s = h.sched
    for i in range(UTTERANCE_HISTORY + 20):
        await s.say(f"Line {i}.")
        await h.ack_all(rounds=1)
    await h.ack_all(rounds=3)
    assert len(s.utterances) <= UTTERANCE_HISTORY + 1


@pytest.mark.asyncio
async def test_cancel_word_does_not_reraise_the_cancelled_announcement(h):
    from speech import Priority
    s = h.sched
    u = await s.say("Deploying in five seconds. Say wait to cancel.", priority=Priority.URGENT, immediate=True)
    await asyncio.sleep(0.1)
    task = asyncio.create_task(s.open_cancel_window(1.0))
    await asyncio.sleep(0.02)
    assert await s.user_final("wait") == "cancel"
    assert await task is True
    assert u.cancelled and not s.is_speaking
    await asyncio.sleep(0.4)                     # a pause passes
    await h.ack_all(rounds=3)
    assert not any(t.startswith("Before I forget") for _, _, t in h.audio())


@pytest.mark.asyncio
async def test_two_urgent_items_produce_one_bridge(h):
    from speech import Priority
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Beta two. Gamma three. Delta four.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    await h.ack(u.id, 0)
    await asyncio.sleep(0.05)
    a = await s.say("First urgent.", priority=Priority.URGENT, immediate=True)
    b = await s.say("Second urgent.", priority=Priority.URGENT, immediate=True)
    await h.ack_all(rounds=15)
    texts = [t for _, _, t in h.audio()]
    bridges = [t for t in texts if t in ("As I was saying —", "Back to it —", "Where was I —")]
    assert bridges == ["As I was saying —"]
    i = texts.index("Second urgent.")
    assert texts[i + 1] == "As I was saying —"
    assert texts.index("First urgent.") < i
    assert u.done and a.done and b.done and h.msgs[-1] == {"type": "status", "state": "idle"}


@pytest.mark.asyncio
async def test_the_first_low_items_of_a_session_are_batched_together(h):
    from speech import Priority
    s = h.sched
    await s.say("Chitauri finished.", priority=Priority.LOW)
    await asyncio.sleep(0.02)
    await s.say("Hammer finished.", priority=Priority.LOW)
    await asyncio.sleep(0.15)
    await h.ack_all(rounds=4)
    assert len({u for u, _, _ in h.audio()}) == 1
    assert [t for _, _, t in h.audio()] == ["Chitauri finished.", "Hammer finished."]


@pytest.mark.asyncio
async def test_a_raising_transport_abandons_the_utterance_instead_of_wedging():
    from speech import SpeechScheduler
    msgs = []
    boom = {"on": 1}

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        if msg["type"] == "audio" and msg["idx"] == boom["on"]:
            raise ConnectionError("client went away")
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2)
    await s.start()
    try:
        u = await s.say("Alpha one. Beta two. Gamma three.")
        await asyncio.sleep(0.2)
        assert u.cancelled and not s.is_speaking
        boom["on"] = -1                              # transport recovers
        later = await s.say("Later line.")
        await asyncio.sleep(0.1)
        await s.played(later.id, 0)
        await asyncio.sleep(0.1)
        assert [m["text"] for m in msgs if m["type"] == "audio"][-1] == "Later line."
        assert later.done and msgs[-1] == {"type": "status", "state": "idle"}
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_voice_failing_notice_arrives_in_order(h):
    s = h.sched
    h.fail_texts.update({"One.", "Two.", "Three."})
    await s.say("One. Two. Three. Four.")
    await asyncio.sleep(0.2)
    await h.ack_all(rounds=3)
    texts = [m.get("text") for m in h.msgs if m["type"] in ("text", "audio")]
    assert texts == ["One.", "Two.", "Three.", "My voice is failing, sir.", "Four."]


@pytest.mark.asyncio
async def test_an_utterance_interrupted_too_often_is_dropped(h):
    from speech import Priority, MAX_RESUMES, BRIDGES
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "Alpha one. Beta two. Gamma three. Delta four. Epsilon five. Zeta six.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)
    await h.ack(u.id, 0)
    await asyncio.sleep(0.05)
    await h.ack(u.id, 1)
    for i in range(MAX_RESUMES + 1):
        urgent = await s.say(f"Urgent {i}.", priority=Priority.URGENT, immediate=True)
        # ack only the urgent item and any bridge — never the turn's own sentences,
        # so the turn keeps being interrupted mid-way
        for _ in range(8):
            await asyncio.sleep(0.03)
            for m in list(h.msgs):
                if (m["type"] == "audio" and not m.get("_acked") and not m.get("_dropped")
                        and (m["utt"] == urgent.id or m["text"] in BRIDGES)):
                    await h.ack(m["utt"], m["idx"])
    await h.ack_all(rounds=6)
    assert u.cancelled and u.resumes <= MAX_RESUMES
    assert h.msgs[-1] == {"type": "status", "state": "idle"}


def test_first_chunk_cuts_early_at_a_strong_break():
    """First audio should not wait for a long first sentence to finish: a dash,
    semicolon or colon past first_chunk_min is a natural place to start speaking."""
    assert _split_all("I can manage your code sessions and answer questions, sir — just tell me what you need.") == [
        "I can manage your code sessions and answer questions, sir —",
        "just tell me what you need.",
    ]
    # too short before the break: no early cut
    assert _split_all("Two things, sir — chitauri and hammer.") == ["Two things, sir — chitauri and hammer."]
    # a comma alone is not a strong break
    assert _split_all("I can manage your code sessions and answer questions, sir, whenever you like.") == [
        "I can manage your code sessions and answer questions, sir, whenever you like."]
    # only the FIRST chunk is cut early
    assert _split_all("Alpha. I can manage your code sessions and answer questions, sir — just tell me.") == [
        "Alpha.", "I can manage your code sessions and answer questions, sir — just tell me."]


# ── final-review fixes ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_lost_ack_does_not_mute_jarvis_forever():
    """One missing `played` (dead tab, parked AudioContext) used to pin the
    current utterance forever. The ack watchdog abandons it."""
    from speech import SpeechScheduler
    msgs = []

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, ack_timeout=0.3)
    await s.start()
    try:
        u = await s.say("Nobody will ack this.")
        await asyncio.sleep(0.1)
        assert s.is_speaking and any(m["type"] == "audio" for m in msgs)
        await asyncio.sleep(0.5)                     # > ack_timeout, no ack ever arrives
        assert u.cancelled and not s.is_speaking
        later = await s.say("Second line.")
        await asyncio.sleep(0.1)
        await s.played(later.id, 0)
        await asyncio.sleep(0.1)
        assert later.done and [m["text"] for m in msgs if m["type"] == "audio"][-1] == "Second line."
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_urgent_speech_with_nobody_listening_is_kept_and_said_later():
    """Spec §4 'No one listening': an URGENT item is never lost because the tab
    was closed — it is re-raised once someone is connected."""
    from speech import SpeechScheduler, Priority
    msgs = []
    listening = {"on": False}

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        if not listening["on"] and msg["type"] in ("audio", "text"):
            raise ConnectionError("no voice client")
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, transport_ready=lambda: listening["on"])
    await s.start()
    try:
        u = await s.say("Hammer needs you.", priority=Priority.URGENT, immediate=True)
        await asyncio.sleep(0.2)
        assert u.cancelled and not any(m["type"] == "audio" for m in msgs)
        await asyncio.sleep(0.4)                     # pauses pass; still nobody listening
        assert not any(m["type"] == "audio" for m in msgs)
        listening["on"] = True                       # a client connects
        await asyncio.sleep(0.5)
        spoken = [m["text"] for m in msgs if m["type"] == "audio"]
        assert spoken and spoken[0].startswith("Before I forget — Hammer needs you.")
    finally:
        await s.stop()


def test_spec_defaults_are_the_spec_defaults():
    from speech import SpeechScheduler, SentenceSplitter

    async def nop(*a):
        return None

    s = SpeechScheduler(nop, nop)
    assert (s.prefetch, s.batch_interval, s.pause_after, s.stale_after, s.echo_window) == (2, 20.0, 3.0, 60.0, 6.0)
    assert s.batch_settle == 2.0 and s.ack_timeout == 45.0
    sp = SentenceSplitter()
    assert (sp._first_chunk_max, sp._first_chunk_min) == (80, 40)   # see test_the_first_chunk_is_capped_at_eighty_characters_by_default


# ── interrupting him while he is still talking ──────────────────────────────
# `user_final` already stopped him on a cancel word, but a final transcript
# only arrives after Chrome has decided the sentence ended — 1.5 to 3 seconds
# of silence later, by which point he has usually finished. The interim path
# is the one that runs WHILE he speaks, and it required two words he had not
# just said. "stop", "wait" and "no" are one word each, so in practice there
# was no way to cut him off.

@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["stop", "wait", "no", "cancel", "hold on"])
async def test_a_cancel_word_interrupts_him_mid_sentence(h, word):
    s = h.sched
    await s.say("Telling chitauri to proceed with the migration now.")
    await asyncio.sleep(0.1)
    assert s.is_speaking
    await s.user_interim(word)
    assert not s.is_speaking, f"{word!r} should have cut him off"


@pytest.mark.asyncio
async def test_two_new_words_still_interrupt(h):
    """The general bar is unchanged."""
    s = h.sched
    await s.say("Telling chitauri to proceed with the migration now.")
    await asyncio.sleep(0.1)
    await s.user_interim("actually hold")
    assert not s.is_speaking


@pytest.mark.asyncio
async def test_one_ordinary_word_still_does_not_interrupt(h):
    """A single stray word — a cough of a transcript — must not stop him."""
    s = h.sched
    await s.say("Telling chitauri to proceed with the migration now.")
    await asyncio.sleep(0.1)
    await s.user_interim("yes")
    assert s.is_speaking


@pytest.mark.asyncio
async def test_hearing_himself_say_stop_does_not_interrupt_him(h):
    """The reason the guard exists: on speakers his own voice comes back in.
    A cancel word he just said himself is an echo, not an instruction."""
    s = h.sched
    await s.say("Stop the migration, sir?")
    await asyncio.sleep(0.1)
    assert s.is_speaking
    await s.user_interim("stop")
    assert s.is_speaking, "that was his own word coming back at him"


# ── the same echo, one inflection away ─────────────────────────────────────
#
# `_should_interrupt` compares whole tokens. He says "Stopped the work in
# chitauri, sir." (server.py's own wording when a run is cancelled), the
# microphone hears the tail of it as "stop", and "stop" is not literally in
# what he said — so `_nonmatching` returns 1, the cancel-word branch fires,
# and he barges in on himself. Same shape for "cancelled" heard as "cancel"
# and "waiting" heard as "wait".
#
# The fix is narrow on purpose: a stem match only rescues the ONE-WORD
# cancel-word branch, and only for tokens of four characters or more whose
# length differs by at most four. The general two-novel-words bar is
# untouched, so a real interruption still cuts him off.

@pytest.mark.asyncio
@pytest.mark.parametrize("said,heard", [
    ("Stopped the work in chitauri, sir.", "stop"),
    ("Stopping the migration now, sir.", "stop"),
    ("Cancelled the run, sir.", "cancel"),
    ("Waiting on the build, sir.", "wait"),
])
async def test_a_clipped_echo_of_his_own_word_does_not_interrupt(h, said, heard):
    s = h.sched
    await s.say(said)
    await asyncio.sleep(0.1)
    assert s.is_speaking
    await s.user_interim(heard)
    assert s.is_speaking, (
        f"heard {heard!r} while saying {said!r} — that is him, not the user")


@pytest.mark.asyncio
async def test_a_real_cancel_word_still_interrupts_a_similar_sentence(h):
    """The guard must not swallow a genuine interruption. Nothing he is
    saying stems to "stop", so "stop" is the user."""
    s = h.sched
    await s.say("Deploying the storefront to production now, sir.")
    await asyncio.sleep(0.1)
    await s.user_interim("stop")
    assert not s.is_speaking


@pytest.mark.asyncio
async def test_the_stem_rescue_does_not_reach_short_words(h):
    """"no" against "nothing" is two characters of evidence. Too little to
    override a deliberate one-word interruption."""
    s = h.sched
    await s.say("Nothing outstanding, sir.")
    await asyncio.sleep(0.1)
    await s.user_interim("no")
    assert not s.is_speaking


@pytest.mark.asyncio
async def test_two_novel_words_are_unaffected_by_the_stem_rule(h):
    """The general bar does not consult stems at all: a follow-up that merely
    shares roots with what he is saying must still cut him off."""
    s = h.sched
    await s.say("Deleting the staging files now, sir.")
    await asyncio.sleep(0.1)
    await s.user_interim("delete the file")
    assert not s.is_speaking


# --- the user's first word is not an echo ------------------------------------
#
# Live, 2026-09-04, on a room microphone rather than a headset:
#
#     21:51:05  User (echo, ignored): now
#     21:51:33  User (echo, ignored): run
#
# Each was the FIRST WORD of the user's reply, delivered by Chrome as a final
# on its own because the endpointer closed a segment when JARVIS's voice
# tailed off. Both words were in what he had just said ("nothing's running
# now", "that run finished"). One token, fully matching, zero novel: the
# classifier called it an echo and the start of the sentence was eaten. The
# discriminator is time -- a real echo arrives during playback; a person
# replies after it.

def _scheduler_with_clock():
    from speech import SpeechScheduler
    t = {"now": 1000.0}
    async def synth(text): return b"x"
    async def emit(msg): pass
    s = SpeechScheduler(synth, emit, clock=lambda: t["now"])
    return s, t


def _he_just_said(s, t, text, *, acked_ago):
    """Register `text` as a chunk the client finished playing `acked_ago`
    seconds before the clock's current reading."""
    from speech import _Spoken, _tokens
    s._recent.append(_Spoken(utt=1, idx=0, tokens=set(_tokens(text)),
                             sent_at=t["now"] - acked_ago - 1.0,
                             acked_at=t["now"] - acked_ago))


@pytest.mark.parametrize("word", ["now", "run", "yes", "sir"])
def test_the_users_first_word_after_a_reply_is_speech_not_echo(word):
    s, t = _scheduler_with_clock()
    _he_just_said(s, t, f"nothing is running {word} sir, that run finished", acked_ago=1.5)
    assert s.classify(word) == "speech", (
        f"{word!r} arrived 1.5s after his audio ended -- that is a person, "
        f"and dropping it eats the start of their sentence")


def test_a_short_echo_during_playback_is_still_an_echo():
    """The window must not open so wide that the speaker's own output gets
    through: a word heard 0.2s after its chunk was acked IS him."""
    s, t = _scheduler_with_clock()
    _he_just_said(s, t, "nothing is running now sir", acked_ago=0.2)
    assert s.classify("now") == "echo"


def test_a_whole_echoed_sentence_is_still_an_echo_for_the_full_window():
    """The relaxation is for one or two words only. A whole sentence of his
    coming back three seconds later is the room, not a person."""
    s, t = _scheduler_with_clock()
    _he_just_said(s, t, "nothing is running now sir that run finished", acked_ago=3.0)
    assert s.classify("nothing is running now sir") == "echo"


def test_two_words_after_the_gap_are_speech_but_three_matching_are_not():
    s, t = _scheduler_with_clock()
    _he_just_said(s, t, "that run finished four minutes ago", acked_ago=1.5)
    assert s.classify("run finished") == "speech"
    assert s.classify("run finished four") == "echo"


# --- an unacked chunk is one that is PLAYING -------------------------------
#
# `_since_last_ack` returned `inf` when nothing in `_recent` was acked, and
# said so: "an unacked chunk cannot be echoing -- it has not been played."
# That is backwards. `frontend/src/voice.ts` sends the ack from
# `source.onended` — when a chunk FINISHES. So a chunk that is sent and
# unacked is a chunk coming out of the speaker right now, which is exactly
# when its echo is loudest, and `inf > SHORT_ECHO_GRACE_SEC` handed the
# relaxation to every short word heard mid-sentence.
#
# The class here is the STATE of `_recent`, enumerated rather than sampled:
# nothing sent, sent-and-unacked (playing), and acked (finished, N seconds
# ago). Each one has an answer and each answer is asserted.

def _he_is_saying(s, t, text, *, sent_ago=0.1):
    """Register `text` as a chunk that was SENT and has not been acked —
    what the scheduler holds while the client is still playing it."""
    from speech import _Spoken, _tokens
    s._recent.append(_Spoken(utt=1, idx=0, tokens=set(_tokens(text)),
                             sent_at=t["now"] - sent_ago, acked_at=None))


def test_a_word_heard_while_a_chunk_is_playing_is_an_echo():
    """The bug, executed against the real scheduler. `run` arrives one tenth
    of a second after JARVIS's chunk went out and before the client has said
    it finished — that is his own voice in the room, not a person."""
    s, t = _scheduler_with_clock()
    _he_is_saying(s, t, "the run finished four minutes ago")
    assert s.classify("run") == "echo"


def test_the_relaxation_is_off_for_the_whole_of_a_long_chunk():
    """A chunk takes seconds to play. Nothing acks it until it ends, so the
    grace must stay shut for all of that time, not just the first 0.75s."""
    s, t = _scheduler_with_clock()
    _he_is_saying(s, t, "nothing is running now sir", sent_ago=5.0)
    assert s.classify("now") == "echo"


def test_a_chunk_still_playing_shuts_the_grace_even_beside_an_older_ack():
    """One utterance, two chunks: the first has been acked a while ago, the
    second is playing. The newest ACK is old, but he is still talking."""
    s, t = _scheduler_with_clock()
    _he_just_said(s, t, "nothing is running", acked_ago=2.0)
    _he_is_saying(s, t, "now sir")
    assert s.classify("now") == "echo"


def test_a_cancel_word_wins_over_the_echo_rule_and_that_is_on_purpose():
    """`classify` answers "cancel" before it looks at `_recent`, so a cancel
    word echoed back inside the window still cancels. Pinned rather than
    fixed, with the argument in `speech.classify`: the window opens only
    after the read-back has finished PLAYING, it is two seconds long and
    deliberate, and the costs are not symmetric — a false cancel costs one
    repeated sentence, a missed one sends a message the user audibly tried
    to stop."""
    s, t = _scheduler_with_clock()
    _he_is_saying(s, t, "telling hammer stop the migration")
    assert s.classify("stop") == "echo"          # outside the window: his voice
    s._cancel_window_open = True
    assert s.classify("stop") == "cancel"        # inside it: stop anyway


def test_with_nothing_sent_at_all_a_word_is_the_user():
    """The empty case: `_recent` holds nothing, so there is no echo to be."""
    s, _t = _scheduler_with_clock()
    assert s.classify("now") == "speech"


# --- an ack for a chunk that was never sent is not an ack --------------------
#
# `played` took the client's word for the chunk index and never compared it
# with what had actually gone out. `played(utt, 9999)` against a live
# five-chunk utterance set `played = 9999`, which makes `done` True,
# `remaining_text()` empty and `wait_for()` return True -- and `wait_for` is
# what `_perform_steer`, `_perform_dialog` and the command path use as "the
# user has heard the read-back" before opening the cancel window. It also
# defeats the prefetch gate (`sent - played < prefetch`), so every remaining
# chunk goes out in one burst, and silently drops an URGENT utterance's
# remaining text from `_unread`.
#
# Reachable only from the page itself (the Origin gate holds), so this is a
# buggy or racing tab rather than an attacker. It is still not an ack.

@pytest.mark.asyncio
async def test_an_ack_beyond_what_was_sent_is_ignored(h, caplog):
    s = h.sched
    u = await s.say("Alpha one. Bravo two. Charlie three. Delta four. Echo five.")
    await asyncio.sleep(0.15)
    assert len(u.chunks) == 5 and u.played == -1
    assert u.sent == 1, "prefetch=2 holds everything past chunk 1"
    sent_before = len(h.audio())
    with caplog.at_level(logging.WARNING, logger="jarvis.speech"):
        await s.played(u.id, 9999)
    await asyncio.sleep(0.1)
    assert u.played == -1, "nothing has been played; a forged index must not say otherwise"
    assert not u.done and u.remaining_text().startswith("Alpha one.")
    assert len(h.audio()) == sent_before, "the prefetch gate must still hold"
    assert not await s.wait_for(u, timeout=0.1), "the read-back gate must not open"
    assert any("beyond" in r.message for r in caplog.records)


# --- an ack cannot arrive before the audio could have finished -------------
#
# `high_sent` bounds WHICH chunk may be acked. It says nothing about WHEN,
# and the prefetch gate raises it on every ack: ack chunk 1, chunks 2-3 go
# out, ack 3, 4-5 go out, ack 5 — three messages, six chunks "played", zero
# heard. Only the page can send it (the Origin gate holds), and the honest
# page does send one early: a chunk that failed to decode is acked at once.
# So an early ack is HELD, not dropped, and applied when it could be true.

def _mp3(seconds: float, kbps: int = 128) -> bytes:
    """A constant-bitrate MPEG-1 Layer III blob of the given length: one
    real frame header (0xFFFB = sync, MPEG-1, Layer III, no CRC; 0x90 =
    128 kbps at 44.1 kHz) and the right number of bytes after it."""
    head = bytes([0xFF, 0xFB, {128: 0x90, 64: 0x50}[kbps], 0x00])
    return head + bytes(int(seconds * kbps * 1000 / 8) - 4)


def test_mp3_seconds_reads_the_frame_header_and_nothing_else():
    import speech
    assert abs(speech.mp3_seconds(_mp3(2.0)) - 2.0) < 0.01
    assert abs(speech.mp3_seconds(_mp3(0.5, kbps=64)) - 0.5) < 0.01
    assert speech.mp3_seconds(b"A:Alpha one.") == 0.0, "a fake has no floor"
    assert speech.mp3_seconds(b"") == 0.0 and speech.mp3_seconds(None) == 0.0
    tagged = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + bytes(10) + _mp3(1.0)
    assert abs(speech.mp3_seconds(tagged) - 1.0) < 0.01, "an ID3 tag is skipped"
    assert speech.ack_floor_seconds(_mp3(100.0)) == speech.ACK_FLOOR_MAX_SEC


@pytest.mark.asyncio
async def test_the_watermark_cannot_be_walked_faster_than_the_audio_plays(h):
    s = h.sched

    async def real_audio(text):
        return _mp3(0.3)
    s._synth = real_audio
    u = await s.say("Alpha one. Bravo two. Charlie three. Delta four. Echo five. Foxtrot six.")
    await asyncio.sleep(0.15)
    assert len(u.chunks) == 6 and u.sent == 1 and u.played == -1

    # The walk: ack whatever the bound allows, as fast as the socket carries it.
    for _ in range(3):
        await s.played(u.id, u.high_sent)
        await asyncio.sleep(0.02)
    assert u.played == -1, "nothing could have finished yet"
    assert u.sent == 1, "the prefetch gate did not move"
    assert len(h.audio()) == 2
    assert not u.done and s.is_speaking
    assert u.remaining_text().startswith("Alpha one.")
    assert not await s.wait_for(u, timeout=0.05), "the read-back gate stays shut"

    # ...and the held ack lands by itself once the chunk could have played.
    await asyncio.sleep(0.35)
    assert u.played == 1, "the early ack was held, not lost"
    assert u.sent == 3, "and it unlocked the next two chunks, as an ack does"


@pytest.mark.asyncio
async def test_a_failed_synthesis_does_not_break_the_chain(h):
    """The sixth audit: a chunk with no audio took the text branch, marked
    itself played at once and left `earliest_ack` at 0.0, so the NEXT chunk's
    chain restarted at `now` — and with the failed chunk last, `wait_for`
    said "heard in full" 22 ms into ten seconds of audio, with no ack from
    the page at all."""
    s = h.sched

    async def flaky(text):
        return None if text.startswith("Bravo") else _mp3(0.4)
    s._synth = flaky
    u = await s.say("Alpha one. Bravo two.")
    assert not await s.wait_for(u, timeout=0.15), "chunk 0 is still playing"
    assert u.played == -1 and u.held_ack == 1
    assert await _until(lambda: u.played == 1 and u.done),         "and it finishes on the audio's schedule"

    u = await s.say("Alpha one. Bravo two. Charlie three.")
    assert await _until(lambda: u.sent == 1 and u.held_ack == 1),         "the failed chunk waits its turn"
    # chunk 0's floor lands, and only then does chunk 2 go out
    assert await _until(lambda: u.sent == 2)
    assert u.chunks[2].earliest_ack >= u.chunks[0].earliest_ack + 0.3, \
        "chunk 2 cannot end before chunk 0 could, whatever happened to chunk 1"


@pytest.mark.asyncio
async def test_a_hold_taken_while_paused_is_not_discharged_against_the_bridge():
    """The seventh audit: `held_ack` is an INDEX, and the resume bridge is
    inserted under it. A preemption clears the hold, but an early ack that
    arrives WHILE PAUSED sets it again, and the bridge then lands on that
    index with `earliest_ack` 0.0 — so the hold discharged at once, against
    audio that had not been sent."""
    from speech import SpeechScheduler, Priority
    msgs = []

    async def synth(text):
        return _mp3(0.05) if text.startswith("Urgent") else _mp3(0.4)

    async def emit(msg):
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, stale_after=5.0)
    await s.start()
    try:
        u = await s.say("Alpha bravo. Charlie delta. Foxtrot golf.")
        await asyncio.sleep(0.1)
        assert u.sent == 1 and u.played == -1
        v = await s.say("Urgent line.", priority=Priority.URGENT, immediate=True)
        await asyncio.sleep(0.1)
        assert s._paused is u, "the preemption happened"
        await s.played(u.id, 0)                 # early: chunk 0 has 0.36s of floor
        assert u.held_ack == 0 and u.played == -1
        await s.played(v.id, 0)                 # the urgent line finished (honestly)
        await asyncio.sleep(0.1)
        assert u.resumes == 0, "the resume waits while chunk 0 can still be playing"
        await asyncio.sleep(0.4)                # the hold lands on chunk 0's floor
        assert u.played == 0 and u.held_ack == -1
        assert u.resumes == 1
        assert u.chunks[0].text == "Alpha bravo." and u.chunks[1].text != "Charlie delta.", \
            "the bridge went in AFTER the chunk that was heard, never under the hold"
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_a_resume_waits_for_the_kept_chunk_before_shifting_its_index():
    """The eighth audit: `held_ack` was only one way for a stale index to
    arrive. An URGENT whose synthesis FAILS is done at once, so the resume
    happened while the browser was still playing the kept chunk — and its
    honest ack, for the index it was sent under, landed on the bridge that
    had just been inserted under that index. The resume now waits for that
    ack (bounded by the chunk's own floor plus a grace)."""
    from speech import SpeechScheduler, Priority
    msgs = []

    async def synth(text):
        return None if text.startswith("Urgent") else _mp3(0.4)

    async def emit(msg):
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, stale_after=5.0)
    await s.start()
    try:
        u = await s.say("Alpha bravo. Charlie delta. Foxtrot golf.")
        await asyncio.sleep(0.1)
        assert u.sent == 1 and u.played == -1
        await s.say("Urgent line.", priority=Priority.URGENT, immediate=True)
        await asyncio.sleep(0.15)          # the urgent failed to synthesise: done at once
        assert s._paused is u and u.resumes == 0, "no bridge while chunk 0 can still be playing"
        assert u.chunks[0].text == "Alpha bravo."
        await s.played(u.id, 0)            # the kept chunk finishes, honestly, under ITS index
        await asyncio.sleep(0.4)
        assert u.played == 0 and u.resumes == 1
        assert u.chunks[0].text == "Alpha bravo." and u.chunks[1].text != "Charlie delta.", \
            "the bridge went in AFTER the chunk that was heard"
        assert u.chunks[2].text == "Charlie delta."
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_a_bridge_shifts_the_sent_bound_with_the_frames_it_displaces():
    from speech import SpeechScheduler, Priority

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        pass

    s = SpeechScheduler(synth, emit, pause_after=0.2, stale_after=5.0)
    await s.start()
    try:
        u = await s.say("Alpha bravo. Charlie delta. Foxtrot golf. Hotel india.")
        await asyncio.sleep(0.15)
        await s.played(u.id, 0)
        await asyncio.sleep(0.1)
        assert (u.sent, u.high_sent) == (2, 2)
        v = await s.say("Urgent line.", priority=Priority.URGENT, immediate=True)
        await asyncio.sleep(0.1)
        await s.played(v.id, 0)
        await asyncio.sleep(0.1)
        assert u.resumes == 1
        assert u.high_sent == 3, "frames 1 and 2 went out; they are now 2 and 3"
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_an_honest_ack_after_the_audio_played_is_immediate(h):
    s = h.sched

    async def real_audio(text):
        return _mp3(0.1)
    s._synth = real_audio
    u = await s.say("Alpha one. Bravo two. Charlie three.")
    await asyncio.sleep(0.15)     # longer than chunk 0's floor
    await s.played(u.id, 0)
    assert u.played == 0 and u.held_ack == -1


@pytest.mark.asyncio
async def test_a_decode_failure_ack_does_not_wedge_the_mouth(h):
    """`voice.ts` acks a chunk it could not decode straight away. That ack
    is early and honest; holding it must cost at most the chunk's own
    length, never `stale_after`."""
    s = h.sched

    async def real_audio(text):
        return _mp3(0.2)
    s._synth = real_audio
    u = await s.say("Alpha one. Bravo two. Charlie three.")
    await asyncio.sleep(0.15)
    await s.played(u.id, 0)                   # "failed to decode", at once
    assert u.played == -1 and u.held_ack == 0
    await asyncio.sleep(0.25)
    assert u.played == 0 and u.sent == 2
    assert not u.cancelled and not u.abandoned


@pytest.mark.asyncio
async def test_an_ack_of_the_last_sent_chunk_is_still_an_ack(h):
    """The bound is `sent`, not something tighter: the newest chunk that has
    actually gone out is exactly the one the client is playing."""
    s = h.sched
    u = await s.say("Alpha one. Bravo two. Charlie three.")
    await asyncio.sleep(0.15)
    assert u.sent == 1
    await s.played(u.id, 1)
    assert u.played == 1
    assert all(sp.acked_at is not None for sp in s._recent)


@pytest.mark.asyncio
async def test_out_of_order_and_duplicate_acks_are_still_ignored(h):
    """Unchanged behaviour, pinned so the new bound cannot regress it."""
    s = h.sched
    u = await s.say("Alpha one. Bravo two. Charlie three.")
    await asyncio.sleep(0.15)
    await s.played(u.id, 1)
    await s.played(u.id, 0)          # late, lower
    await s.played(u.id, -5)         # nonsense, lower
    await s.played(u.id, 1)          # duplicate
    assert u.played == 1


# --- the fourth state: sent, unacked, and STALE ------------------------------
#
# The echo tests enumerated "nothing sent", "sent and playing" and "acked N
# seconds ago". They never covered a chunk that was sent, will never be
# acked, and is long past any possible playback -- which is exactly what the
# dropped-preemption path deliberately creates.

def test_a_sent_chunk_stops_being_echo_relevant_after_the_backstop():
    """`RECENT_BACKSTOP_SEC` had no test at all: raising it to 1e9 left the
    whole suite green. It is the last-resort bound on how long an unacked
    chunk can be believed to be playing, and here it is asserted."""
    import speech
    assert speech.RECENT_BACKSTOP_SEC == 120.0, (
        "the number is the assertion, so the seconds below are written out "
        "rather than derived from it -- a test that jumps "
        "`RECENT_BACKSTOP_SEC + 1` seconds passes for any value at all")
    s, t = _scheduler_with_clock()
    _he_is_saying(s, t, "nothing is running now sir")
    assert s.classify("now") == "echo"
    t["now"] += 119
    assert s.classify("now") == "echo", "still inside the backstop"
    t["now"] += 2
    assert s.classify("now") == "speech", "past the backstop: it cannot still be playing"
    assert s._recent == []


def test_an_orphaned_chunk_expires_on_the_watchdog_timescale_not_the_backstop():
    """The scheduler's own answer to "nobody is going to ack this" is
    `ack_timeout`. A chunk it has stopped tracking must not outlive that by
    75 seconds just because `_recent`'s backstop is the only thing left
    watching it."""
    from speech import SpeechScheduler
    t = {"now": 1000.0}

    async def synth(text):
        return b"x"

    async def emit(msg):
        pass

    s = SpeechScheduler(synth, emit, ack_timeout=45.0, clock=lambda: t["now"])
    _he_is_saying(s, t, "charlie delta echo")
    s._orphan(1)
    assert s.classify("charlie delta echo") == "echo", "it may still be playing"
    t["now"] += 44
    assert s.classify("charlie delta echo") == "echo"
    t["now"] += 2
    assert s.classify("charlie delta echo") == "speech"


@pytest.mark.asyncio
async def test_a_dropped_preempted_chunk_does_not_poison_echo_for_two_minutes():
    """The bug of `64a7680` on a different path. An URGENT preempts, the
    preempted utterance goes stale and is dropped -- and `_settle(above =
    played + 1)` deliberately leaves the chunk that "may still be playing"
    unacked. That `_Spoken` belongs to an utterance that is neither
    `_current` nor `_paused`, so the 45s ack watchdog never sees it; only
    `RECENT_BACKSTOP_SEC` did, 120 seconds later. Driven live, `classify`
    answered "echo" for 119 of them."""
    from speech import SpeechScheduler, Priority
    msgs = []
    skew = {"d": 0.0}

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, stale_after=0.05,
                        echo_window=0.5, ack_timeout=5.0,
                        clock=lambda: time.monotonic() + skew["d"])
    await s.start()
    try:
        u = await s.say("Alpha bravo. Charlie delta echo. Foxtrot golf. Hotel india.")
        await asyncio.sleep(0.15)
        await s.played(u.id, 0)                 # chunk 0 is the only one ever acked
        await asyncio.sleep(0.1)
        assert u.sent == 2
        urgent = await s.say("Urgent line.", priority=Priority.URGENT, immediate=True)
        await asyncio.sleep(0.15)
        assert any(m["type"] == "drop_queued" for m in msgs), "the preemption must have happened"
        await s.played(urgent.id, 0)
        await asyncio.sleep(0.25)               # past stale_after: the paused one is dropped
        assert u.cancelled and s._paused is None and s._current is None
        orphan = [sp for sp in s._recent if sp.utt == u.id and sp.acked_at is None]
        assert [sp.idx for sp in orphan] == [1], "chunk 1 was left unacked on purpose"
        assert s.classify("charlie delta echo") == "echo", "it may genuinely still be playing"
        skew["d"] += s.ack_timeout + 1          # long past playback, nowhere near 120s
        assert s.classify("charlie delta echo") == "speech"
        assert s._nothing_in_flight()
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_the_last_listener_leaving_settles_everything_in_flight():
    """`SpeechScheduler` is process-global and outlives any one tab. With no
    voice client there is no speaker for an echo to come from, so every
    unacked chunk is settled at once -- otherwise a reloaded tab inherits the
    previous tab's unacked entries and hears itself for two minutes."""
    from speech import SpeechScheduler
    t = {"now": 1000.0}

    async def synth(text):
        return b"x"

    async def emit(msg):
        pass

    s = SpeechScheduler(synth, emit, echo_window=6.0, clock=lambda: t["now"])
    _he_is_saying(s, t, "charlie delta echo", sent_ago=0.5)
    assert not s._nothing_in_flight()
    s.transport_gone()
    assert s._nothing_in_flight(), "nothing is coming out of a speaker any more"
    assert s.classify("charlie delta echo") == "echo", "the tail is still within echo_window"
    t["now"] += s.echo_window + 1
    assert s.classify("charlie delta echo") == "speech"


@pytest.mark.asyncio
async def test_an_ack_in_flight_across_a_preemption_still_counts():
    """The bound cannot be `sent`. A preemption rolls `sent` BACKWARDS — the
    client is told to drop everything past the chunk it is playing — and a
    resume bridge rolls it back again. An ack already in the air for a chunk
    that really did go out is not a forgery, and rejecting it makes the
    scheduler resend a chunk the user has already heard."""
    from speech import SpeechScheduler, Priority
    msgs = []

    async def synth(text):
        return b"A:" + text.encode()

    async def emit(msg):
        msgs.append(msg)

    s = SpeechScheduler(synth, emit, pause_after=0.2, stale_after=5.0)
    await s.start()
    try:
        u = await s.say("Alpha bravo. Charlie delta. Foxtrot golf. Hotel india.")
        await asyncio.sleep(0.15)
        await s.played(u.id, 0)
        await asyncio.sleep(0.1)
        assert (u.sent, u.high_sent) == (2, 2)
        await s.say("Urgent line.", priority=Priority.URGENT, immediate=True)
        await asyncio.sleep(0.15)
        assert u.sent == 1 and u.high_sent == 2, "the preemption rolled `sent` back"
        await s.played(u.id, 2)                  # was in flight when the drop went out
        assert u.played == 2, "chunk 2 really was sent; that ack is genuine"
    finally:
        await s.stop()


# ── his own voice, garbled ──────────────────────────────────────────────────
# The echo rule matches TOKENS against what he just said. Over a speaker the
# recogniser rarely returns his words intact — it returns something near them,
# and near is not equal, so the mis-hear reads as the user talking. Observed
# live: "Found it — chitauri is idle on ..." came back as
# 'found it guitar an' and cut him off with two chunks still unplayed.

@pytest.mark.asyncio
async def test_a_garbled_mis_hear_does_not_cut_him_off_mid_sentence(h):
    s = h.sched
    await s.say("Found it — chitauri is idle on project status "
                "and deployment planning. Shall I tell it?")
    await asyncio.sleep(0.1)
    assert s.is_speaking

    await s.user_interim("found it guitar an")

    assert s.is_speaking, "his own voice, garbled, must not interrupt him"


@pytest.mark.asyncio
async def test_a_cancel_word_still_interrupts_while_he_is_audible(h):
    """The bar rises for garbled echo, not for the user."""
    s = h.sched
    await s.say("Found it — chitauri is idle on project status.")
    await asyncio.sleep(0.1)
    await s.user_interim("stop")
    assert not s.is_speaking


@pytest.mark.asyncio
async def test_a_real_instruction_still_interrupts_while_he_is_audible(h):
    """Long enough that garbling is not a plausible explanation."""
    s = h.sched
    await s.say("Found it — chitauri is idle on project status.")
    await asyncio.sleep(0.1)
    await s.user_interim("actually cancel that and check the other one")
    assert not s.is_speaking


@pytest.mark.asyncio
async def test_the_quiet_room_bar_returns_once_his_audio_has_ended(h):
    """Two new words is right in the gap after he stops — that is the user
    speaking into silence, not his voice coming back."""
    s = h.sched
    await s.say("Found it.")
    await h.ack_all()
    await asyncio.sleep(0.05)
    u = s.begin_turn()
    s.feed(u, "Shall I tell it?")
    await s.end_turn(u)
    await asyncio.sleep(0.05)
    await h.ack_all()
    await asyncio.sleep(0.05)
    assert not s.is_speaking


# --- his voice, off ---------------------------------------------------------
#
# A second toggle beside the microphone's. While it is off nothing is
# synthesized — no Fish call, no audio bytes — and every sentence goes out
# through the text path the page renders in the Conversation panel. The
# text path already existed for a FAILED synthesis; a deliberate silence
# must not look like one.

@pytest.mark.asyncio
async def test_voice_is_on_until_told_otherwise(h):
    assert h.sched.voice_on is True
    h.sched.set_voice(False)
    assert h.sched.voice_on is False


@pytest.mark.asyncio
async def test_voice_off_sends_text_and_never_calls_tts(h):
    s = h.sched
    s.set_voice(False)
    u = s.begin_turn()
    s.feed(u, "First one. Second one.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    assert h.synth_calls == []
    assert h.audio() == []
    assert [m["text"] for m in h.msgs if m["type"] == "text"] == ["First one.", "Second one."]
    assert u.done


@pytest.mark.asyncio
async def test_voice_off_is_not_a_failing_voice(h):
    s = h.sched
    s.set_voice(False)
    u = s.begin_turn()
    s.feed(u, "One. Two. Three. Four.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    assert {"type": "text", "text": "My voice is failing, sir."} not in h.msgs
    assert "speaking" not in [m.get("state") for m in h.msgs if m["type"] == "status"]
    assert s._tts_failures == 0


@pytest.mark.asyncio
async def test_voice_off_leaves_the_echo_window_alone(h):
    """With the mic still on, a reply he never said aloud was never in the
    room. Counting it as spoken would make his own text reply swallow the
    user's next sentence as an echo of it."""
    s = h.sched
    s.set_voice(False)
    u = s.begin_turn()
    s.feed(u, "It uses WordPress, sir.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    assert s._recent == []
    assert await s.user_final("it uses wordpress sir") != "echo"


@pytest.mark.asyncio
async def test_voice_off_completes_at_once(h):
    """No audio, so nothing to pace: a text reply is over when it is sent."""
    s = h.sched
    s.set_voice(False)
    u = s.begin_turn()
    s.feed(u, "A long sentence that would take a good while to play aloud, sir.")
    await s.end_turn(u)
    assert await s.wait_for(u, timeout=1.0)


@pytest.mark.asyncio
async def test_turning_voice_off_mid_reply_sends_the_rest_as_text(h):
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "First one. ")
    await asyncio.sleep(0.1)                 # synthesized and sent as audio
    s.set_voice(False)
    s.feed(u, "Second one. Third one.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    await h.ack_all()
    assert [t for _, _, t in h.audio()] == ["First one."]
    assert [m["text"] for m in h.msgs if m["type"] == "text"] == ["Second one.", "Third one."]
    assert u.done


@pytest.mark.asyncio
async def test_a_chunk_already_synthesized_goes_as_text_once_voice_is_off(h):
    s = h.sched
    u = s.begin_turn()
    s.feed(u, "One. Two. Three.")
    await s.end_turn(u)
    await asyncio.sleep(0.1)                 # all three synthesized; two sent (prefetch)
    assert len(h.audio()) == 2
    s.set_voice(False)
    await h.ack_all()
    assert [t for _, _, t in h.audio()] == ["One.", "Two."]
    assert {"type": "text", "text": "Three."} in h.msgs
    assert u.done


@pytest.mark.asyncio
async def test_turning_voice_on_mid_reply_speaks_the_rest(h):
    s = h.sched
    s.set_voice(False)
    u = s.begin_turn()
    s.feed(u, "First one. ")
    await asyncio.sleep(0.05)
    s.set_voice(True)
    s.feed(u, "Second one.")
    await s.end_turn(u)
    await asyncio.sleep(0.15)
    await h.ack_all()
    assert "First one." in [m["text"] for m in h.msgs if m["type"] == "text"]
    assert [t for _, _, t in h.audio()] == ["Second one."]
    assert u.done


@pytest.mark.asyncio
async def test_the_first_chunk_records_when_it_was_cut_and_when_it_was_ready(h):
    """What the server's latency line decomposes: the brain's text arrives,
    the first chunk is cut from it, synthesis makes it ready, it is sent.
    Each of those is a timestamp on the utterance, in that order, and the
    second chunk never overwrites the first's."""
    u = h.sched.begin_turn()
    assert u.first_cut_at is None and u.first_ready_at is None
    h.sched.feed(u, "First one. Second one.")
    await h.sched.end_turn(u)
    assert await _until(lambda: u.first_sent_at is not None)
    assert u.first_cut_at is not None and u.first_ready_at is not None
    assert u.created <= u.first_cut_at <= u.first_ready_at <= u.first_sent_at, \
        (u.created, u.first_cut_at, u.first_ready_at, u.first_sent_at)
    first = (u.first_cut_at, u.first_ready_at, u.first_sent_at)
    await h.ack_all()
    assert u.done
    assert (u.first_cut_at, u.first_ready_at, u.first_sent_at) == first


def test_the_first_chunk_is_capped_at_eighty_characters_by_default():
    """Measured 2026-09-24: Fish returns its first byte after about half a
    second and the whole chunk after that plus roughly a third of the
    audio's length, so a 100-character first sentence costs 2.3 s of
    synthesis before anything is heard, and a 60-character one 1.4 s. The
    first chunk is therefore cut at its last clause break once it passes
    80 characters (it was 160), while the sentence is still streaming; the
    rest of the sentence is synthesized in parallel and is ready long
    before the first part has finished playing. Later chunks are whole
    sentences as before."""
    from speech import SentenceSplitter
    s = SentenceSplitter()
    sentence = ("I can watch your Claude Code sessions, spawn runs or full builds, "
                "and read or open your projects, sir. ")
    out = []
    for i in range(0, len(sentence), 10):          # streamed, ten characters at a time
        out += s.feed(sentence[i:i + 10])
    assert out[0] == "I can watch your Claude Code sessions, spawn runs or full builds,", out
    assert out[1] == "and read or open your projects, sir.", out
    assert s.feed("A short second sentence, with a comma. ") == ["A short second sentence, with a comma."]


def test_the_cap_applies_to_a_first_sentence_that_arrives_whole():
    """The hold in front of the mouth (server._OneLinePerTurn) releases the
    opening 0.6 s of a reply in ONE piece, so the first sentence usually
    arrives complete. Measured: with the cap applied only to a sentence still
    streaming, a 100-character opener went to Fish whole and cost 2.2-3.4 s
    before anything was heard. The cap holds either way: past 80 characters
    the first chunk is cut at the last clause break past 40, so the second
    chunk (synthesized in parallel) is ready before the first has played."""
    from speech import SentenceSplitter
    sentence = ("I can watch your Claude Code sessions, spawn runs or full builds, "
                "and read or open your projects, sir. ")
    assert SentenceSplitter().feed(sentence) == [
        "I can watch your Claude Code sessions, spawn runs or full builds,",
        "and read or open your projects, sir."]
    # No clause break past 40 characters: the sentence goes whole rather
    # than as a scrap the next chunk could not keep up with.
    scrap = "Yes, sir, the build is running and two of the four tasks are done with nothing waiting on you. "
    assert SentenceSplitter().feed(scrap) == [scrap.strip()]
    # A short first sentence is untouched, and later ones are never cut.
    assert SentenceSplitter().feed("Evening, sir. " + sentence) == [
        "Evening, sir.",
        "I can watch your Claude Code sessions, spawn runs or full builds, and read or open your projects, sir."]


# Measured live, 2026-09-30: "Read connector tools' own names on the Claude
# path has stopped and wants you, sir." is 82 characters, so it was cut at its
# last comma, and "sir." was synthesized and played on its own and shown in
# the chat panel as a message of its own. A cut is worth the tail's synthesis
# time and no more — about a quarter second per 20 characters — so a first
# chunk is not cut where what is left of its sentence is shorter than that.
SCRAP_LINE = "Read connector tools' own names on the Claude path has stopped and wants you, sir."


def _all(s, text, final=False):
    out = s.feed(text, final=final) if final else s.feed(text)
    tail = s.flush()
    return out + ([tail] if tail else [])


def test_a_whole_line_is_not_cut_to_leave_a_scrap():
    """`SpeechScheduler.say` hands a line over whole with nothing after it:
    the splitter is told it is final, so the end of the line is the end of
    its sentence, and a cut leaving "sir." behind is not made."""
    from speech import SentenceSplitter
    assert len(SCRAP_LINE) > 80
    assert _all(SentenceSplitter(), SCRAP_LINE, final=True) == [SCRAP_LINE]


def test_a_complete_first_sentence_is_not_cut_to_leave_a_scrap():
    from speech import SentenceSplitter
    assert SentenceSplitter().feed(SCRAP_LINE + " Shall I look? ") == [
        SCRAP_LINE, "Shall I look?"]


def test_a_whole_line_with_a_long_tail_is_still_cut_early():
    """The cap is kept wherever it buys something: the tail here is long
    enough to be ready before the first part has played."""
    from speech import SentenceSplitter
    line = ("I can watch your Claude Code sessions, spawn runs or full builds, "
            "and read or open your projects, sir.")
    assert _all(SentenceSplitter(), line, final=True) == [
        "I can watch your Claude Code sessions, spawn runs or full builds,",
        "and read or open your projects, sir."]
    dash = "I can manage your code sessions and answer questions, sir — just tell me what you need."
    assert _all(SentenceSplitter(), dash, final=True) == [
        "I can manage your code sessions and answer questions, sir —",
        "just tell me what you need."]


def test_a_strong_break_is_not_taken_to_leave_a_scrap_either():
    from speech import SentenceSplitter
    line = "The thread “Read connector tools' own names” is waiting on input, sir — go on."
    assert _all(SentenceSplitter(), line, final=True) == [line]


@pytest.mark.asyncio
async def test_an_announcement_is_one_chunk_not_a_line_and_a_scrap(h):
    """The live line, through the mouth: one synthesis, one chunk — so one
    message in the chat panel, which records each chunk it is sent."""
    await h.sched.say(SCRAP_LINE)
    assert await _until(lambda: h.audio())
    await h.ack_all()
    assert [t for _, _, t in h.audio()] == [SCRAP_LINE]
    assert h.synth_calls == [SCRAP_LINE]
