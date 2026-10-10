"""
speech.py — the mouth. A sentence splitter for streamed text and a scheduler
that owns every utterance JARVIS makes (see spec §4).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Awaitable, Callable, Optional

log = logging.getLogger("jarvis.speech")

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "inc", "ltd",
    "co", "no", "approx", "fig", "dept", "e.g", "i.e", "a.m", "p.m", "u.s", "u.k",
}
_CLOSERS = '"\')]'
_CLAUSE_BREAKS = (", ", "; ", " — ", " – ", ": ")
_STRONG_BREAKS = (" — ", " – ", "; ", ": ")   # a pause a listener already expects


def _word_before(text: str, i: int) -> str:
    j = i
    while j > 0 and not text[j - 1].isspace():
        j -= 1
    return text[j:i]


class SentenceSplitter:
    def __init__(self, first_chunk_max: int = 80, first_chunk_min: int = 40,
                 last_chunk_min: int = 20):
        self._buf = ""
        self.emitted = 0
        self._first_chunk_max = first_chunk_max
        self._first_chunk_min = first_chunk_min
        self._last_chunk_min = last_chunk_min

    def feed(self, delta: str, final: bool = False) -> list[str]:
        """The chunks `delta` completes. `final`: nothing follows it — the
        whole line was handed over at once (`SpeechScheduler.say`) — so the
        end of the buffer is the end of its last sentence."""
        self._buf += delta
        out: list[str] = []
        if self.emitted == 0:
            # A first sentence that is already complete and longer than
            # `first_chunk_max` is cut at its last clause break past
            # `first_chunk_min`, the same as one still streaming would be.
            # The hold in front of the mouth releases the opening of a reply
            # in one piece, so this is the common case, not the exception.
            end = self._boundary(self._buf)
            if end is None and final:
                end = len(self._buf.rstrip())
            if end is not None and end > self._first_chunk_max:
                cut = self._last_clause_break(self._buf[:self._first_chunk_max])
                if (cut is not None and cut >= self._first_chunk_min
                        and self._leaves_enough(cut, end)):
                    out.append(self._take(cut))
        while True:
            cut = self._boundary(self._buf)
            if cut is None:
                break
            out.append(self._take(cut))
        if self.emitted == 0:
            # Get the first audio out early: the moment a STRONG break (dash,
            # semicolon, colon) appears past `first_chunk_min`, cut there — it is
            # a pause the listener expects anyway. Past `first_chunk_max` with no
            # sentence end, fall back to the last break of any kind.
            #
            # `first_chunk_max` is 80, down from 160, on a measurement
            # (2026-09-24): Fish returns its first byte after ~0.5 s and the
            # whole chunk after that plus about a third of the audio's
            # length, so every 20 characters of first chunk is ~0.25 s of
            # silence before anything is heard. The rest of the sentence is
            # synthesized in parallel and is ready long before the first part
            # has finished playing, so the join at a comma costs nothing but
            # the comma's own pause.
            cut = self._first_strong_break(self._buf, self._first_chunk_min)
            if cut is None and len(self._buf) >= self._first_chunk_max:
                cut = self._last_clause_break(self._buf)
                if cut is not None and cut < self._first_chunk_min:
                    cut = None      # a scrap the next chunk could not keep up with
            end = len(self._buf.rstrip()) if final else None
            if cut and self._leaves_enough(cut, end):
                out.append(self._take(cut))
        return [c for c in out if c]

    def _leaves_enough(self, cut: int, end: Optional[int]) -> bool:
        """Whether cutting the first chunk at `cut` buys anything. What it
        buys is the synthesis time of what is left of the sentence, about a
        quarter second per 20 characters (see above), so a tail shorter than
        `last_chunk_min` is left on: measured live, 2026-09-30, "…has
        stopped and wants you," and then "sir." on its own, heard as two and
        shown in the chat panel as two messages. An end not yet known — a
        reply still streaming — is cut as before."""
        return end is None or end - cut >= self._last_chunk_min

    @staticmethod
    def _first_strong_break(text: str, min_pos: int) -> Optional[int]:
        best = None
        for sep in _STRONG_BREAKS:
            pos = text.find(sep, min_pos)
            if pos >= min_pos:
                end = pos + len(sep.rstrip())
                if best is None or end < best:
                    best = end
        return best

    def flush(self) -> Optional[str]:
        chunk = self._buf.strip()
        self._buf = ""
        if chunk:
            self.emitted += 1
            return chunk
        return None

    def _take(self, cut: int) -> str:
        chunk = self._buf[:cut].strip()
        self._buf = self._buf[cut:].lstrip()
        if chunk:
            self.emitted += 1
        return chunk

    def _boundary(self, text: str) -> Optional[int]:
        i, n = 0, len(text)
        while i < n:
            if text[i] in ".!?":
                j = i
                while j < n and text[j] in ".!?":
                    j += 1
                k = j
                while k < n and text[k] in _CLOSERS:
                    k += 1
                if k >= n:
                    return None                      # need to see what follows
                if not text[k].isspace():
                    i = k                            # "3.5", "server.py"
                    continue
                if text[i] == "." and j - i == 1:
                    word = _word_before(text, i)
                    if (word.lower() in _ABBREVIATIONS or (len(word) == 1 and word.isupper())
                            or word.isdigit()):      # "Mr.", initials "J.", list markers "1."
                        i = k
                        continue
                return k
            i += 1
        return None

    @staticmethod
    def _last_clause_break(text: str) -> Optional[int]:
        best = None
        for sep in _CLAUSE_BREAKS:
            pos = text.rfind(sep)
            if pos > 0:
                end = pos + len(sep.rstrip())
                best = end if best is None or end > best else best
        return best


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    URGENT = 2


BRIDGES = ("As I was saying —", "Back to it —", "Where was I —")
CANCEL_WORDS = ("wait", "no", "stop", "cancel", "hold on")
UNREAD_PREFIX = "Before I forget —"
# The last resort, and only that. An unacked chunk's tokens count as echo
# because the chunk may still be coming out of the speaker — and nothing plays
# for two minutes, so this number is not a claim about audio. It is the bound
# on a `_Spoken` that nothing else is watching, and after `_orphan` there
# should be no such entry on any live path: the ack watchdog settles the
# current utterance, `_orphan` expires a dropped preempted one, `barge_in`,
# `_abandon` and `transport_gone` settle the rest, all on `ack_timeout` or
# sooner. Twenty times the 6s an ACKED chunk gets is indefensible as a
# statement about playback; as a floor under a path nobody thought of, it is
# cheap. Lower it and the honest place to look is why anything reached it.
RECENT_BACKSTOP_SEC = 120.0      # a sent chunk is forgotten after this even if never acked
UTTERANCE_HISTORY = 64           # finished utterances kept for late acks
MAX_RESUMES = 3                  # an utterance interrupted more often than this is dropped

# An ack says a chunk FINISHED playing. It cannot have finished before the
# previous chunk finished plus its own length, so an ack that arrives
# earlier than that is held until the moment it could be true. Without the
# hold, `high_sent` was walkable: each ack unlocks a send, the send raises
# the bound, the next ack is "for a chunk that went out" — three messages
# marked six chunks played while none had, and `wait_for` opened a steer's
# cancel window over audio nobody had heard. The length comes from the mp3
# itself (`mp3_seconds`), scaled DOWN so it is a floor and never an estimate,
# and capped well under `ack_timeout` so a held ack can never look like a
# client that went away. A blob that is not mp3 (every test fake) has no
# floor at all — which is the old behaviour, exactly.
ACK_FLOOR_FACTOR = 0.9
ACK_FLOOR_MAX_SEC = 30.0
RESUME_ACK_GRACE_SEC = 1.0       # after a kept chunk's floor, how long a resume waits for its ack

_MP3_KBPS_V1 = {1: 32, 2: 40, 3: 48, 4: 56, 5: 64, 6: 80, 7: 96, 8: 112,
                9: 128, 10: 160, 11: 192, 12: 224, 13: 256, 14: 320}
_MP3_KBPS_V2 = {1: 8, 2: 16, 3: 24, 4: 32, 5: 40, 6: 48, 7: 56, 8: 64,
                9: 80, 10: 96, 11: 112, 12: 128, 13: 144, 14: 160}


def mp3_seconds(data: Optional[bytes]) -> float:
    """How long this MPEG Layer III audio plays, from its first frame header
    (constant bitrate, which is what Fish Audio emits). 0.0 for anything
    that is not one — no header, no floor."""
    if not data:
        return 0.0
    i = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        i = 10 + ((data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9])
    end = min(len(data), i + 4096)
    while i + 4 <= end:
        b1, b2, b3 = data[i], data[i + 1], data[i + 2]
        if b1 == 0xFF and (b2 & 0xE0) == 0xE0:
            version, layer = (b2 >> 3) & 0x3, (b2 >> 1) & 0x3
            kbps_index, rate_index = b3 >> 4, (b3 >> 2) & 0x3
            if (version != 1 and layer == 1 and 0 < kbps_index < 15
                    and rate_index != 3):
                table = _MP3_KBPS_V1 if version == 3 else _MP3_KBPS_V2
                return (len(data) - i) * 8 / (table[kbps_index] * 1000)
        i += 1
    return 0.0


def ack_floor_seconds(audio: Optional[bytes]) -> float:
    return min(mp3_seconds(audio) * ACK_FLOOR_FACTOR, ACK_FLOOR_MAX_SEC)

# A one- or two-word utterance whose every token JARVIS just said is only an
# echo if it arrived while that speech was still coming out of the speaker.
# Chrome's endpointer, on a room mic, closes a segment when his voice tails
# off and hands the user's FIRST word over as a final on its own: "now", "run"
# -- both words he had just used. Measured live (2026-09-04): those arrived
# 1-2s after his last chunk was acked, the ordinary human turn-taking gap. A
# true echo of a chunk lands within its own playback, so anything this long
# after the ack is a person, and dropping it eats the start of their sentence.
# Longer utterances keep the 6s window: a whole echoed sentence needs it.
SHORT_ECHO_GRACE_SEC = 0.75

# While his audio is audible, how much of a phrase may be made of HIS OWN
# words before it is treated as his voice coming back rather than the user's.
#
# The echo rule already catches his sentence returning intact. What it cannot
# catch is the recogniser mangling it: over a speaker it returns something
# near his words, and near is not equal, so a mis-hear reads as speech.
# Observed live: "Found it — chitauri is idle on ..." came back as
# 'found it guitar an', which cut him off with two chunks unplayed — the user
# never heard the end of his question and had to ask again.
#
# A count of new words cannot separate these: 'found it guitar an' carries two
# ("guitar", "an") and so does the genuine interruption "actually hold". The
# proportion can. Half that phrase is words he was saying at that moment;
# "actually hold" shares none of his, and "delete the file" over "Deleting the
# staging files" shares one word in three.
ECHO_SHARE_WHILE_AUDIBLE = 0.5
SHORT_UTTERANCE_TOKENS = 2

Synthesize = Callable[[str], Awaitable[Optional[bytes]]]
Emit = Callable[[dict], Awaitable[None]]

_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


# "Say that again" must be replayed, not answered — regenerating an answer
# costs a brain turn AND comes back as different words, which does not even
# answer what the user asked. So this is matched narrowly, on the tokenized
# text with a small set of filler words trimmed off BOTH ends only (never the
# middle, so a filler word embedded in a real sentence cannot accidentally
# collapse it into a trigger):
#   "say that again" / "say it again" / "repeat" / "repeat that" /
#   "repeat it" / "what did you say" / "what was that" / "come again" /
#   "one more time" / "pardon" / "pardon me"
# Deliberately NOT matched: anything that asks a real question, however
# related — "what did you mean by that", "what do you mean", "can you
# explain that again", "say more about that". Those go to the brain like any
# other turn.
_REPLAY_FILLER = frozenset({"sorry", "jarvis", "please", "um", "uh", "i'm"})
_REPLAY_TRIGGERS = frozenset({
    ("say", "that", "again"),
    ("say", "it", "again"),
    ("repeat",),
    ("repeat", "that"),
    ("repeat", "it"),
    ("what", "did", "you", "say"),
    ("what", "was", "that"),
    ("come", "again"),
    ("one", "more", "time"),
    ("pardon",),
    ("pardon", "me"),
})


def _is_replay_request(toks: list[str]) -> bool:
    i, j = 0, len(toks)
    while i < j and toks[i] in _REPLAY_FILLER:
        i += 1
    while j > i and toks[j - 1] in _REPLAY_FILLER:
        j -= 1
    return tuple(toks[i:j]) in _REPLAY_TRIGGERS


@dataclass
class Chunk:
    """One speakable piece of an utterance. Synthesis writes to the object, never
    to an index, so chunks can be inserted (the resume bridge) while synthesis
    for later chunks is still in flight."""
    text: str
    audio: Optional[bytes] = None
    ready: bool = False
    notice: bool = False        # this chunk's failure was the third in a row: warn after it
    silent: bool = False        # never synthesized ON PURPOSE (voice off): text, not a failure
    earliest_ack: float = 0.0   # no honest ack can arrive before this (see ACK_FLOOR_FACTOR)
    cut_at: float = 0.0         # when its text was cut from the stream
    ready_at: Optional[float] = None   # when synthesis finished (or was skipped)


@dataclass
class Utterance:
    id: int
    priority: Priority
    kind: str                                   # "turn" | "say" | "batched"
    created: float
    immediate: bool = True                      # may start while the user is talking
    chunks: list[Chunk] = field(default_factory=list)
    sent: int = -1                              # highest chunk index sent to the client
    high_sent: int = -1                         # highest index EVER sent; only ever grows.
                                                 # `sent` is rolled BACKWARDS by a preemption
                                                 # (the client keeps only the playing chunk)
                                                 # and by a resume bridge, so it is not a
                                                 # bound on what the client may legitimately
                                                 # ack — an ack already in flight for a chunk
                                                 # that really did go out is not a forgery
    played: int = -1                            # highest chunk index the client acked
    held_ack: int = -1                          # an ack that arrived before its chunk could
                                                 # have finished; applied by `_step` when it could
    closed: bool = False                        # no more chunks coming
    cancelled: bool = False
    abandoned: bool = False                     # set only by _abandon(): a transport
                                                 # failure, never a genuine user cancel
    paused_at: Optional[float] = None
    resumes: int = 0
    first_sent_at: Optional[float] = None
    # Where the time before the first audio went, for the server's latency
    # line: the first chunk was cut from the brain's text at `first_cut_at`
    # and its synthesis finished at `first_ready_at`; `first_sent_at` is
    # when its audio left. The second chunk never overwrites these.
    first_cut_at: Optional[float] = None
    first_ready_at: Optional[float] = None
    last_progress_at: Optional[float] = None    # last send or ack; the ack watchdog reads it
    alias: Optional["Utterance"] = None         # a batched LOW item points at the merged utterance
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)

    @property
    def done(self) -> bool:
        if self.alias is not None:
            return self.alias.done
        return self.cancelled or (self.closed and self.played >= len(self.chunks) - 1
                                  and self.sent >= len(self.chunks) - 1)

    @property
    def was_cancelled(self) -> bool:
        return (self.alias or self).cancelled

    @property
    def was_abandoned(self) -> bool:
        """True only for a transport failure (_abandon()), never a genuine
        cancel word or barge-in — those set `cancelled` directly without
        this flag. Distinguishes the two so a caller checking `was_cancelled`
        can tell a dead client from the user's own decision."""
        return (self.alias or self).abandoned

    def remaining_text(self) -> str:
        return " ".join(c.text for c in self.chunks[self.played + 1:])

    def texts(self) -> list[str]:
        return [c.text for c in self.chunks]


@dataclass
class _Spoken:
    utt: int
    idx: int
    tokens: set
    sent_at: float
    acked_at: Optional[float] = None
    expires_at: Optional[float] = None   # nobody will ever ack this; believe it
                                          # is playing only until here


class SpeechScheduler:
    """Owns the mouth. Everything JARVIS says goes through here (spec §4)."""

    def __init__(self, synthesize: Synthesize, emit: Emit, *,
                 prepare: Optional[Callable[[str], str]] = None, prefetch: int = 2,
                 batch_interval: float = 20.0, pause_after: float = 3.0,
                 stale_after: float = 60.0, echo_window: float = 6.0,
                 batch_settle: float = 2.0, ack_timeout: float = 45.0,
                 transport_ready: Optional[Callable[[], bool]] = None,
                 bridges: tuple = BRIDGES, cancel_words: tuple = CANCEL_WORDS,
                 clock: Callable[[], float] = time.monotonic):
        """`clock` drives scheduling decisions only; `wait_for` and
        `open_cancel_window` sleep in real time. `transport_ready` says whether
        anyone can hear us — proactive (unread/batched) speech waits for it."""
        self._clock = clock
        self.ack_timeout = ack_timeout             # a chunk unacked this long = the client is gone
        self._transport_ready = transport_ready or (lambda: True)
        self._synth = synthesize
        self._emit_raw = emit
        # One protocol frame at a time, in order. Not re-entrant: the transport
        # passed as `emit` must never call back into barge_in()/say() from
        # inside a send.
        self._emit_lock = asyncio.Lock()
        self.batch_settle = batch_settle       # LOW items collect this long before flushing
        self._prepare = prepare or (lambda s: s)
        self.prefetch = prefetch
        self.batch_interval = batch_interval
        self.pause_after = pause_after
        self.stale_after = stale_after
        self.echo_window = echo_window
        self.bridges = bridges
        self.cancel_words = tuple(cancel_words)
        self._next_id = 1
        self._current: Optional[Utterance] = None
        self._paused: Optional[Utterance] = None
        self._pending: list[Utterance] = []
        self._batch: list[Utterance] = []
        self._unread: list[str] = []
        self._recent: list[_Spoken] = []
        self._last_spoken: Optional[Utterance] = None   # for "say that again"
        self._last_user_speech: Optional[float] = None
        self._last_batch_flush = self._clock() - batch_interval   # the first flush needs no wait
        self._batch_since: Optional[float] = None
        self._cancel_window_open = False
        self._cancel_event = asyncio.Event()
        self._speaking = False
        # His voice, off or on — the speaker button beside the microphone's.
        # Off, nothing is synthesized and every sentence leaves as a `text`
        # frame; see `set_voice`.
        self.voice_on = True
        self._wake = asyncio.Event()
        self._tick_task: Optional[asyncio.Task] = None
        self._tasks: set[asyncio.Task] = set()
        self._tts_failures = 0
        self.utterances: dict[int, Utterance] = {}

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._tick_task is None:
            self._tick_task = asyncio.create_task(self._tick())

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

    async def _tick(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), 0.25)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._pump()
            except Exception as e:  # the mouth must never die
                log.exception(f"speech pump failed: {e}")

    def _kick(self) -> None:
        self._wake.set()

    def _kick_later(self, delay: float) -> None:
        """Wake the pump when a timed gate (batch settle, user pause) elapses."""
        try:
            asyncio.get_running_loop().call_later(max(0.0, delay) + 0.01, self._kick)
        except RuntimeError:
            pass

    async def _emit(self, msg: dict) -> None:
        async with self._emit_lock:
            await self._emit_raw(msg)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    # ── inputs ─────────────────────────────────────────────────────────
    def _new(self, priority: Priority, kind: str, immediate: bool) -> Utterance:
        u = Utterance(id=self._next_id, priority=priority, kind=kind,
                      created=self._clock(), immediate=immediate)
        self._next_id += 1
        self.utterances[u.id] = u
        self._evict_history()
        return u

    def _evict_history(self) -> None:
        if len(self.utterances) <= UTTERANCE_HISTORY:
            return
        for uid in list(self.utterances):
            u = self.utterances[uid]
            # `_last_spoken` is exempt: it is what "say that again" replays,
            # and eviction freeing its audio would silently turn a free,
            # instant replay into a re-synthesis (still correct, just no
            # longer free) with no bug and no signal that it happened.
            if (u.done and u is not self._current and u is not self._paused
                    and u is not self._last_spoken):
                for c in u.chunks:
                    c.audio = None            # free the synthesized bytes
                del self.utterances[uid]
            if len(self.utterances) <= UTTERANCE_HISTORY:
                break

    def begin_turn(self, priority: Priority = Priority.NORMAL) -> Utterance:
        """A streaming reply to the user's own words: starts immediately."""
        u = self._new(priority, "turn", immediate=True)
        self._pending.append(u)
        self._kick()
        return u

    def feed(self, utt: Utterance, delta: str) -> None:
        if utt.cancelled or utt.closed:
            return
        for text in utt.splitter.feed(delta):
            self._add_chunk(utt, text)

    async def end_turn(self, utt: Utterance) -> None:
        if not utt.cancelled:
            tail = utt.splitter.flush()
            if tail:
                self._add_chunk(utt, tail)
        utt.closed = True
        self._kick()

    def _fill(self, u: Utterance, text: str) -> None:
        for chunk in u.splitter.feed(text, final=True):
            self._add_chunk(u, chunk)
        tail = u.splitter.flush()
        if tail:
            self._add_chunk(u, tail)
        u.closed = True

    async def say(self, text: str, priority: Priority = Priority.NORMAL,
                  immediate: Optional[bool] = None) -> Utterance:
        """A complete utterance. NORMAL starts at once; LOW/URGENT wait for a pause
        unless `immediate` says otherwise. LOW is batched: the returned utterance
        becomes done when the merged announcement it joined has been spoken."""
        if priority == Priority.LOW and immediate is not True:
            u = self._new(priority, "batched", immediate=False)
            u.chunks.append(Chunk(text.strip()))
            u.closed = True
            self._batch.append(u)
            self._batch_since = self._batch_since or self._clock()
            self._kick_later(self.batch_settle)
            return u
        if immediate is None:
            immediate = priority == Priority.NORMAL
        u = self._new(priority, "say", immediate=immediate)
        self._fill(u, text)
        self._pending.append(u)
        self._kick()
        return u

    async def wait_for(self, utt: Utterance, timeout: float = 60.0) -> bool:
        """True when the utterance has been fully played; False on cancel/timeout."""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if utt.done:
                return not utt.was_cancelled
            await asyncio.sleep(0.02)
        return False

    async def open_cancel_window(self, seconds: float) -> bool:
        """Listen for a cancel word. True if the user cancelled."""
        self._cancel_event = asyncio.Event()
        self._cancel_window_open = True
        try:
            await asyncio.wait_for(self._cancel_event.wait(), seconds)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._cancel_window_open = False

    def classify(self, text: str, *, allow_cancel: bool = True, allow_replay: bool = True) -> str:
        """'echo' (JARVIS hearing himself), 'cancel', 'replay' ("say that
        again" — see `_is_replay_request`), or 'speech'."""
        toks = _tokens(text)
        if not toks:
            return "echo"
        # DELIBERATELY before `_recent` is consulted: a cancel word heard
        # while the window is open cancels, even if JARVIS himself has just
        # said it. Three reasons, and they are worth writing down because
        # this reads like the bug above and is not one.
        #
        # The window is not opened until the read-back has finished PLAYING
        # — `_perform_steer` and `_perform_dialog` both `wait_for(utt)`
        # first — so during the window there is nothing of his coming out of
        # the speaker at all. What could echo is the read-back that just
        # ended, and that only carries a cancel word if the message being
        # read back does ("Telling hammer: stop the migration").
        #
        # The window is short (STEER_CANCEL_WINDOW, two seconds) and it is
        # deliberate: the user has just been told what is about to happen and
        # is being given a moment to stop it.
        #
        # And the costs are not symmetric. A false cancel costs one repeated
        # sentence. A missed cancel sends a message, or presses a key in
        # somebody's terminal, that the user audibly tried to stop. In this
        # one window, failing toward NOT acting is the right way to be wrong.
        if allow_cancel and self._cancel_window_open and " ".join(toks) in self.cancel_words:
            return "cancel"
        if allow_replay and _is_replay_request(toks):
            return "replay"
        recent = self._recent_tokens()
        if recent:
            matching = sum(1 for t in toks if t in recent)
            if matching / len(toks) >= 0.7 and len(toks) - matching < 2:
                if (len(toks) <= SHORT_UTTERANCE_TOKENS
                        and self._since_last_ack() > SHORT_ECHO_GRACE_SEC):
                    return "speech"     # his word, but the user's mouth: see SHORT_ECHO_GRACE_SEC
                return "echo"
        return "speech"

    def seconds_since_last_played(self) -> float:
        """For the log, and for the log only: how long since the client
        finished playing anything. `inf` when nothing has been acked.

        Deliberately NOT `_since_last_ack`. The log line asks a factual
        question ("when did his audio last end?") and `inf` is the honest
        answer to it when nothing has ended yet. The classifier asks a
        different question — see below — and conflating the two is the bug
        this pair was split to fix.
        """
        acked = [s.acked_at for s in self._recent if s.acked_at is not None]
        if not acked:
            return float("inf")
        return self._clock() - max(acked)

    def _since_last_ack(self) -> float:
        """How long the room has been quiet, for the short-utterance grace.

        This used to return `inf` when nothing in `_recent` was acked, and
        said "an unacked chunk cannot be echoing -- it has not been played."
        That is backwards. `frontend/src/voice.ts` sends the ack from
        `source.onended`, i.e. when a chunk FINISHES playing. A chunk that is
        sent and unacked is therefore a chunk coming out of the speaker RIGHT
        NOW — which is exactly when its echo is loudest — and `inf` handed
        the relaxation to every short word heard mid-sentence. Driven against
        the real scheduler: mid-playback, `classify('run')` came back
        `speech`.

        So: while anything is sent and unacked, the room is not quiet, and
        the answer is 0.0 — the echo rule applies in full. The relaxation is
        only for the gap AFTER his audio has actually ended, which is the
        user's real bug (Chrome hands over his first word on its own, 1-2s
        after the last chunk was acked: see SHORT_ECHO_GRACE_SEC).

        `inf` survives for the one case that means it: nothing in `_recent`
        at all, so there is nothing that could be echoing.
        """
        if any(s.acked_at is None for s in self._recent):
            return 0.0
        return self.seconds_since_last_played()

    def _nonmatching(self, text: str) -> int:
        recent = self._recent_tokens()
        return sum(1 for t in _tokens(text) if t not in recent)

    async def user_interim(self, text: str) -> str:
        # Replay is decided only on the FINAL transcript (a partial "say
        # that" is not yet "say that again"); this also keeps the existing
        # interim barge-in behaviour — driven by `verdict == "speech"` —
        # unchanged for the phrase's early fragments.
        verdict = self.classify(text, allow_cancel=False, allow_replay=False)
        if verdict == "speech":
            self._last_user_speech = self._clock()
            self._kick_later(self.pause_after)
            if self._speaking and self._should_interrupt(text):
                await self.barge_in(reason=f"heard {text[:40]!r}")
        return verdict

    def _should_interrupt(self, text: str) -> bool:
        """Whether an interim heard mid-sentence should stop him talking.

        Two new words is the general bar, and it exists so a stray echo
        fragment cannot cut him off. But the words people actually interrupt
        with are ONE word — "stop", "wait", "no" — and they were failing that
        bar every time, which left no way to cut him off at all short of
        saying two novel words on purpose.

        A cancel word counts on its own, provided it is not simply him being
        heard back: `_nonmatching` >= 1 means he did not just say it himself.

        `_nonmatching` compares whole tokens, and ASR clips inflections. He
        says "Stopped the work in chitauri, sir." — server.py's own wording
        when a run is cancelled — the microphone returns "stop", "stop" is
        not literally among his words, and he barged in on himself. Same for
        "Cancelled the run" heard as "cancel" and "Waiting on the build"
        heard as "wait". So the one-word branch also asks whether the word is
        a STEM of something he is saying.
        """
        novel = self._nonmatching(text)
        # While his audio is actually coming out of the speaker, the room is
        # full of his voice and the recogniser garbles it into words he never
        # said. Observed live: "found it guitar an" cut him off mid-sentence
        # with two chunks unplayed. Token matching cannot catch that — a
        # mis-hear matches nothing he said, which is exactly what makes it
        # look like speech.
        #
        # So the bar rises while he is audibly speaking. Real interruptions
        # still get through: a cancel word on its own (below), and anything
        # long enough that garbling is an implausible explanation.
        # `_since_last_ack` is the classifier's question — "how long has the
        # room been quiet" — and it is 0.0 for exactly as long as a chunk is
        # sent and unacked, i.e. while his voice is audible.
        # `seconds_since_last_played` looks similar and is for the log: it
        # answers `inf` until something has been acked, which would leave this
        # guard switched off for the whole first utterance.
        if self._since_last_ack() == 0.0 and self._echo_share(text) >= ECHO_SHARE_WHILE_AUDIBLE:
            return False
        if novel >= 2:
            # The general bar is untouched, stems included: a real follow-up
            # that happens to share roots with what he is saying ("delete the
            # file" over "Deleting the staging files") must still cut him off.
            return True
        toks = _tokens(text)
        if " ".join(toks) not in self.cancel_words or novel < 1:
            return False
        return not self._is_stem_echo(toks)

    def _echo_share(self, text: str) -> float:
        """What proportion of these words are ones he is saying right now.

        Whole-token, like `_nonmatching`, and for the same reason: a stem rule
        here would swallow real follow-ups that share roots with him. This
        only has to separate a mangled echo (half his words or more) from an
        interruption that merely brushes against them.
        """
        toks = _tokens(text)
        if not toks:
            return 1.0                     # nothing said is not the user talking
        recent = self._recent_tokens()
        if not recent:
            return 0.0
        return sum(1 for t in toks if t in recent) / len(toks)

    def _is_stem_echo(self, toks: list) -> bool:
        """Is every one of these words just a clipped form of something he is
        currently saying?

        Deliberately narrow. Four characters is the minimum stem — "no"
        against "nothing" is two characters of evidence, far too little to
        override a deliberate one-word interruption — and the two words may
        differ by at most four characters, which covers the endings ASR
        actually drops (-s, -ed, -ing, -ping, -led) and not much else. Used
        ONLY by the one-word cancel branch above.
        """
        recent = self._recent_tokens()
        if not recent:
            return False
        return all(
            any(len(t) >= 4 and abs(len(r) - len(t)) <= 4
                and (r.startswith(t) or t.startswith(r))
                for r in recent)
            for t in toks)

    async def user_final(self, text: str, *, typed: bool = False) -> str:
        verdict = self.classify(text)
        if typed and verdict == "echo":
            verdict = "speech"
        if verdict == "echo":
            return verdict
        self._last_user_speech = self._clock()
        self._kick_later(self.pause_after)
        is_cancel_word = " ".join(_tokens(text)) in self.cancel_words
        if verdict == "cancel":
            self._cancel_event.set()
        if self._speaking and (typed or is_cancel_word or verdict == "replay" or self._nonmatching(text) >= 2):
            # A cancel — or a replay request — is an answer to what was being
            # said: it was heard, so it must not come back as "Before I
            # forget —". A single-word replay trigger ("repeat", "pardon")
            # would not otherwise clear the >=2 non-matching bar, so it is
            # named explicitly rather than relying on word count.
            await self.barge_in(keep_unread=(verdict != "cancel"),
                                reason=f"{verdict}: {text[:40]!r}")
        return verdict

    async def replay_last(self) -> bool:
        """"Say that again": resend the most recently completed utterance.

        Reuses its synthesized audio VERBATIM whenever every chunk still has
        it — no TTS call, no brain turn, effectively instant. That is chosen
        over asking the brain to answer again for two reasons: it is free,
        and it is exact, where a fresh brain turn would (as the user found
        live) come back as different words that do not even answer what was
        asked.

        If the audio is no longer held — a chunk's synthesis failed the
        first time, or the utterance aged out of `UTTERANCE_HISTORY` (it is
        exempted while it is `_last_spoken`, but a newer completed utterance
        displaces it from that role) — the same words are re-synthesized
        instead of silently doing nothing. Still no brain turn, and the
        words are still the ones JARVIS actually said, not a regeneration.

        Returns False when nothing has been said yet this session.
        """
        u = self._last_spoken
        if u is None or not u.chunks:
            return False
        texts = [c.text for c in u.chunks]
        if all(c.audio is not None for c in u.chunks):
            replay = self._new(Priority.NORMAL, "say", immediate=True)
            for c in u.chunks:
                replay.chunks.append(Chunk(c.text, audio=c.audio, ready=True))
            replay.closed = True
            self._pending.append(replay)
            self._kick()
        else:
            await self.say(" ".join(texts), priority=Priority.NORMAL, immediate=True)
        return True

    async def played(self, utt_id: int, idx: int) -> None:
        """The client finished playing chunk `idx` of utterance `utt_id`.

        An ack for a chunk that was never sent is not an ack. Nothing
        validated `idx` against `sent`, so `played(utt, 9999)` on a live
        five-chunk utterance made `done` True, `remaining_text()` empty and
        `wait_for()` return True — and `wait_for` is what `_perform_steer`,
        `_perform_dialog` and the command path use as "the user has heard the
        read-back" before opening the cancel window. It also defeated the
        prefetch gate (`sent - played < prefetch`), emptying the whole
        utterance into the socket at once, and dropped an URGENT utterance's
        remaining text from `_unread`.

        Only the page itself can send this (the Origin gate holds), so it is
        a buggy or racing tab rather than an attacker — which is why this
        drops the frame and logs it instead of closing the connection. An ack
        that is merely late or duplicated is still simply ignored.

        The bound is `high_sent`, not `sent`: a preemption and a resume
        bridge both roll `sent` backwards, and an ack already in the air for
        a chunk that really did go out must still count.
        """
        u = self.utterances.get(utt_id)
        now = self._clock()
        if u is not None and idx > u.high_sent:
            log.warning(f"speech: ignoring ack beyond what was sent "
                        f"(utterance {utt_id}, chunk {idx}, sent up to {u.high_sent})")
            return
        if u is not None and 0 <= idx < len(u.chunks) and now < u.chunks[idx].earliest_ack:
            # Too soon to be true (see ACK_FLOOR_FACTOR). Not dropped: the
            # honest client acks a chunk it FAILED TO DECODE at once, and a
            # dropped ack would wedge the prefetch gate for `stale_after`.
            # Held, and applied by `_step` the moment it could be honest.
            u.held_ack = max(u.held_ack, idx)
            u.last_progress_at = now                # the client is alive
            self._kick_later(u.chunks[idx].earliest_ack - now)
            return
        self._accept_ack(u, utt_id, idx, now)

    def _accept_ack(self, u: Optional[Utterance], utt_id: int, idx: int, now: float) -> None:
        if u is not None and idx > u.played:
            u.played = idx
            u.last_progress_at = now
        for s in self._recent:
            if s.utt == utt_id and s.idx <= idx and s.acked_at is None:
                s.acked_at = now
        self._kick()

    def _apply_held_acks(self, now: float) -> None:
        for u in (self._current, self._paused):
            if u is None or u.held_ack < 0:
                continue
            idx = u.held_ack
            if idx >= len(u.chunks) or now >= u.chunks[idx].earliest_ack:
                u.held_ack = -1
                self._accept_ack(u, u.id, idx, now)

    async def barge_in(self, keep_unread: bool = True, reason: str = "") -> None:
        """The user is talking: stop everything now."""
        cur = self._current
        # Barging in and resuming are the only things that make him say the
        # same sentence twice, and neither left any trace at all — which is
        # why "he repeated himself three times" could not be explained from
        # a log. Record what cut him off, and what was left unsaid.
        if cur is not None and not cur.done:
            log.info("speech: barge-in on utterance %s%s; %d chunk(s) unplayed",
                     cur.id, f" ({reason})" if reason else "",
                     max(0, len(cur.chunks) - cur.played - 1))
        cur = self._current
        # Flag first, emit second: a send loop suspended in an await sees the
        # flag before it can emit anything after our `stop`.
        if cur is not None:
            if keep_unread and cur.priority == Priority.URGENT and not cur.done:
                rest = cur.remaining_text()
                if rest:
                    self._unread.append(rest)
            cur.cancelled = True
            self._current = None
        if self._paused is not None:
            self._paused.cancelled = True
            self._paused = None
        for u in self._pending:
            if u.priority == Priority.NORMAL:
                u.cancelled = True
            else:
                u.immediate = False          # anything that survives waits for a pause
        self._pending = [u for u in self._pending if not u.cancelled]
        self._settle_in_flight()
        if self._cancel_window_open:
            self._cancel_event.set()
        await self._emit({"type": "stop"})
        await self._set_speaking(False)
        self._kick()

    def set_voice(self, on: bool) -> None:
        """His voice, off or on. Independent of the microphone.

        Off: `_synthesize` calls nothing and marks the chunk `silent`, and
        `_send_ready` sends it through the text path — the one that already
        existed for a FAILED synthesis, minus everything that made a failure
        a failure: no failure count (so no "My voice is failing, sir."), no
        speaking state, and no entry in the echo window, because a reply he
        never said aloud was never in the room and must not swallow the
        user's next sentence as an echo of it. A chunk that already has
        audio when the voice goes off is sent as text too.

        On again: sentences skipped while silent and not yet sent are
        synthesized after all, so the rest of a reply follows the switch.
        """
        on = bool(on)
        if on == self.voice_on:
            return
        self.voice_on = on
        log.info("speech: voice %s", "on" if on else "off (text only)")
        if on:
            live = [u for u in (self._current, self._paused, *self._pending) if u is not None]
            for u in live:
                for idx, chunk in enumerate(u.chunks):
                    if idx > u.sent and chunk.silent:
                        chunk.silent = False
                        chunk.ready = False
                        self._spawn(self._synthesize(u, chunk))
        self._kick()

    # ── internals ──────────────────────────────────────────────────────
    def _add_chunk(self, utt: Utterance, text: str) -> None:
        chunk = Chunk(text, cut_at=self._clock())
        utt.chunks.append(chunk)
        if utt.first_cut_at is None:
            utt.first_cut_at = chunk.cut_at
        self._spawn(self._synthesize(utt, chunk))

    def _mark_ready(self, utt: Utterance, chunk: Chunk) -> None:
        chunk.ready = True
        chunk.ready_at = self._clock()
        if utt.first_ready_at is None and utt.chunks and utt.chunks[0] is chunk:
            utt.first_ready_at = chunk.ready_at

    async def _synthesize(self, utt: Utterance, chunk: Chunk) -> None:
        if not self.voice_on:
            # Deliberate silence: no call to TTS, and marked so that delivery
            # knows this is not a synthesis that failed.
            chunk.silent = True
            self._mark_ready(utt, chunk)
            self._kick()
            return
        audio: Optional[bytes] = None
        try:
            audio = await self._synth(self._prepare(chunk.text))
        except Exception as e:
            log.error(f"synth failed: {e}")
        if utt.cancelled:
            return
        chunk.audio = audio
        self._mark_ready(utt, chunk)
        self._kick()

    def _user_silent(self, now: float) -> bool:
        return self._last_user_speech is None or now - self._last_user_speech >= self.pause_after

    def _startable(self, u: Utterance, now: float) -> bool:
        return not u.cancelled and (u.immediate or self._user_silent(now))

    def _settle(self, utt_id: int, *, above: int = -1) -> None:
        """Chunks the client will never play (dropped or cancelled) count as heard now."""
        now = self._clock()
        for s in self._recent:
            if s.utt == utt_id and s.idx > above and s.acked_at is None:
                s.acked_at = now

    def _orphan(self, utt_id: int) -> None:
        """This utterance is gone from the scheduler with a chunk still
        unacked: give that chunk a deadline of its own.

        The dropped-preemption path settles `above=played + 1` on purpose —
        the kept chunk may genuinely still be coming out of the speaker, so
        it must not be declared heard. But the `_Spoken` it leaves behind
        belongs to an utterance that is thereafter neither `_current` nor
        `_paused`, so the ack watchdog in `_step` never looks at it, and the
        only thing left watching was `RECENT_BACKSTOP_SEC` — 120 seconds of
        `classify` answering "echo" and eating the user's words. That is the
        bug of 64a7680 on a second path.

        `ack_timeout` is the scheduler's own existing answer to "nobody is
        going to ack this", so it is the answer here too: the same timescale
        as the watchdog rather than the backstop behind it.
        """
        deadline = self._clock() + self.ack_timeout
        for s in self._recent:
            if s.utt == utt_id and s.acked_at is None:
                s.expires_at = deadline if s.expires_at is None else min(s.expires_at, deadline)

    def transport_gone(self) -> None:
        """Nobody is listening any more: settle everything in flight.

        `SpeechScheduler` is process-global and outlives any one tab. With no
        voice client there is no speaker for an echo to come from, so an
        unacked chunk cannot be playing — and left alone, a reloaded tab
        would inherit the previous tab's unacked entries and be classified as
        echoing them. Settled exactly as `barge_in` settles: acked *now*, so
        the tail that was in the air stays echo-relevant for `echo_window`
        and no longer.
        """
        self._settle_in_flight()
        self._kick()

    def _settle_in_flight(self) -> None:
        now = self._clock()
        for s in self._recent:
            if s.acked_at is None:
                s.acked_at = now

    def _still_relevant(self, s: _Spoken, now: float) -> bool:
        """Could this chunk still be reaching the microphone? Three states:
        acked (it finished playing — `echo_window` of room reverb after
        that), orphaned (nobody will ever ack it, so `_orphan`'s deadline
        decides), and simply in flight (the backstop, which nothing should
        now reach — see RECENT_BACKSTOP_SEC)."""
        if s.acked_at is not None:
            return now - s.acked_at < self.echo_window
        if s.expires_at is not None and now >= s.expires_at:
            return False
        return now - s.sent_at < RECENT_BACKSTOP_SEC

    def _recent_tokens(self) -> set:
        now = self._clock()
        self._recent = [s for s in self._recent if self._still_relevant(s, now)]
        out: set = set()
        for s in self._recent:
            out |= s.tokens
        return out

    def _nothing_in_flight(self) -> bool:
        self._recent_tokens()                    # prune
        return all(s.acked_at is not None for s in self._recent)

    async def _set_speaking(self, on: bool) -> None:
        if on != self._speaking:
            self._speaking = on
            await self._emit({"type": "status", "state": "speaking" if on else "idle"})

    def _release_gated(self, now: float) -> None:
        if not self._user_silent(now) or not self._transport_ready():
            return
        if self._unread:
            text = f"{UNREAD_PREFIX} " + " ".join(self._unread)
            self._unread.clear()
            u = self._new(Priority.URGENT, "say", immediate=True)
            self._fill(u, text)
            self._pending.append(u)
        if (self._batch and self._current is None
                and now - self._last_batch_flush >= self.batch_interval
                and now - (self._batch_since or now) >= self.batch_settle):
            members = self._batch
            self._batch = []
            self._batch_since = None
            self._last_batch_flush = now
            u = self._new(Priority.LOW, "say", immediate=True)
            self._fill(u, " ".join(c.text for m in members for c in m.chunks))
            for m in members:
                m.alias = u
            self._pending.append(u)

    def _next_pending(self, now: float, min_priority: Priority = Priority.LOW) -> Optional[Utterance]:
        candidates = [u for u in self._pending if u.priority >= min_priority and self._startable(u, now)]
        if not candidates:
            return None
        candidates.sort(key=lambda u: (-int(u.priority), u.created))
        return candidates[0]

    async def _pump(self) -> None:
        for _ in range(16):
            if not await self._step():
                break

    async def _step(self) -> bool:
        now = self._clock()
        self._apply_held_acks(now)
        self._release_gated(now)
        self._pending = [u for u in self._pending if not u.cancelled]

        # URGENT preempts a lower-priority speaker at a chunk boundary.
        cur = self._current
        if cur is not None and self._paused is None and cur.priority < Priority.URGENT and not cur.done:
            urgent = self._next_pending(now, Priority.URGENT)
            if urgent is not None:
                cur.paused_at = now
                cur.sent = min(cur.sent, cur.played + 1)      # the client keeps only the playing chunk
                self._settle(cur.id, above=cur.played + 1)     # the dropped ones will never be acked
                cur.held_ack = -1                              # ...and neither will a held one for them
                await self._emit({"type": "drop_queued"})
                self._paused = cur
                self._pending.remove(urgent)
                self._current = urgent
                return True

        if self._current is None or self._current.done:
            if self._current is not None and self._current.done:
                finished = self._current
                self._current = None
                # What "say that again" replays: the last utterance that was
                # actually heard in full, never one that was interrupted (it
                # is `cancelled`, so `was_cancelled` is True) or lost to a
                # dead transport (`was_abandoned`).
                if not finished.was_cancelled and not finished.was_abandoned and finished.chunks:
                    self._last_spoken = finished
            if self._paused is not None:
                # Another URGENT waiting? Speak it before resuming, or the
                # resumed utterance would be preempted again and bridged twice.
                urgent = self._next_pending(now, Priority.URGENT)
                if urgent is not None:
                    self._pending.remove(urgent)
                    self._current = urgent
                    return True
                p = self._paused
                stale = now - (p.paused_at or now) > self.stale_after
                kept = p.chunks[p.sent] if 0 <= p.sent < len(p.chunks) else None
                if (kept is not None and p.sent > p.played and not p.cancelled
                        and not stale and ack_floor_seconds(kept.audio) > 0
                        and now < kept.earliest_ack + RESUME_ACK_GRACE_SEC):
                    # The chunk the client kept is still coming out of the
                    # speaker, and its ack will name the index it was SENT
                    # under. The bridge shifts every index above `played`,
                    # so inserting it now would let that ack land on the
                    # bridge. Wait for the ack — at most the audio's own
                    # length plus a grace — before resuming. Only audio with
                    # a readable length (mp3, which is all tts.py asks for)
                    # can be waited for; a blob with no floor resumes at once,
                    # as before.
                    self._kick_later(kept.earliest_ack + RESUME_ACK_GRACE_SEC - now)
                    return False
                self._paused = None
                spoke = self._last_user_speech is not None and p.paused_at is not None \
                    and self._last_user_speech > p.paused_at
                if p.cancelled or stale or spoke or p.resumes >= MAX_RESUMES:
                    p.cancelled = True
                    self._settle(p.id, above=p.played + 1)   # the kept chunk may still be playing
                    self._orphan(p.id)                       # ...but not for two minutes
                else:
                    self._insert_bridge(p)
                    self._current = p
                    return True
            nxt = self._next_pending(now)
            if nxt is not None:
                self._pending.remove(nxt)
                self._current = nxt
                return True
            if self._speaking and self._nothing_in_flight():
                await self._set_speaking(False)
            return False

        cur = self._current
        if (cur.sent > cur.played and cur.last_progress_at is not None
                and now - cur.last_progress_at > self.ack_timeout):
            # No ack for far longer than any chunk takes to play: the client is
            # gone or wedged. Never let that pin the mouth forever.
            log.warning(f"speech: no ack for {self.ack_timeout:.0f}s on utterance {cur.id}; abandoning")
            self._abandon(cur, keep_unread=True)
            return True
        return await self._send_ready(cur, now)

    def _insert_bridge(self, u: Utterance) -> None:
        """Resume after a preemption: a bridge phrase goes in front of the first
        unplayed chunk. Chunks own their audio, so shifting positions is safe even
        while synthesis for later chunks is still running."""
        bridge = Chunk(self.bridges[u.resumes % len(self.bridges)])
        u.resumes += 1
        log.info("speech: resuming utterance %s (resume %d of %d) from chunk %d",
                 u.id, u.resumes, MAX_RESUMES, u.played + 1)
        pos = u.played + 1
        u.chunks.insert(pos, bridge)
        if u.high_sent >= pos:
            u.high_sent += 1        # every frame that went out at >= pos now sits one higher
        u.sent = u.played
        # A hold taken while paused is an INDEX, and the bridge just moved
        # every index from `pos` up by one: discharged now, it would mark the
        # bridge played before its audio exists. The chunks it referred to
        # are re-sent behind the bridge, so the client acks them again.
        u.held_ack = -1
        self._spawn(self._synthesize(u, bridge))

    async def _send_ready(self, u: Utterance, now: float) -> bool:
        progressed = False
        while not u.cancelled and u.sent - u.played < self.prefetch:
            idx = u.sent + 1
            if idx >= len(u.chunks):
                break
            chunk = u.chunks[idx]
            if not chunk.ready:
                break
            # Synthesis runs concurrently and can finish out of order. Count
            # failures in delivery order, once per chunk (resumes may replay).
            silent = chunk.silent or not self.voice_on
            if idx > u.high_sent and not silent:
                if chunk.audio is None:
                    self._tts_failures += 1
                    chunk.notice = self._tts_failures == 3
                else:
                    self._tts_failures = 0
            if chunk.audio is None or silent:
                # Nothing to play, so nothing to ack — but it still takes its
                # TURN. Without a link here the next chunk's chain restarted
                # at `now`, and a single failed synthesis let `wait_for` say
                # "heard in full" 22 ms into ten seconds of audio.
                previous = u.chunks[idx - 1].earliest_ack if idx > 0 else 0.0
                chunk.earliest_ack = max(previous, now)
                u.sent = idx
                u.high_sent = max(u.high_sent, idx)
                if now >= chunk.earliest_ack:
                    u.played = max(u.played, idx)
                else:
                    u.held_ack = max(u.held_ack, idx)
                    self._kick_later(chunk.earliest_ack - now)
                if not await self._send({"type": "text", "text": chunk.text}, u):
                    return False
                if chunk.notice:
                    await self._send({"type": "text", "text": "My voice is failing, sir."}, u)
                progressed = True
                continue
            await self._set_speaking(True)
            async with self._emit_lock:
                if u.cancelled:                 # a barge-in landed while we waited
                    break
                spoken = _Spoken(u.id, idx, set(_tokens(chunk.text)), now)
                self._recent.append(spoken)
                # Sequential playback: this chunk cannot end before the one
                # before it could, plus its own length.
                previous = u.chunks[idx - 1].earliest_ack if idx > 0 else 0.0
                chunk.earliest_ack = max(previous, now) + ack_floor_seconds(chunk.audio)
                u.sent = idx
                if u.first_sent_at is None:
                    u.first_sent_at = self._clock()   # this moment, not the tick's
                u.last_progress_at = now
                try:
                    await self._emit_raw({"type": "audio", "utt": u.id, "idx": idx,
                                          "data": base64.b64encode(chunk.audio).decode(),
                                          "text": chunk.text})
                    u.high_sent = max(u.high_sent, idx)   # it really did go out
                except Exception as e:
                    # The transport is broken: nothing we send will play. Drop
                    # this utterance cleanly rather than wedge the mouth.
                    log.error(f"speech transport failed: {e}")
                    self._recent.remove(spoken)
                    u.sent = idx - 1
                    self._abandon(u)
                    return False
            progressed = True
        return progressed

    async def _send(self, msg: dict, u: Utterance) -> bool:
        """Emit a non-audio frame on behalf of `u`; on transport failure abandon `u`."""
        async with self._emit_lock:
            if u.cancelled:
                return False
            try:
                await self._emit_raw(msg)
                return True
            except Exception as e:
                log.error(f"speech transport failed: {e}")
                self._abandon(u)
                return False

    def _abandon(self, u: Utterance, keep_unread: bool = True) -> None:
        """The client cannot hear this. An URGENT item is kept as unread so it is
        re-raised once someone is listening; everything else is simply lost."""
        if keep_unread and u.priority == Priority.URGENT and not u.done:
            rest = u.remaining_text()
            if rest:
                self._unread.append(rest)
        u.cancelled = True
        u.abandoned = True
        self._settle(u.id)
        if self._current is u:
            self._current = None
        self._speaking = False       # the client never heard the `idle`; do not pretend it did
        self._kick()
