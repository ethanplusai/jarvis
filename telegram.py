"""JARVIS on Telegram — the Bot API transport for the owner's phone.

The second line to the owner (the first is `whatsapp.py`; `messaging.py`
holds what they share). It exists because a dedicated WhatsApp number needs
a Facebook login and a Meta Business Portfolio, and a Telegram bot needs
neither: `@BotFather` hands out a token in a minute, there is no phone
number, no 24-hour window and no template review, and the Bot API's
`getUpdates` long polling is made for a process that must not be reachable
from the network. What Telegram cannot do is place a call; the urgent lines
go as a voice note in his voice, exactly as on WhatsApp.

**Long polling, not a webhook.** `getUpdates` holds the request open for up
to `POLL_TIMEOUT_SEC` and answers the moment something arrives, so the
owner's message is seen within a second and an idle line costs one request
every twenty-five seconds. JARVIS stays bound to loopback; nothing listens.
`deleteWebhook` is called once at start, because a webhook set on the bot
elsewhere makes every poll answer 409. The offset is persisted so a restart
carries on, and every `update_id` is claimed in SQLite before it is acted on,
so an update redelivered after a crash cannot act twice.

**The token is in the URL, and never in a log.** The Bot API wants it in
the path (`/bot<token>/getUpdates`), and httpx logs every request's URL at
INFO — which server.py writes to backend.err.log. A filter on every logger
that can print a URL (`URL_LOGGERS`) rewrites the secret half before any
handler sees the line, so the request lines stay and the credential does not.

**Pairing.** `TELEGRAM_BOT_TOKEN` is required. `TELEGRAM_OWNER_ID` — the
owner's numeric Telegram id — is what makes the line usable, and nobody
knows their own id. So the Settings panel (or `scripts/telegram_setup.py
pair`) asks for a six-digit code, good for ten minutes, once; the owner
sends that code to the bot from their phone; the poller binds that sender
as the owner and writes the id to `.env`. Paired already or not: pairing
again is how a lost or replaced phone is moved off the line, and the owner
sending the code is pairing, never a turn. While a code is live, every
six-digit message in a private chat is a pairing attempt, whoever sent it —
unless it is dated before the code was made (on Telegram's clock, see
`_clock_offset`): it sat in the queue before the code existed, so it neither
pairs nor counts, and from the owner it is an ordinary turn. The code is
withdrawn after `PAIR_MAX_MISSES` wrong ones. Each code has a serial, an outcome — paired,
cancelled, lapsed, locked — and a note (paired for this run only; the clock
looks wrong), which is what the page and the script wait for: `configured`
cannot say whether THIS pairing worked. The code itself is never in the
status: only whoever minted it sees it. Unpair forgets the owner, and a
turn his messages had queued is dropped rather than run for a phone no
longer on the line. Any other message from anybody else is dropped; its
sender's id (never the name he chose) is kept in the status.

**Owner only, both ways.** Every send goes to the owner's id — there is no
`to` parameter here — and only a private chat from that id is read. What
the owner's message DOES is `messaging.on_text` / `on_button` / `on_other`,
the same policy the WhatsApp line uses; every decision it records is
`via:telegram`. A message he FORWARDED is marked as such (`forwarded`):
its words are somebody else's, and the policy treats them so.

**Buttons.** Telegram's `callback_data` is 64 bytes, so the Approve id
carries the card's id and the first sixteen hex characters of its digest
(`messaging.button_ids`); the policy checks the stored card's digest begins
with them and decides with the full one. Once a card is decided the
buttons are taken off the message, so a second tap has nothing to press.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing
from typing import Any, NamedTuple, Optional

import httpx

import messaging
from data_paths import db_path
from messaging import ChannelError, NotConfigured, chunks, clean as _clean, sniff_audio

log = logging.getLogger("jarvis.telegram")

NAME = "telegram"
LABEL = "Telegram"
DEFAULT_BASE_URL = "https://api.telegram.org"

TEXT_BODY_MAX = 4096
TEXT_CHUNK = messaging.TEXT_CHUNK
CALLBACK_DATA_MAX = 64          # bytes, Telegram's own limit
BODY_MAX = TEXT_CHUNK           # a card is an ordinary message with a keyboard
BUTTON_DIGEST_CHARS = 16        # "ok:" + uuid + ":" + 16 hex = 56 bytes

POLL_TIMEOUT_SEC = 25           # how long Telegram may hold a getUpdates open
REQUEST_TIMEOUT = 15.0
POLL_REQUEST_TIMEOUT = POLL_TIMEOUT_SEC + 15.0
MEDIA_TIMEOUT = 40.0
IDLE_SEC = 20.0                 # between polls when the line is not set up
PAIR_TTL_SEC = 600.0
PAIR_MAX_MISSES = 5             # wrong six-digit guesses before a code is withdrawn
# How much earlier than the code — both on Telegram's clock, see
# `_clock_offset` — a message may be dated and still count, right code or
# wrong alike: Telegram dates in whole seconds, and the offset is read from a
# whole-second header. Anything older sat in the queue before the code
# existed, and is neither a pairing nor a miss — so a backlog of blind
# guesses can neither pair nor lock a fresh code.
PAIR_CLOCK_SLACK_SEC = 10.0
# The right code's grace before any answer has carried a Date to measure the
# offset by (a self-hosted Bot API server sends none): this clock may run
# ahead of the server's, and refusing the right code helps nobody.
PAIR_UNMEASURED_SLACK_SEC = 300.0
STRANGERS_KEPT = 5

# A bot token is "<bot id>:<secret>"; an owner id is a plain integer.
_TOKEN = re.compile(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,80}")
_USER_ID = re.compile(r"[1-9][0-9]{2,19}")
_CODE = re.compile(r"/?(?:start[ \t]+)?(?P<code>[0-9]{6})")

_GREETING = ("At your service, sir. Approval cards, sessions waiting on you and finished "
             "work arrive here; text me anything, and 'approve' or 'reject' decides a card.")
_PAIRED_LINE = ("Paired, sir — this line is yours. " + _GREETING)


class Conflict(ChannelError):
    """409: another poller, or a webhook, holds this bot's updates."""


# ---------------------------------------------------------------------------
# The token stays out of the log
# ---------------------------------------------------------------------------

# Every logger that can print a request URL. httpx logs each request at INFO
# ("HTTP Request: POST <url> ..."); httpcore's loggers are DEBUG and print
# hosts rather than paths today, and are covered anyway because a logger's
# filter only ever sees records made by THAT logger, not by its children.
URL_LOGGERS = ("httpx", "httpcore", "httpcore.connection", "httpcore.connection_pool",
               "httpcore.http11", "httpcore.http2", "httpcore.proxy")

_TOKEN_IN_TEXT = re.compile(r"(?P<head>bot[0-9]{5,15}):[A-Za-z0-9_-]{30,80}")


def _hide_token(text: str) -> str:
    return _TOKEN_IN_TEXT.sub(r"\g<head>:•••", text)


class _HideBotToken(logging.Filter):
    """Rewrites `bot<id>:<secret>` to `bot<id>:•••` in a record, whatever
    carries it — the format string or any argument (httpx passes the URL as
    an `httpx.URL`, which only becomes text when the record is formatted).
    The record is formatted here once, so the handler prints what was
    checked. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Arguments that do not fit the format. Passed on untouched, the
            # handler would fail the same way and print the raw arguments —
            # URL and token — to stderr, which is the log. Say what it was,
            # hidden, instead.
            try:
                shown = f"{record.msg!r} args={record.args!r}"
            except Exception:
                shown = f"(an unformattable log record from {record.name}; arguments withheld)"
            record.msg = _hide_token(shown)
            record.args = ()
            return True
        hidden = _hide_token(message)
        if hidden != message:
            record.msg, record.args = hidden, ()
        return True


def _install_log_filter() -> None:
    """Once per logger, however often this module is imported or reloaded:
    a reload makes a new class object, so the check is by name."""
    for name in URL_LOGGERS:
        logger = logging.getLogger(name)
        logger.filters = [f for f in logger.filters
                          if type(f).__name__ != _HideBotToken.__name__]
        logger.addFilter(_HideBotToken())


_install_log_filter()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _flag(name: str, default: bool) -> bool:
    value = env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


class Config(NamedTuple):
    token: str
    owner_id: int
    base_url: str
    approvals: bool
    voice_notes: bool


def missing() -> list[str]:
    out = []
    if not env("TELEGRAM_BOT_TOKEN"):
        out.append("TELEGRAM_BOT_TOKEN")
    if not env("TELEGRAM_OWNER_ID"):
        out.append("TELEGRAM_OWNER_ID")
    return out


def issue() -> Optional[str]:
    """Why what is set cannot be used, or None. Checked on the token alone
    too, so a bad token is reported before pairing rather than after."""
    token = env("TELEGRAM_BOT_TOKEN")
    if token and not _TOKEN.fullmatch(token):
        return "TELEGRAM_BOT_TOKEN does not look like a bot token (123456789:AA…, from @BotFather)"
    owner = env("TELEGRAM_OWNER_ID")
    if owner and not _USER_ID.fullmatch(owner):
        return "TELEGRAM_OWNER_ID must be your numeric Telegram id (pair from Settings to fill it in)"
    return None


def touched() -> bool:
    return bool(env("TELEGRAM_BOT_TOKEN") or env("TELEGRAM_OWNER_ID"))


def token() -> str:
    """The bot token when it is usable, else "". Enough to poll for a
    pairing, not enough to reach anybody."""
    value = env("TELEGRAM_BOT_TOKEN")
    return value if value and _TOKEN.fullmatch(value) else ""


def config() -> Optional[Config]:
    if missing() or issue():
        return None
    return Config(
        token=env("TELEGRAM_BOT_TOKEN"),
        owner_id=int(env("TELEGRAM_OWNER_ID")),
        base_url=(env("TELEGRAM_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        approvals=_flag("TELEGRAM_APPROVALS", True),
        voice_notes=_flag("TELEGRAM_VOICE_NOTES", True),
    )


def configured() -> bool:
    return config() is not None


# Goes up on every pairing and every unpairing, so `owner_key` tells "the
# same phone, paired again" from "the phone that was here before".
_owner_epoch = 0


def owner_key() -> Optional[tuple]:
    """Whose line this is right now — (owner id, pairing epoch) — or None.
    `messaging` compares it for everything a message does after the poll
    matched its sender: a phone unpaired, replaced, or unpaired and paired
    again in the meantime gets nothing done."""
    cfg = config()
    return (cfg.owner_id, _owner_epoch) if cfg else None


def approvals() -> bool:
    return _flag("TELEGRAM_APPROVALS", True)


def voice_notes() -> bool:
    return _flag("TELEGRAM_VOICE_NOTES", True)


def require() -> Config:
    cfg = config()
    if cfg is None:
        names = missing()
        if names == ["TELEGRAM_OWNER_ID"]:
            raise NotConfigured("Telegram is not paired yet: Settings → Telegram → Pair, "
                                "then send the code to the bot from your phone")
        raise NotConfigured(
            "Telegram is not set up: set " + ", ".join(names) + " in .env"
            if names else "Telegram is not usable: " + str(issue()))
    return cfg


# ---------------------------------------------------------------------------
# Tables: the claim on each update, the offset, and a bounded log
# ---------------------------------------------------------------------------

LOG_ROWS_MAX = 2000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS telegram_messages (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ref TEXT UNIQUE NOT NULL,
            direction TEXT NOT NULL,
            kind TEXT NOT NULL,
            at REAL NOT NULL,
            summary TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS telegram_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)


def _state_get(key: str) -> str:
    try:
        with closing(_connect()) as conn:
            row = conn.execute("SELECT value FROM telegram_state WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


def _state_set(key: str, value: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO telegram_state(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _bot_id(bot_token: str) -> str:
    """The public half of a token: which bot, never its secret."""
    return bot_token.partition(":")[0]


def _offset_key(bot_token: str) -> str:
    """Where this bot's offset is kept. Per bot, because update ids are:
    a different token is a different queue, and an offset carried over from
    the old bot would tell Telegram to skip the new one's messages."""
    return f"offset:{_bot_id(bot_token)}"


def _update_ref(bot_token: str, update_id: int) -> str:
    """The claim on one update of one bot."""
    return f"u:{_bot_id(bot_token)}:{update_id}"


def _claim(ref: str, direction: str, kind: str, at: float, summary: str = "") -> bool:
    """Record one update or message once. True the first time, False ever
    after — the guarantee that nothing here runs twice."""
    if not ref:
        return False
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO telegram_messages(ref,direction,kind,at,summary) "
            "VALUES(?,?,?,?,?)", (ref, direction, kind, at, summary[:200]))
        first = cursor.rowcount == 1
        if first:
            conn.execute("DELETE FROM telegram_messages WHERE seq <= "
                         "(SELECT MAX(seq) FROM telegram_messages) - ?", (LOG_ROWS_MAX,))
        return first


def _last(direction: str) -> Optional[float]:
    try:
        with closing(_connect()) as conn:
            row = conn.execute("SELECT MAX(at) FROM telegram_messages WHERE direction=?",
                               (direction,)).fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        return None


def recent(limit: int = 20) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT ref,direction,kind,at,summary FROM telegram_messages "
                            "ORDER BY seq DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# The Bot API client
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None
_client_key: tuple = ()
_last_error: Optional[str] = None


def _base_url() -> str:
    return (env("TELEGRAM_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _new_client(base_url: str, bot_token: str) -> httpx.AsyncClient:
    """The one place a client is made — tests replace it with a MockTransport.
    The token is in the URL, as the Bot API wants it; `_HideBotToken` keeps
    it out of every log line that prints one."""
    return httpx.AsyncClient(base_url=f"{base_url}/bot{bot_token}", timeout=REQUEST_TIMEOUT,
                             follow_redirects=False, headers={"Accept": "application/json"})


async def _get_client(bot_token: str) -> httpx.AsyncClient:
    global _client, _client_key
    key = (_base_url(), bot_token)
    if _client is None or _client_key != key:
        if _client is not None:
            try:
                await _client.aclose()
            except Exception:
                pass
        _client = _new_client(*key)
        _client_key = key
    return _client


async def close() -> None:
    global _client, _client_key
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client, _client_key = None, ()


def _error_of(body: Any, status: int) -> ChannelError:
    """A ChannelError out of the Bot API's answer: the status and its error
    code in the sentence, its description only in `detail`."""
    code = None
    detail = ""
    retry_after = None
    if isinstance(body, dict):
        try:
            code = int(body.get("error_code")) if body.get("error_code") is not None else None
        except (TypeError, ValueError):
            code = None
        detail = str(body.get("description") or "")
        params = body.get("parameters")
        if isinstance(params, dict):
            retry_after = params.get("retry_after")
    detail = _clean(detail)[:200]
    message = f"Telegram returned HTTP {status}" + (f" (error {code})" if code is not None else "")
    error = Conflict if (code or status) == 409 else ChannelError
    out = error(message, status=status, code=code, detail=detail)
    out.retry_after = retry_after  # type: ignore[attr-defined]
    return out


# How far this machine's clock runs ahead of Telegram's, in seconds, from the
# `Date` header on Telegram's own latest answer. Pairing reads a message's
# date by Telegram's clock, so the code's minting time is put on that clock
# before the two are compared — and the slack needs to cover only whole-second
# dates and a network hop, not a guess at how wrong this clock might be.
_clock_offset = 0.0
_clock_measured = False     # has any answer carried a usable Date yet?
CLOCK_OFFSET_BELIEVABLE_SEC = 86400.0


def _note_server_clock(date_header: Optional[str]) -> None:
    """Measure the offset from a response's `Date`. A side measurement:
    whatever the header holds, it never fails the request it came with."""
    global _clock_offset, _clock_measured
    if not date_header:
        return
    try:
        from datetime import timezone
        from email.utils import parsedate_to_datetime
        when = parsedate_to_datetime(date_header)
        if when.tzinfo is None:          # "-0000", asctime: an HTTP-date is UTC
            when = when.replace(tzinfo=timezone.utc)
        server = when.timestamp()
    except Exception:
        return
    offset = time.time() - server
    if abs(offset) > CLOCK_OFFSET_BELIEVABLE_SEC:
        return          # a header that says it is another day entirely is not a clock
    _clock_offset = offset
    _clock_measured = True


async def _request(method: str, *, json_body: Optional[dict] = None, data: Optional[dict] = None,
                   files: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT,
                   bot_token: str = "") -> Any:
    """One Bot API call; returns its `result`. Raises ChannelError (Conflict
    for 409). `bot_token` lets the pairing poll run before an owner exists."""
    bot_token = bot_token or token()
    if not bot_token:
        raise NotConfigured("Telegram is not set up: set TELEGRAM_BOT_TOKEN in .env")
    client = await _get_client(bot_token)
    try:
        response = await client.request("POST", f"/{method}", json=json_body, data=data,
                                        files=files, timeout=timeout)
    except httpx.HTTPError as e:
        raise ChannelError(f"Could not reach Telegram: {type(e).__name__}") from None
    _note_server_clock(response.headers.get("date"))
    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}
    if not 200 <= response.status_code < 300 or not (isinstance(body, dict) and body.get("ok")):
        raise _error_of(body, response.status_code)
    return body.get("result")


# ---------------------------------------------------------------------------
# Sending — always to the owner
# ---------------------------------------------------------------------------

async def _send(method: str, payload: dict, *, kind: str, summary: str = "",
                files: Optional[dict] = None, timeout: float = REQUEST_TIMEOUT) -> dict:
    """One message to the owner. `chat_id` is set HERE and nowhere else."""
    global _last_error
    cfg = require()
    if files is not None:
        data = {"chat_id": str(cfg.owner_id), **{k: str(v) for k, v in payload.items()}}
        json_body = None
    else:
        data, json_body = None, {"chat_id": cfg.owner_id, **payload}
    try:
        result = await _request(method, json_body=json_body, data=data, files=files, timeout=timeout)
    except ChannelError as e:
        _last_error = e.report
        raise
    _last_error = None
    message_id = ""
    if isinstance(result, dict):
        message_id = str(result.get("message_id") or "")
    _claim(f"m:{message_id}" if message_id else f"out:{time.time_ns()}", "outbound", kind,
           time.time(), summary)
    return {"message_id": message_id, "kind": kind}


async def send_text(text: str, *, reply_markup: Optional[dict] = None, kind: str = "text") -> dict:
    """One message, as given (the caller has already chunked it). Plain
    text: no parse mode, so nothing the brain writes is ever markup."""
    text = _clean(text)
    if not 1 <= len(text) <= TEXT_BODY_MAX:
        raise ChannelError(f"A message is 1 to {TEXT_BODY_MAX} characters")
    payload: dict = {"text": text, "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return await _send("sendMessage", payload, kind=kind, summary=text[:120])


async def deliver(text: str) -> dict:
    """Say `text` to the owner, however long. Returns the last receipt."""
    pieces = chunks(text)
    if not pieces:
        raise ChannelError("Nothing to send")
    last: dict = {}
    total = len(pieces)
    for index, piece in enumerate(pieces, 1):
        marked = f"({index}/{total}) {piece}" if total > 1 else piece
        last = {**(await send_text(marked)), "via": "text"}
    return last


def _keyboard(buttons: list[tuple[str, str]]) -> dict:
    if not 1 <= len(buttons) <= 3:
        raise ChannelError("One to three buttons")
    row = []
    for button_id, title in buttons:
        button_id, title = _clean(button_id), _clean(title)
        if not 1 <= len(button_id.encode("utf-8")) <= CALLBACK_DATA_MAX or not 1 <= len(title) <= 40:
            raise ChannelError("A button needs a payload under 64 bytes and a short title")
        row.append({"text": title, "callback_data": button_id})
    return {"inline_keyboard": [row]}


async def send_buttons(body: str, buttons: list[tuple[str, str]]) -> dict:
    """A message with an inline keyboard of up to three buttons: (id, title)."""
    return await send_text(body, reply_markup=_keyboard(buttons), kind="buttons")


async def announce_card(action: dict, body: str, buttons: list[tuple[str, str]]) -> bool:
    """The policy's card, on this wire. Never raises."""
    try:
        await send_buttons(body, buttons)
        return True
    except ChannelError as e:
        log.warning("telegram: card not announced: %s", e.report)
        return False


async def send_document(data: bytes, filename: str, mime: str, caption: str = "") -> dict:
    """A file to the owner as it is — a hand-post's image or video, sent as
    a document so Telegram does not recompress what he will upload."""
    if not data:
        raise ChannelError("No file to send")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:80] or "file"
    payload = {"caption": caption[:1000]} if caption else {}
    return await _send("sendDocument", payload, kind="document", summary=f"(file {name})",
                       files={"document": (name, data, mime)}, timeout=MEDIA_TIMEOUT)


async def send_voice_note(audio: bytes) -> dict:
    """JARVIS's voice: `sendVoice` (the waveform bubble) for Ogg Opus,
    `sendAudio` for anything else."""
    if not audio:
        raise ChannelError("No audio to send")
    mime, voice = sniff_audio(audio)
    ext = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav"}.get(mime, "bin")
    field = "voice" if voice else "audio"
    return await _send("sendVoice" if voice else "sendAudio", {}, kind="voice" if voice else "audio",
                       summary="(voice note)", files={field: (f"jarvis.{ext}", audio, mime)},
                       timeout=MEDIA_TIMEOUT)


async def _typing() -> None:
    """The "typing…" line while the brain thinks. Best effort."""
    cfg = config()
    if cfg is None:
        return
    try:
        await _request("sendChatAction", json_body={"chat_id": cfg.owner_id, "action": "typing"})
    except ChannelError as e:
        log.debug("sendChatAction failed: %s", e)


async def _answer_callback(callback_id: str, text: str = "") -> None:
    """Dismiss the button's spinner, with a toast. Best effort."""
    if not callback_id:
        return
    try:
        await _request("answerCallbackQuery",
                       json_body={"callback_query_id": callback_id, "text": _clean(text)[:200]})
    except ChannelError as e:
        log.debug("answerCallbackQuery failed: %s", e)


async def _strip_buttons(chat_id: int, message_id: int) -> None:
    """Take the keyboard off a decided card, so a second tap has nothing to
    press. Best effort."""
    if not message_id:
        return
    try:
        await _request("editMessageReplyMarkup",
                       json_body={"chat_id": chat_id, "message_id": message_id,
                                  "reply_markup": {"inline_keyboard": []}})
    except ChannelError as e:
        log.debug("editMessageReplyMarkup failed: %s", e)


# The line-local sends the Telegram routes and tests use. The server goes
# through `messaging.reach` / `messaging.say`, which fan out to every line.

async def reach(line: str, *, voice: bool = False) -> bool:
    global _last_error
    cfg = config()
    if cfg is None:
        return False
    try:
        await deliver(line)
    except ChannelError as e:
        _last_error = e.report
        log.warning("telegram: could not reach the owner: %s", e.report)
        return False
    except Exception:
        log.warning("telegram: could not reach the owner", exc_info=True)
        return False
    if voice and cfg.voice_notes:
        audio = await messaging._synthesise(line)
        if audio:
            await messaging._voice_on(_this(), audio)
    return True


async def say(text: str, *, voice: bool = False) -> dict:
    receipt = await deliver(text)
    voice_note = False
    if voice:
        audio = await messaging._synthesise(text)
        voice_note = bool(audio) and await messaging._voice_on(_this(), audio)
    receipt["voice_note"] = voice_note
    return receipt


def _this():
    import sys
    return sys.modules[__name__]


def button_ids(action: dict) -> tuple[str, str]:
    """This line's button ids: the card's id and sixteen characters of its
    digest, under Telegram's 64-byte limit."""
    return messaging.button_ids(action, BUTTON_DIGEST_CHARS)


async def notify_card(action: dict) -> bool:
    if config() is None or not isinstance(action, dict) or action.get("state") != "pending":
        return False
    return await messaging.announce(_this(), action)


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

def _no_pairing() -> dict:
    """The pairing state before any code: nothing live, nothing decided.
    `note` is anything the owner should know about the latest code: that a
    pairing held for this run only, or that the clock looks wrong."""
    return {"code": "", "expires": 0.0, "minted_mono": 0.0, "serial": 0, "outcome": "",
            "misses": 0, "note": ""}


_pair: dict = _no_pairing()
_recent_strangers: list[dict] = []
_paired_id: Optional[int] = None


def new_pairing_code() -> dict:
    """A six-digit code, good for ten minutes, once. Shown on the local
    Settings page and nowhere else; whoever sends it to the bot from a
    private chat becomes the owner — in place of any owner there is — so the
    page is the thing being trusted. A new code replaces a live one; its
    serial is how a watcher tells ITS pairing from an older one."""
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    _pair.update(code=code, expires=time.monotonic() + PAIR_TTL_SEC, minted_mono=time.monotonic(),
                 serial=int(_pair["serial"]) + 1, outcome="", misses=0, note="")
    return pairing_status()


def _live() -> bool:
    return bool(_pair["code"]) and time.monotonic() < _pair["expires"]


def pairing_status() -> dict:
    """The live code while it is live, and how the latest code ended: ""
    while it is live (or before there was one), else "paired", "cancelled",
    "lapsed", or "locked" — withdrawn after `PAIR_MAX_MISSES` wrong guesses."""
    live = _live()
    outcome = _pair["outcome"]
    if not live and not outcome and _pair["code"]:
        outcome = "lapsed"
    return {"active": live, "code": _pair["code"] if live else "",
            "seconds_left": max(0, int(_pair["expires"] - time.monotonic())) if live else 0,
            "serial": int(_pair["serial"]), "outcome": outcome, "note": _pair["note"]}


def _end_pairing(outcome: str) -> None:
    """Close the code, recording how it ended — unless it had already
    lapsed, which is how it ended."""
    if _pair["code"] and not _pair["outcome"]:
        _pair["outcome"] = outcome if _live() else "lapsed"
    _pair.update(code="", expires=0.0)


def cancel_pairing(serial: Optional[int] = None) -> None:
    """Withdraw the live code — only if it is code number `serial`, when
    one is given, so a page abandoning its own code cannot withdraw a newer
    one somebody else minted."""
    if serial is not None and serial != int(_pair["serial"]):
        return
    _end_pairing("cancelled")


def _code_in(text: str) -> str:
    match = _CODE.fullmatch(_clean(text))
    return match.group("code") if match else ""


_NOT_THE_CODE_LINE = "That is not the pairing code, sir."
_CODE_WITHDRAWN_LINE = ("That was one wrong code too many, sir, so it has been withdrawn. "
                        "Press Pair on the Settings page for a new one.")


async def _pairing_attempt(update: "Update", code: str,
                           cfg: Optional[Config]) -> Optional[bool]:
    """A six-digit message while a code is live, in a private chat, from
    anybody — the owner too. True when it paired its sender; False when it
    was an attempt (a wrong code, counted; or the right one, too early);
    None when it cannot have been an attempt at this code at all, and is
    ordinary traffic — a turn from the owner, dropped from anybody else.

    What cannot be an attempt: a message dated before the code existed. It
    sat in Telegram's queue while nobody could know the code, so it neither
    pairs nor counts, and a backlog of blind guesses can neither pair nor
    lock a fresh code. The minting time is put on Telegram's clock first
    (`_clock_offset`), and one `PAIR_CLOCK_SLACK_SEC` applies to the right
    code and a wrong one alike. The right code dated earlier than that is
    reported (the clock reading was off), not taken. A wrong code counts
    against the code; the owner is told, a stranger is never answered."""
    global _ignored_strangers
    from_owner = cfg is not None and update.sender_id == cfg.owner_id
    right = secrets.compare_digest(code, _pair["code"])
    # When the code was made, read off THIS clock as it stands now (monotonic,
    # so a clock step since cannot move it), then put on Telegram's with the
    # offset measured against this same clock.
    minted = time.time() - (time.monotonic() - _pair["minted_mono"])
    on_telegrams_clock = minted - _clock_offset
    earliest = on_telegrams_clock - PAIR_CLOCK_SLACK_SEC
    if right and not _clock_measured:
        # No answer has said what Telegram's clock reads (a self-hosted Bot
        # API server sends no Date): this clock may run ahead, so the RIGHT
        # code keeps the wide grace. Wrong codes keep the narrow one.
        earliest = on_telegrams_clock - PAIR_UNMEASURED_SLACK_SEC
    if update.at < earliest:
        if not right:
            return None
        _pair["note"] = ("the code arrived dated before it was made — check this "
                         "computer's clock, then start pairing again")
        log.warning("telegram: the pairing code arrived dated %.0f s before it was made; "
                    "is this machine's clock ahead?", on_telegrams_clock - update.at)
        if not from_owner:
            _ignored_strangers += 1
            _remember_stranger(update)
        return False
    if right:
        await _pair_with(update.sender_id)
        return True
    _pair["misses"] = int(_pair["misses"]) + 1
    log.info("telegram: a wrong pairing code from user id %s (%d of %d)",
             update.sender_id, _pair["misses"], PAIR_MAX_MISSES)
    if not from_owner:
        _ignored_strangers += 1
        _remember_stranger(update)
    locked = _pair["misses"] >= PAIR_MAX_MISSES
    if locked:
        _end_pairing("locked")
        log.warning("telegram: pairing code withdrawn after %d wrong guesses", PAIR_MAX_MISSES)
    if from_owner:
        try:
            await deliver(_CODE_WITHDRAWN_LINE if locked else _NOT_THE_CODE_LINE)
        except ChannelError as e:
            log.warning("telegram: could not answer a wrong code: %s", e.report)
    return False


def _bind_owner(user_id: int) -> str:
    """Make `user_id` the owner: for this process at once, and in `.env`
    when that can be written. Returns "" or why `.env` was not written."""
    global _paired_id, _owner_epoch
    os.environ["TELEGRAM_OWNER_ID"] = str(user_id)
    _paired_id = user_id
    _owner_epoch += 1
    _end_pairing("paired")
    _pair["note"] = ""          # a clock warning from an earlier try is history now
    try:
        import settings_api
        settings_api._write_env_key("TELEGRAM_OWNER_ID", str(user_id))
        return ""
    except Exception as e:
        log.warning("telegram: paired for this run, but .env was not written: %s", e)
        # On the page and in the script too: this pairing ends with the run.
        _pair["note"] = f"paired for this run only; put TELEGRAM_OWNER_ID={user_id} in .env"
        return _pair["note"]


async def _pair_with(user_id: int) -> None:
    note = _bind_owner(user_id)
    log.info("telegram: paired with user id %s", user_id)
    try:
        await deliver(_PAIRED_LINE + (f" ({note}.)" if note else ""))
    except ChannelError as e:
        log.warning("telegram: paired, but the greeting failed: %s", e.report)


def forget_owner() -> str:
    """Unpair: nobody owns the line from now on — in this process at once,
    and in `.env` when that can be written — and a live code goes too. How a
    lost phone loses the line before a new one is paired. Returns "" or why
    `.env` was not written."""
    global _paired_id, _owner_epoch
    _end_pairing("cancelled")
    os.environ["TELEGRAM_OWNER_ID"] = ""
    _paired_id = None
    _owner_epoch += 1
    try:
        import settings_api
        settings_api._write_env_key("TELEGRAM_OWNER_ID", "")
        return ""
    except Exception as e:
        log.warning("telegram: unpaired for this run, but .env was not written: %s", e)
        return "unpaired for this run only; clear TELEGRAM_OWNER_ID in .env"


def _remember_stranger(update: "Update") -> None:
    """The id of somebody who wrote to the bot and is not the owner. Never
    the name: that is whatever he typed into Telegram, and the Settings page
    must not offer anybody on the strength of it."""
    entry = {"id": update.sender_id, "at": time.time()}
    _recent_strangers[:] = ([e for e in _recent_strangers if e["id"] != update.sender_id]
                            + [entry])[-STRANGERS_KEPT:]


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------

class Update(NamedTuple):
    update_id: int
    kind: str               # text | button | other | none
    sender_id: int
    sender_name: str
    chat_id: int
    chat_type: str
    message_id: int
    text: str
    callback_id: str
    callback_data: str
    at: float
    forwarded: bool = False     # somebody else's words, sent on by the owner
    reply_to: int = 0           # the message this one answers, when it is a reply


# What marks a message as not the sender's own words. Bot API 7 says
# `forward_origin`; older payloads, and some clients, the rest. `via_bot` is
# text an inline bot composed. A REPLY is not here: `reply_to_message` quotes
# a message the brain is never shown, and the text is the owner's.
_FORWARD_MARKS = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name",
                  "forward_date", "is_automatic_forward", "via_bot")


def parse_update(item: Any) -> Optional[Update]:
    """One update as `getUpdates` returns it, or None for a shape this line
    does not read (an edited message, a channel post, a member joining)."""
    if not isinstance(item, dict):
        return None
    try:
        update_id = int(item.get("update_id"))
    except (TypeError, ValueError):
        return None
    message = item.get("message")
    callback = item.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        origin = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = origin.get("chat") if isinstance(origin.get("chat"), dict) else {}
        return Update(update_id, "button", _int(sender.get("id")), _who(sender),
                      _int(chat.get("id")), str(chat.get("type") or ""),
                      _int(origin.get("message_id")), "", str(callback.get("id") or ""),
                      _clean(callback.get("data")), time.time())
    if isinstance(message, dict):
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        text = message.get("text")
        kind = "text" if isinstance(text, str) and text.strip() else "other"
        return Update(update_id, kind, _int(sender.get("id")), _who(sender),
                      _int(chat.get("id")), str(chat.get("type") or ""),
                      _int(message.get("message_id")), _clean(text) if kind == "text" else "",
                      "", "", float(message.get("date") or time.time()),
                      any(message.get(mark) for mark in _FORWARD_MARKS),
                      _int((message.get("reply_to_message") or {}).get("message_id"))
                      if isinstance(message.get("reply_to_message"), dict) else 0)
    return Update(update_id, "none", 0, "", 0, "", 0, "", "", "", time.time())


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _who(sender: dict) -> str:
    name = " ".join(str(sender.get(k) or "") for k in ("first_name", "last_name")).strip()
    handle = str(sender.get("username") or "")
    return _clean(f"{name} (@{handle})" if name and handle else name or (f"@{handle}" if handle else ""))


_poller: Optional[asyncio.Task] = None
_last_poll: Optional[float] = None
_ignored_strangers = 0
_errors_in_a_row = 0
_webhook_cleared = False
_bot_username: str = ""
_polled_token: str = ""
# What the poll itself last ran into, cleared by the next poll that works —
# unlike `_last_error`, which is the last failure of any kind on the line.
_poll_error: Optional[str] = None


def _notice_token(bot_token: str) -> None:
    """A different token is a different bot: forget what was learnt about
    the last one — its name, its webhook, its run of errors — so the new one
    is neither named wrongly nor made to wait out the old one's backoff."""
    global _polled_token, _errors_in_a_row, _webhook_cleared, _bot_username, _last_error
    global _poll_error
    if bot_token == _polled_token:
        return
    _polled_token = bot_token
    _errors_in_a_row = 0
    _webhook_cleared = False
    _bot_username = ""
    _last_error = None
    _poll_error = None


_NOT_RUNNING_LINE = ("JARVIS is not running just now, sir — only pairing is. Send that again "
                     "once he is.")


async def poll_once(*, timeout: int = POLL_TIMEOUT_SEC, pairing_only: bool = False) -> int:
    """One `getUpdates`: act on each new update once, move the offset.
    Returns how many were acted on. Runs on the token alone, so pairing
    works before there is an owner.

    Nothing here waits for a brain turn: the policy runs the owner's turns
    beside the poll (`messaging.on_text`), so a tap on a card the turn is
    holding is read while it holds it.

    `pairing_only` is `scripts/telegram_setup.py` pairing with the server
    down: a code is still a code, and anything else from the owner is only
    told that JARVIS is not running — nothing decides a card or starts a
    turn in a process that has no brain."""
    global _last_poll, _ignored_strangers
    bot_token = token()
    if not bot_token:
        raise NotConfigured("Telegram is not set up: set TELEGRAM_BOT_TOKEN in .env")
    offset = _int(_state_get(_offset_key(bot_token)))
    result = await _request("getUpdates", json_body={
        "offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
        timeout=POLL_REQUEST_TIMEOUT if timeout else REQUEST_TIMEOUT, bot_token=bot_token)
    _last_poll = time.time()
    items = result if isinstance(result, list) else []
    handled = 0
    newest = offset
    for item in items:
        update = parse_update(item)
        if update is None:
            continue
        newest = max(newest, update.update_id + 1)
        if not _claim(_update_ref(bot_token, update.update_id), "inbound", update.kind, update.at):
            continue
        if update.kind == "none":
            continue
        cfg = config()
        # While a code is live, a six-digit message in a private chat is a
        # pairing attempt — checked before anything else, paired or not,
        # whoever sent it: sending the code is how a phone takes the line,
        # the owner's own phone included, and it is never a turn.
        if update.kind == "text" and _live() and update.chat_type == "private" \
                and update.sender_id:
            code = _code_in(update.text)
            attempt = await _pairing_attempt(update, code, cfg) if code else None
            if attempt is not None:
                handled += 1 if attempt else 0
                continue
        if cfg is None or update.sender_id != cfg.owner_id or update.chat_type != "private":
            _ignored_strangers += 1
            _remember_stranger(update)
            log.info("telegram: ignored a message from user id %s%s", update.sender_id,
                     " while unpaired" if cfg is None else "")
            continue
        if pairing_only:
            if update.kind == "button":
                await _answer_callback(update.callback_id, "JARVIS is not running")
            try:
                await deliver(_NOT_RUNNING_LINE)
            except ChannelError as e:
                log.warning("telegram: could not say JARVIS is not running: %s", e.report)
            continue
        handled += 1
        try:
            # Who the sender was just matched as — taken here, before any
            # await, and carried to everything the message does.
            await handle(update, owner=owner_key())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("telegram: handling an update failed", exc_info=True)
    if newest > offset:
        _state_set(_offset_key(bot_token), str(newest))
    return handled


async def handle(update: Update, owner: Optional[tuple] = None) -> None:
    """Act on one update from the OWNER (the caller has checked that, and
    passes the `owner_key` it matched): the policy decides, this line reads
    it back, answers the tap, and strips the buttons off a card once it is
    decided. The policy re-checks `owner` after this line's own awaits."""
    me = _this()
    owner = owner if owner is not None else owner_key()
    if update.kind == "button":
        outcome = await messaging.on_button(me, update.callback_data, owner=owner)
        await _answer_callback(update.callback_id,
                               "Done" if outcome.get("decided") else "Nothing changed")
        if outcome.get("decided"):
            await _strip_buttons(update.chat_id, update.message_id)
        return
    if update.kind != "text":
        await messaging.on_other(me)
        return
    if not update.forwarded and _clean(update.text).lower().split()[:1] == ["/start"]:
        await deliver(_GREETING)
        return
    if update.reply_to and not update.forwarded:
        # The owner answering a hand-post with the post's link: that reply is
        # his say-so to write it on the card, and needs no brain turn.
        import linkedin_handpost
        try:
            card = linkedin_handpost.record_reply(update.reply_to, update.text)
        except Exception:
            log.warning("telegram: could not record a hand-post link", exc_info=True)
            card = None
        if card is not None:
            await deliver(f"Recorded, sir: {card['result'].get('post_url')} is on card {card['id'][:8]}.")
            return
    await _typing()
    await messaging.on_text(me, update.text, f"tg:{update.chat_id}:{update.message_id}",
                            forwarded=update.forwarded, owner=owner)


async def _clear_webhook(bot_token: str) -> None:
    """A webhook set on this bot elsewhere makes every getUpdates a 409.
    Once per start; the answer is not needed."""
    global _webhook_cleared
    if _webhook_cleared:
        return
    try:
        await _request("deleteWebhook", json_body={"drop_pending_updates": False}, bot_token=bot_token)
    except ChannelError as e:
        log.debug("deleteWebhook failed: %s", e)
    _webhook_cleared = True


async def _learn_bot_name(bot_token: str) -> None:
    global _bot_username
    if _bot_username:
        return
    try:
        me = await _request("getMe", bot_token=bot_token)
        if isinstance(me, dict):
            _bot_username = _clean(me.get("username"))[:64]
    except ChannelError as e:
        log.debug("getMe failed: %s", e)


async def check_token() -> None:
    """Ask Telegram whether the saved token is a bot it knows, and learn
    the bot's name on the way. Raises ChannelError when Telegram refuses the
    token (401, 404); any other failure — no network — says nothing about
    the token, and passes."""
    global _bot_username
    bot_token = token()
    if not bot_token:
        raise NotConfigured(issue() or "Set TELEGRAM_BOT_TOKEN first")
    _notice_token(bot_token)
    try:
        me = await _request("getMe", bot_token=bot_token)
    except ChannelError as e:
        if e.status in (401, 404):
            raise
        log.debug("getMe failed: %s", e)
        return
    if isinstance(me, dict):
        _bot_username = _clean(me.get("username"))[:64]


async def _rest(delay: float, bot_token: str, *, step: float = 1.0) -> None:
    """Sleep `delay`, or less: a token saved in the meantime ends the wait,
    so a corrected token is not made to sit out the wrong one's backoff."""
    deadline = time.monotonic() + delay
    while True:
        left = deadline - time.monotonic()
        if left <= 0 or token() != bot_token:
            return
        await asyncio.sleep(min(step, left))


async def _poll_forever() -> None:
    global _errors_in_a_row, _last_error, _webhook_cleared, _poll_error
    while True:
        bot_token = token()
        if not bot_token:
            await asyncio.sleep(IDLE_SEC)
            continue
        _notice_token(bot_token)
        try:
            await _clear_webhook(bot_token)
            await _learn_bot_name(bot_token)
            began = time.monotonic()
            handled = await poll_once()
            _errors_in_a_row = 0
            _poll_error = None
            # Telegram answered, so a card it could not take while it was
            # unreachable can go now (2026-10-01: "Don't see it").
            await messaging.retry_unannounced(_this())
            # Long polling: straight back in — Telegram held the request
            # until something arrived or the timeout ran out. A server that
            # answers an empty poll at once (a proxy, a fake) would turn that
            # into a busy loop, so an instant empty answer costs a second.
            if not handled and time.monotonic() - began < 1.0:
                await asyncio.sleep(1.0)
            continue
        except asyncio.CancelledError:
            raise
        except Conflict as e:
            _errors_in_a_row += 1
            _last_error = _poll_error = e.report
            _webhook_cleared = False     # try deleting it again next round
            log.warning("telegram: another poller or a webhook holds this bot (%d in a row)",
                        _errors_in_a_row)
        except ChannelError as e:
            _errors_in_a_row += 1
            _last_error = _poll_error = e.report
            log.warning("telegram: poll failed (%d in a row): %s", _errors_in_a_row, e.report)
        except Exception:
            _errors_in_a_row += 1
            _poll_error = "the poll failed; see the server log"
            log.warning("telegram: poll failed (%d in a row)", _errors_in_a_row, exc_info=True)
        await _rest(min(300.0, IDLE_SEC * (2 ** min(_errors_in_a_row, 4))), bot_token)


def start(*, poll: bool = True) -> Optional[asyncio.Task]:
    """This line's poller. Returns the task, or None when nothing is to be
    polled. Safe to call unconfigured: it sleeps until a token arrives."""
    global _poller
    init_db()
    if not poll:
        return None
    if _poller is not None and not _poller.done():
        return _poller
    _poller = asyncio.create_task(_poll_forever(), name="telegram-poll")
    return _poller


async def stop() -> None:
    global _poller
    task, _poller = _poller, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await close()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status() -> dict:
    """What the settings panel, the preflight check and the diagnostics
    page show. The token is never in it; the owner's id is (it is needed to
    finish a setup by hand, and it is not a secret), and so are the ids of
    the last few strangers — ids only, see `_remember_stranger`."""
    cfg = config()
    last_in = _last("inbound") if _tables_exist() else None
    last_out = _last("outbound") if _tables_exist() else None
    return {
        "configured": cfg is not None,
        "touched": touched(),
        "missing": missing(),
        "issue": issue(),
        "token_set": bool(env("TELEGRAM_BOT_TOKEN")),
        "owner_id": env("TELEGRAM_OWNER_ID"),
        "bot_username": _bot_username,
        # Never the code itself: this is a GET, and anything that can read
        # a page on this machine — the brain's own `read_page` included —
        # could then send the code before the phone meant to. The code goes
        # only to whoever minted it (POST /api/telegram/pair).
        "pairing": {k: v for k, v in pairing_status().items() if k != "code"},
        "approvals": approvals(),
        "voice_notes": voice_notes(),
        "polling": _poller is not None and not _poller.done(),
        "last_poll": _last_poll,
        "last_sent": last_out,
        "last_received": last_in,
        "last_error": _last_error,
        "poll_error": _poll_error,
        "ignored_strangers": _ignored_strangers,
        "recent_strangers": list(_recent_strangers),
        "calls": ("Not available: a Telegram bot cannot place calls. Urgent lines go as a "
                  "voice note instead; a ringing phone call is the Twilio provider on the "
                  "Business desk."),
    }


def _tables_exist() -> bool:
    try:
        with closing(_connect()) as conn:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                "name='telegram_messages'").fetchone() is not None
    except sqlite3.Error:
        return False
