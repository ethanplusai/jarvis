"""One policy for every line to the owner's phone.

JARVIS can reach the user on WhatsApp (`whatsapp.py`, through Kapso) and on
Telegram (`telegram.py`, a bot). The wire differs — what a button is, how a
message is read back, what the far end returns — and everything else does
not: a card is shown the same way, a tap decides it the same way, "approve"
and "go" mean the same thing, and a text from the owner is the same turn.
That shared half lives here, once, so a rule fixed on one line cannot stay
broken on the other. The transports are thin: config, client, poll, send.

What a transport provides (the `Line` protocol below) is deliberately
small: a way to deliver text to the owner and nobody else, a way to put a
card with two buttons in front of him, a way to send a voice note, and its
own status. What this module provides in return is the policy: the
callbacks the server registers (`chat`, `synth`, `confirm`), the fan-out
the server's announcements go through (`reach`, `say`, `notify_card`),
and the handling of an OWNER'S message a transport hands over (`on_text`,
`on_button`, `on_other`).

**Owner only.** No function here takes a recipient. A transport is handed
a message only after it has matched the sender against its configured
owner; a stranger never reaches this module.

**Decisions are the desk's.** A button carries a card's id and its digest
(the whole digest on WhatsApp; on Telegram, whose button payload is 64
bytes, its first sixteen hex characters, checked against the stored card
before the FULL digest is used). Either way the decision is
`business_api.decide`, the same compare-and-swap the browser route uses,
recorded as `via:<line>`.

**A turn runs beside the poll, not inside it.** A tap, an "approve" and a
"go" are decided at once; a message for the brain is handed to a task
(`_start_turn`) and the poll carries on. The poll used to wait for the whole
turn — and a connector call made in that turn is held for two minutes
waiting for the owner's tap, which then sat unread behind the very turn it
was for. Turns still run one at a time, in the order they came
(`_turn_gate`), and `stop` cancels one in flight.

**A forward is not the owner speaking.** A message he forwarded carries
somebody else's words. It never reaches the word gates — a forwarded
"approve" or "go" decides nothing — and the server gives the brain its text
walled and marked as foreign (`server._phone_chat`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional, Protocol

log = logging.getLogger("jarvis.messaging")

# Both services cap a text message at 4096 characters; a chunk leaves room
# for the "(2/3)" marker.
TEXT_CHUNK = 4000
# How much of a request a card shows when the line gives it no more room.
# A card shows the WHOLE request whenever it fits; this is only the floor.
CARD_PREVIEW_MAX = 520
# A request too long for the card goes in full in plain messages just before
# it, up to this much; past that the phone gets the start and the desk the
# rest. A LinkedIn post is 3,000 characters at most.
PRELUDE_MAX = 3 * TEXT_CHUNK

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# A button carries the whole digest (64) or, where the wire is short, a
# prefix of it; never fewer than sixteen hex characters, which is more than
# enough to tell two cards apart and still leaves room in 64 bytes.
_DIGEST_PREFIX = re.compile(r"[0-9a-f]{16,64}")

# A decision typed as words: the verb, then optionally the card's id or its
# first characters. Deliberately NOT "yes" / "no" / "ok": a bare yes is how
# the owner answers a question JARVIS asked, and a card happening to be
# pending must not turn that into an approval.
#
# Whole-value gates (`fullmatch`), on text `clean` has already stripped, and
# with no `\s` in them: `\s` admits every separator `str.splitlines()` knows,
# so a trailing `\s*` would have let "approve\n<anything>" through the door
# meant for one word. tests/test_anchored_patterns.py holds every gate in the
# repository to this.
_DECISION = re.compile(
    r"/?(?P<verb>approve|approved|allow|reject|rejected|decline|deny)\b"
    r"[ \t:,\-]*(?:card[ \t]*)?(?P<ref>[0-9a-f][0-9a-f-]{3,35})?[.!]?",
    re.IGNORECASE)
_APPROVE_VERBS = {"approve", "approved", "allow"}
# "go" / "cancel" for something a turn staged and read back (a steer, a
# command, a keypress — server._phone_confirm). Only consulted while
# something IS waiting; otherwise the same words are conversation.
_CONFIRM = re.compile(
    r"/?(?P<verb>go ahead|go|send it|send|confirm|proceed|do it|yes go|"
    r"cancel|stop|don'?t|abort)\b[ \t]*(?:#|no\.?[ \t]*)?(?P<n>[0-9]{1,4})?[.!]?",
    re.IGNORECASE)
_GO_VERBS = {"go", "go ahead", "send it", "send", "confirm", "proceed", "do it", "yes go"}

_NOTHING_WAITING_LINE = "Nothing is waiting for you, sir."
_DECIDED_ALREADY_LINE = ("That card has already been decided, or it has lapsed, "
                         "sir — nothing changed.")
_NOT_TEXT_LINE = "I can only read text here, sir — type it and I'll see it."
_NOT_MINE_LINE = "That button is not one of mine, sir."


class ChannelError(Exception):
    """A send or read that did not happen.

    `str(e)` is a sentence written HERE — the status and the service's
    error code, never the service's own words — because it is what the
    brain is told when `message_user` fails, and a service's message is
    somebody else's text. What the service said is kept in `detail`,
    bounded, for the log and the status line the Settings panel renders
    with textContent.
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[int] = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail

    @property
    def report(self) -> str:
        """For the log and the status page: the sentence, then the detail."""
        return f"{self} — {self.detail}" if self.detail else str(self)


class NotConfigured(ChannelError):
    """The line has no token, no number, or no owner."""


class Line(Protocol):
    """What a transport module offers this policy. Module-level, so a test
    can hand in a stand-in with the same names."""
    NAME: str                   # "whatsapp" | "telegram": the audit's `via:`
    LABEL: str                  # "WhatsApp" | "Telegram": for sentences
    BODY_MAX: int               # the longest body a card message may have
    BUTTON_DIGEST_CHARS: int    # how much of the digest a button id can hold

    def configured(self) -> bool: ...
    def owner_key(self) -> Any: ...     # whose line it is now; None unpaired
    def approvals(self) -> bool: ...
    def voice_notes(self) -> bool: ...
    def status(self) -> dict: ...
    async def deliver(self, text: str) -> dict: ...
    async def announce_card(self, action: dict, body: str,
                            buttons: list[tuple[str, str]]) -> bool: ...
    async def send_voice_note(self, audio: bytes) -> dict: ...
    def start(self, *, poll: bool = True) -> Optional[asyncio.Task]: ...
    async def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Text helpers shared by every line
# ---------------------------------------------------------------------------

def clean(text: Any) -> str:
    return _CONTROL.sub("", str(text or "")).strip()


def name(value: Any, limit: int = 60) -> str:
    """A provider, an operation, an id: one line, clipped."""
    text = clean(value).replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def chunks(text: str, limit: int = TEXT_CHUNK) -> list[str]:
    """`text` as pieces of at most `limit` characters, cut at a paragraph,
    a line, a sentence, or a space — in that order of preference."""
    text = clean(text)
    if not text:
        return []
    out = []
    while len(text) > limit:
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            cut = text.rfind(sep, limit // 2, limit)
            if cut != -1:
                cut += len(sep) if sep != " " else 0
                break
        if cut <= 0:
            cut = limit
        piece, text = text[:cut].rstrip(), text[cut:].lstrip()
        if piece:
            out.append(piece)
    if text:
        out.append(text)
    return out


def sniff_audio(audio: bytes) -> tuple[str, bool]:
    """(MIME type, is it a voice note) for what a synthesiser handed back.

    Both services draw a voice note — the waveform bubble that plays in the
    chat — only for Ogg Opus; an MP3 is still delivered, as a plain audio
    file. Decided from the bytes rather than from what was asked for, so a
    synthesiser that ignores the format request still gets its audio through.
    """
    if audio[:4] == b"OggS":
        return "audio/ogg", True
    if audio[:3] == b"ID3" or (len(audio) > 1 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
        return "audio/mpeg", False
    if audio[:4] == b"RIFF":
        return "audio/wav", False
    return "audio/mpeg", False


# ---------------------------------------------------------------------------
# The lines, and what the server hands them
# ---------------------------------------------------------------------------

# (text, message_id, line, forwarded=…) → the reply
ChatFn = Callable[..., Awaitable[str]]
SynthFn = Callable[[str], Awaitable[Optional[bytes]]]
# ("go" or not, the item's number or None, the line it came over) → what to
# reply, or None when nothing was waiting and the word was just conversation.
ConfirmFn = Callable[[bool, Optional[int], str], Awaitable[Optional[str]]]
# (the line, the message id) — the reply to THAT message, read-back and all,
# has reached the owner there. Per message: a reply that got through must not
# vouch for an earlier one that did not.
ShownFn = Callable[[str, str], None]

_lines: list = []
_chat: Optional[ChatFn] = None
_synth: Optional[SynthFn] = None
_confirm: Optional[ConfirmFn] = None
_shown: Optional[ShownFn] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_keep: set = set()
# The owner's turns in flight, and what keeps them to one at a time. The
# lock is made on the loop that first needs it: a test suite runs many.
_turns: set = set()
_turn_gate: Optional[tuple] = None          # (loop, asyncio.Lock)
# False from the moment a shutdown cancels the turns: a message that arrives
# after that is told to come back, not started on a brain about to stop.
_accepting = True
_told_restarting: set = set()           # lines already told, this shutdown

# Said on a restart and on a plain stop alike, so it promises neither.
_RESTARTING_LINE = ("I'm going offline, sir. If I haven't answered your last message, send it "
                    "again when I'm running.")


def register(line) -> None:
    if line not in _lines:
        _lines.append(line)


def _ensure_default_lines() -> None:
    """The two transports this repository ships, imported here rather than
    at the top: each of them imports this module for the policy."""
    if _lines:
        return
    import telegram
    import whatsapp
    register(whatsapp)
    register(telegram)


def lines() -> list:
    _ensure_default_lines()
    return list(_lines)


def configured_lines() -> list:
    return [line for line in lines() if line.configured()]


def configured() -> bool:
    return bool(configured_lines())


def start(*, chat: Optional[ChatFn] = None, synth: Optional[SynthFn] = None,
          confirm: Optional[ConfirmFn] = None, shown: Optional[ShownFn] = None,
          poll: bool = True) -> list:
    """Wire every line to the running server. Returns the poller tasks.
    Safe to call unconfigured: a poller sleeps until its settings arrive
    (they can be saved from the Settings panel without a restart)."""
    global _chat, _synth, _confirm, _shown, _loop, _accepting
    _chat, _synth, _confirm, _shown = chat, synth, confirm, shown
    _loop = asyncio.get_running_loop()
    _accepting = True
    _told_restarting.clear()
    tasks = []
    for line in lines():
        try:
            task = line.start(poll=poll)
        except Exception:
            log.warning("%s line could not start", line.NAME, exc_info=True)
            continue
        if task is not None:
            tasks.append(task)
    return tasks


async def cancel_turns() -> None:
    """Cancel the owner's turns in flight — before the brain they are
    waiting on is stopped, so a shutdown does not text him that it lost its
    train of thought — and take no new ones. Each line that lost a turn
    tells him once that it is restarting (`_turn`). Loops until none is
    left, so a turn started while the others were being cancelled goes too."""
    global _accepting
    _accepting = False
    while _turns:
        turns = list(_turns)
        for task in turns:
            task.cancel()
        await asyncio.gather(*turns, return_exceptions=True)
        _turns.difference_update(turns)


async def stop() -> None:
    """Cancel the owner's turns in flight, then stop every line."""
    await cancel_turns()
    for line in lines():
        try:
            await line.stop()
        except Exception:
            log.warning("%s line did not stop cleanly", line.NAME, exc_info=True)


async def wait_for_turns() -> None:
    """Until every turn handed off so far has finished — its reply sent.
    The tests' way to wait for a turn the poll no longer waits for."""
    while _turns:
        await asyncio.gather(*list(_turns), return_exceptions=True)
        _turns.difference_update([task for task in list(_turns) if task.done()])


def schedule(make: Callable[[], Awaitable[Any]]) -> bool:
    """Run a coroutine on the server's loop from any thread — the store's
    hook fires inside a threadpool when the desk's own route proposes."""
    loop = _loop
    if loop is None or loop.is_closed():
        return False

    def _go() -> None:
        task = loop.create_task(_swallow(make()))
        _keep.add(task)
        task.add_done_callback(_keep.discard)

    try:
        loop.call_soon_threadsafe(_go)
    except RuntimeError:
        return False
    return True


async def _swallow(aw: Awaitable[Any]) -> None:
    try:
        await aw
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("messaging: background send failed", exc_info=True)


# ---------------------------------------------------------------------------
# Outbound: what the server says to the owner, on every line he has
# ---------------------------------------------------------------------------

async def _synthesise(text: str) -> Optional[bytes]:
    if _synth is None:
        return None
    try:
        return await _synth(text)
    except Exception:
        log.warning("messaging: synthesis for a voice note failed", exc_info=True)
        return None


async def _voice_on(line, audio: bytes) -> bool:
    try:
        await line.send_voice_note(audio)
        return True
    except ChannelError as e:
        log.warning("%s: voice note not sent: %s", line.NAME, e.report)
        return False
    except Exception:
        log.warning("%s: voice note not sent", line.NAME, exc_info=True)
        return False


async def reach(text: str, *, voice: bool = False) -> bool:
    """Tell the owner `text` on every configured line — and, when `voice`
    and that line's setting allow it, as a voice note too. Never raises;
    True when at least one line took it."""
    reached = False
    audio: Optional[bytes] = None
    synthesised = False
    for line in configured_lines():
        try:
            await line.deliver(text)
        except ChannelError as e:
            log.warning("%s: could not reach the owner: %s", line.NAME, e.report)
            continue
        except Exception:
            log.warning("%s: could not reach the owner", line.NAME, exc_info=True)
            continue
        reached = True
        if voice and line.voice_notes():
            if not synthesised:
                audio, synthesised = await _synthesise(text), True
            if audio:
                await _voice_on(line, audio)
    return reached


async def say(text: str, *, voice: bool = False) -> dict:
    """The brain's and the test buttons' send: text on every configured
    line, plus a voice note on request. Raises NotConfigured when there is
    no line, and the first ChannelError when no line took the text."""
    targets = configured_lines()
    if not targets:
        raise NotConfigured("No phone line is set up: see docs/telegram.md or docs/whatsapp.md")
    sent, failed, first_error = [], {}, None
    receipts: dict = {}
    for line in targets:
        try:
            receipts[line.NAME] = await line.deliver(text)
            sent.append(line.NAME)
        except ChannelError as e:
            failed[line.NAME] = str(e)
            first_error = first_error or e
    if not sent:
        raise first_error or ChannelError("Nothing was sent")
    voice_note = False
    if voice:
        audio = await _synthesise(text)
        if audio:
            for line in targets:
                if line.NAME in sent and await _voice_on(line, audio):
                    voice_note = True
    return {"sent": sent, "failed": failed, "voice_note": voice_note,
            "via": receipts[sent[0]].get("via", "text"), "receipts": receipts}


# ---------------------------------------------------------------------------
# Approval cards
# ---------------------------------------------------------------------------

def button_ids(action: dict, digest_chars: int = 64) -> tuple[str, str]:
    """The Approve and Reject ids for a card: each names the card AND (as
    much of) its digest (as the wire allows), so a tap can only ever decide
    the exact request it was sent for."""
    digest = str(action["digest"])[:max(16, digest_chars)]
    return f"ok:{action['id']}:{digest}", f"no:{action['id']}:{digest}"


def parse_button(button_id: str) -> Optional[tuple[bool, str, str]]:
    """(approve?, card id, digest or its prefix) out of a button id JARVIS
    made, or None for anything else."""
    verb, _, rest = str(button_id or "").partition(":")
    action_id, _, digest = rest.partition(":")
    if verb not in ("ok", "no") or not _UUID.fullmatch(action_id) \
            or not _DIGEST_PREFIX.fullmatch(digest):
        return None
    return verb == "ok", action_id, digest


def readable_request(payload: Any) -> str:
    """A request as the owner should read it on a phone: one field per line,
    and a long or multi-line text as itself — line breaks and all, never a
    JSON string with `\\n` in it. Measured live, 2026-10-01: a 1,300-character
    post reached the phone as 520 characters of escaped JSON, and that card
    is what was approved. The desk keeps the exact JSON as well."""
    import business_api  # lazy: heavy, and never needed by the MCP child
    if not isinstance(payload, dict):
        return business_api.display_payload(payload)
    blocks: list[str] = []
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, str) and ("\n" in value or len(value) > 60):
            blocks.append(f"{key}:\n{value}")
        elif isinstance(value, str):
            blocks.append(f"{key}: {value}")
        else:
            blocks.append(f"{key}: " + business_api.display_payload(value))
    return "\n\n".join(blocks)


def _request_for_phone(action: dict, found: set) -> str:
    import business_api
    payload = action.get("payload")
    # A LinkedIn post through the API: the post itself, as it will read, not
    # the account it is bound to (that is the desk's to show).
    if action.get("provider") in ("linkedin", "linkedin_hand") and isinstance(payload, dict) \
            and isinstance(payload.get("request"), dict):
        payload = payload["request"]
    try:
        return readable_request(
            business_api.redact(payload, business_api.secret_values(), found))
    except Exception:
        return "(request could not be shown)"


def _card_parts(action: dict, body_max: int) -> tuple[str, str, str, int]:
    """(head, the whole request, tail, room the request has) for one card."""
    import business_api  # lazy: heavy, and never needed by the MCP child
    provider, operation = name(action.get("provider")), name(action.get("operation"))
    connector = str(action.get("provider") or "").startswith("connector:")
    found: set = set()
    request = _request_for_phone(action, found)
    try:
        repeat = business_api.repeat_note(action)
    except Exception:
        repeat = None
    when = ""
    try:
        when = datetime.fromtimestamp(float(action.get("expires"))).strftime("%a %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    head = [f"Approval needed, sir — {provider} · {operation}.",
            f"Card {str(action.get('id', ''))[:8]}" + (f" · lapses {when}" if when else "") + "."]
    if repeat:
        head.append("⚠ " + (repeat if len(repeat) <= 300 else repeat[:299] + "…"))
    tail = [("Approving allows exactly this call once — now, if I am still holding it "
             "(two minutes), or the next time you ask me for it.") if connector
            else "Approving sends exactly this request now.",
            "The whole card is on the Business desk."]
    if found:
        tail.insert(0, "A secret in the request is shown as [redacted].")
    head_text, tail_text = "\n".join(head), "\n".join(tail)
    room = body_max - len(head_text) - len(tail_text) - 4
    return head_text, request, tail_text, room


def card_text(action: dict, *, body_max: int = 1024, preview_max: Optional[int] = None,
              sent_above: bool = False) -> str:
    """What the owner reads on his phone: what it is, when it lapses, the
    redacted request as it will read, and what a yes does. The WHOLE request
    whenever it fits in `body_max`; when it does not, the start of it, and
    either "the message just above" (`sent_above`: `announce` sent it in
    full first) or the desk. Never longer than `body_max`."""
    head, request, tail, room = _card_parts(action, body_max)
    if len(request) > room:
        note = ("\n\n[The whole request is in the message just above; this card "
                "approves exactly that.]" if sent_above
                else "\n\n[Cut short here; the whole request is on the Business desk.]")
        keep = min(preview_max or room, room) - len(note) - 1
        request = (request[:keep] + "…" if keep > 0 else "") + note
    body = head + "\n\n" + request + "\n\n" + tail
    return body if len(body) <= body_max else body[:body_max - 1] + "…"


def fits_on_card(action: dict, body_max: int) -> bool:
    """Whether the whole request fits on the card itself."""
    _head, request, _tail, room = _card_parts(action, body_max)
    return len(request) <= room


# Cards a line could not announce, by line name: sent again once the line
# answers a poll (`retry_unannounced`). Card ids only; the card is read
# afresh from the store, so one decided or lapsed meanwhile is dropped.
_unannounced: dict[str, set] = {}


async def announce(line, action: dict) -> bool:
    """One card on one line. A request too long for the card goes first, in
    full, as plain messages (`PRELUDE_MAX`), and the card says so. A card
    that could not be announced is remembered for `retry_unannounced`.
    Never raises; True when the card itself went."""
    action_id = str(action.get("id") or "")
    try:
        ok, no = button_ids(action, line.BUTTON_DIGEST_CHARS)
        sent_above = False
        if not fits_on_card(action, line.BODY_MAX):
            request = _request_for_phone(action, set())
            if len(request) <= PRELUDE_MAX:
                try:
                    await line.deliver(f"Card {action_id[:8]} — exactly what would be sent:\n\n"
                                       + request)
                    sent_above = True
                except Exception as e:
                    # WhatsApp's shut 24-hour window takes no free text; the
                    # card still goes, and points at the desk instead.
                    log.info("%s: request not sent ahead of its card: %s",
                             getattr(line, "NAME", "?"), e)
        body = card_text(action, body_max=line.BODY_MAX, sent_above=sent_above)
        told = bool(await line.announce_card(action, body, [(ok, "Approve"), (no, "Reject")]))
    except Exception:
        log.warning("%s: card not announced", getattr(line, "NAME", "?"), exc_info=True)
        told = False
    waiting = _unannounced.setdefault(getattr(line, "NAME", "?"), set())
    if told:
        waiting.discard(action_id)
    elif action_id:
        # 2026-10-01: Telegram was unreachable as the card was staged, the
        # owner said "Don't see it", and nothing ever sent it again.
        waiting.add(action_id)
    return told


async def retry_unannounced(line) -> int:
    """Send again what `line` could not announce, if it is still waiting for
    a decision. Called by each line's poller once a poll succeeds — the sign
    the line is back. Returns how many cards went. Never raises."""
    import business_store
    waiting = _unannounced.get(getattr(line, "NAME", "?"))
    if not waiting:
        return 0
    sent = 0
    for action_id in sorted(waiting):
        try:
            action = business_store.get_action(action_id)
        except Exception:
            waiting.discard(action_id)
            continue
        if action.get("state") != "pending" or float(action.get("expires") or 0) <= time.time():
            waiting.discard(action_id)
            continue
        if await announce(line, action):
            sent += 1
    return sent


async def notify_card(action: dict) -> bool:
    """A new pending card → every configured line, with buttons. Never
    raises; True when at least one line announced it."""
    if not isinstance(action, dict) or action.get("state") != "pending":
        return False
    told = False
    for line in configured_lines():
        if await announce(line, action):
            told = True
    return told


# ---------------------------------------------------------------------------
# Inbound: what a message from the OWNER does
# ---------------------------------------------------------------------------

def _approvals_off_line(line) -> str:
    return (f"Approvals over {line.LABEL} are switched off, sir "
            f"({line.NAME.upper()}_APPROVALS=0); decide it on the desk.")


async def _reply(line, text: str) -> bool:
    """Send `text` on `line`. True when it went."""
    try:
        await line.deliver(text)
        return True
    except ChannelError as e:
        log.warning("%s: reply not sent: %s", line.NAME, e.report)
    except Exception:
        log.warning("%s: reply not sent", line.NAME, exc_info=True)
    return False


_TURN_FAILED_LINE = "I lost my train of thought, sir. Say that again?"


_UNSET: Any = object()


def _authenticated(line, owner: Any) -> Any:
    """The owner a message was authenticated as. A transport passes the
    line's `owner_key` as it stood when it matched the sender — before any
    await of its own; a caller that passes nothing gets the owner now."""
    return _owner_of(line) if owner is _UNSET else owner


def _changed_hands(line, owner: Any) -> bool:
    """True when the line no longer belongs to `owner` — or never did: on a
    line that has owners at all, a None owner is nobody's. Compared by the
    line's whole `owner_key`, which on Telegram carries a pairing epoch, so
    a phone unpaired and paired again does not match its old self."""
    if not callable(getattr(line, "owner_key", None)):
        return False
    return owner is None or _owner_of(line) != owner


async def on_text(line, text: str, message_id: str, *, forwarded: bool = False,
                  owner: Any = _UNSET) -> None:
    """A text from the owner: a decision, a go-ahead, or a turn.

    A decision and a go-ahead are settled here and now. A turn is handed
    off (`_start_turn`), so the line goes on being read while the brain
    works — see the module docstring. A FORWARDED message is only ever a
    turn: its words are not the owner's, so they decide nothing.

    `owner` is who the transport authenticated the sender as. If the line
    has changed hands since — an unpair landing while the transport showed
    "typing…" — nothing is decided, confirmed or started."""
    text = clean(text)
    owner = _authenticated(line, owner)
    if _changed_hands(line, owner):
        log.info("%s: a message was dropped: the line changed hands", line.NAME)
        return
    if not forwarded:
        words = _DECISION.fullmatch(text)
        if words:
            await _decide_by_words(line, words.group("verb").lower() in _APPROVE_VERBS,
                                   (words.group("ref") or "").lower())
            return
        confirm = _CONFIRM.fullmatch(text)
        if confirm and _confirm is not None:
            number = confirm.group("n")
            outcome = await _confirm(confirm.group("verb").lower() in _GO_VERBS,
                                     int(number) if number else None, line.NAME)
            if outcome:
                await _reply(line, outcome)
                return
    if _chat is None:
        await _reply(line, "I can hear you, sir, but my brain is not wired to this line yet.")
        return
    if not _accepting:
        await _reply(line, _RESTARTING_LINE)
        return
    _start_turn(line, text, message_id, forwarded, owner)


def _owner_of(line) -> Any:
    """Who the line belongs to right now — the line's own `owner_key`, or
    None for a stand-in that has none."""
    key = getattr(line, "owner_key", None)
    try:
        return key() if callable(key) else None
    except Exception:
        return None


def _gate() -> asyncio.Lock:
    """The lock that keeps the owner's turns to one at a time, made on the
    running loop the first time it is needed there."""
    global _turn_gate
    loop = asyncio.get_running_loop()
    if _turn_gate is None or _turn_gate[0] is not loop:
        _turn_gate = (loop, asyncio.Lock())
    return _turn_gate[1]


def _start_turn(line, text: str, message_id: str, forwarded: bool, owner: Any) -> None:
    gate = _gate()      # taken NOW, so turns queue in the order they arrived
    task = asyncio.get_running_loop().create_task(
        _turn(line, text, message_id, forwarded, gate, owner),
        name=f"{line.NAME}-turn")
    _turns.add(task)
    task.add_done_callback(_turns.discard)


async def _turn(line, text: str, message_id: str, forwarded: bool, gate: asyncio.Lock,
                owner: Any = None) -> None:
    """One of the owner's messages through the brain, and the reply back on
    the line it came over. Only once THAT reply has gone is what it read
    back answerable with "go" (`_shown`) — a stand-in "lost my train of
    thought" carries no read-back, and vouches for none.

    `owner` is who the transport authenticated the sender as. If the line
    has been unpaired or moved to another phone while it waited its turn,
    it is dropped: a phone taken off the line does not get a turn. If that
    happens while the turn RUNS, its reply is not sent — the line now
    reaches somebody else — and what it read back is never shown, so it
    lapses unperformed. The server stamps each read-back with this same
    owner (`_chat(owner=…)`), so no later owner can answer it."""
    told = False
    try:
        async with gate:
            if _changed_hands(line, owner):
                log.info("%s: a queued message was dropped: the line changed hands", line.NAME)
                return
            try:
                reply = await _chat(text, message_id, line.NAME, forwarded=forwarded,
                                    owner=owner)
                own = True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("%s: the turn failed", line.NAME, exc_info=True)
                reply, own = _TURN_FAILED_LINE, False
            if _changed_hands(line, owner):
                log.info("%s: a reply was not sent: the line changed hands during the turn",
                         line.NAME)
                return
            if reply:
                told = await _reply(line, reply)
            if told and own and _shown is not None:
                try:
                    _shown(line.NAME, message_id)
                except Exception:
                    log.warning("%s: could not mark the read-back as shown", line.NAME,
                                exc_info=True)
    except asyncio.CancelledError:
        if not told:
            await _tell_restarting(line)
        raise


async def _tell_restarting(line) -> None:
    """A turn cancelled by a shutdown: tell the owner once per line, briefly,
    and never hold the shutdown up for it."""
    if line.NAME in _told_restarting:
        return
    _told_restarting.add(line.NAME)
    try:
        await asyncio.wait_for(line.deliver(_RESTARTING_LINE), 5.0)
    except (Exception, asyncio.CancelledError):
        log.info("%s: could not say it is restarting", line.NAME)


async def on_button(line, button_id: str, *, owner: Any = _UNSET) -> dict:
    """A tap on a card's button. Returns {"decided": bool, "reply": str}
    so the transport can strip the buttons once the card is settled. A tap
    from a phone that no longer owns the line decides nothing, and is not
    answered."""
    if _changed_hands(line, _authenticated(line, owner)):
        log.info("%s: a tap was dropped: the line changed hands", line.NAME)
        return {"decided": False, "reply": ""}
    parsed = parse_button(button_id)
    if parsed is None:
        await _reply(line, _NOT_MINE_LINE)
        return {"decided": False, "reply": _NOT_MINE_LINE}
    approve, action_id, digest = parsed
    return await _decide(line, action_id, digest, approve)


async def on_other(line) -> None:
    """A picture, a sticker, a voice message from the owner."""
    await _reply(line, _NOT_TEXT_LINE)


async def _decide_by_words(line, approve: bool, ref: str) -> None:
    import business_store as store
    if not line.approvals():
        await _reply(line, _approvals_off_line(line))
        return
    now = time.time()
    if ref:
        rows = [row for row in store.find_actions(ref, limit=6)
                if row["state"] == "pending" and float(row["expires"]) > now]
    else:
        rows = [row for row in store.live_actions() if row["state"] == "pending"]
    if not rows:
        await _reply(line, _NOTHING_WAITING_LINE if not ref
                     else f"No card waiting for you starts with {ref}, sir.")
        return
    if len(rows) > 1:
        lines_ = ["More than one card is waiting, sir — say which:"]
        for row in rows[:6]:
            lines_.append(f"• {row['id'][:8]} — {name(row['provider'])} · {name(row['operation'])}")
        lines_.append("e.g. 'approve " + rows[0]["id"][:8] + "'.")
        await _reply(line, "\n".join(lines_))
        return
    row = rows[0]
    digest = row.get("digest") or store.get_action(row["id"])["digest"]
    await _decide(line, row["id"], digest, approve)


async def _decide(line, action_id: str, digest: str, approve: bool) -> dict:
    """One decision through `business_api.decide`, the desk's own function.

    `digest` is the whole digest or a prefix of it (a Telegram button holds
    sixteen characters): the stored card's digest must begin with it, and
    the decision is made with the stored, full digest — so a button decides
    only the card it was made for, and never a card whose request changed.
    """
    import business_api
    import business_store as store
    if not line.approvals():
        text = _approvals_off_line(line)
        await _reply(line, text)
        return {"decided": False, "reply": text}
    try:
        current = store.get_action(action_id)
        full = str(current.get("digest") or "")
        if not full.startswith(digest):
            raise ValueError("digest does not match the card")
        row = await business_api.decide(action_id, full, approve, via=line.NAME)
    except ValueError:
        await _reply(line, _DECIDED_ALREADY_LINE)
        return {"decided": False, "reply": _DECIDED_ALREADY_LINE}
    except Exception:
        log.warning("%s: decision failed", line.NAME, exc_info=True)
        text = "I could not record that decision, sir; use the desk."
        await _reply(line, text)
        return {"decided": False, "reply": text}
    text = _outcome_line(row, approve)
    await _reply(line, text)
    return {"decided": True, "reply": text}


def _outcome_line(row: dict, approve: bool) -> str:
    what = f"{name(row.get('provider'))} · {name(row.get('operation'))}"
    state = str(row.get("state") or "")
    if not approve:
        return f"Rejected, sir — {what}. Nothing was sent."
    if state == "approved":
        return (f"Allowed, sir — {what}. It goes out now if I am still holding it; "
                f"otherwise the next time you ask me for exactly that.")
    if state == "submitted":
        return f"Approved and sent, sir — {what}."
    if state == "failed":
        return f"Approved, sir, but {what} failed: " + name((row.get("result") or {}).get("message"), 160)
    if state == "unknown":
        return (f"Approved, sir, but I cannot tell whether {what} went — check the provider "
                f"before asking for it again.")
    return f"Recorded, sir — {what} is now {state}."


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

CALLS_NOTE = ("Not available on either line: WhatsApp calls need a WebRTC media stack and "
              "a non-US business number, and Telegram bots cannot place calls. Urgent lines "
              "go as a voice note instead; a ringing phone call is the Twilio provider on "
              "the Business desk.")


def status() -> dict:
    """Every line's own status, and whether any is usable. No secrets."""
    return {"configured": configured(),
            "lines": {line.NAME: line.status() for line in lines()},
            "calls": CALLS_NOTE}
