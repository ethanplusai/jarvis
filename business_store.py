"""Durable business records and single-attempt, immutable action approvals.

The same database as run history makes backup/restore cover the whole ledger.
No network call happens while a database transaction is held.
"""
from contextlib import closing
import hashlib
import json
import logging
import sqlite3
import time
import uuid

from data_paths import db_path
import schema

log = logging.getLogger("jarvis.business")

# Called with the new card after `propose` has committed it, from whichever
# door staged it — the brain's `business_propose`, the PreToolUse gate, or
# the desk's own route. This is how the owner's phone learns that something
# is waiting (`server._whatsapp_card_hook`). A hook runs AFTER the
# transaction, never inside it, and one that raises costs nothing but a log
# line: staging a card must not depend on the phone.
ON_PROPOSED: list = []


def connect():
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with closing(connect()) as conn:
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS business_actions (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            provider TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
            digest TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
            expires REAL NOT NULL, updated REAL NOT NULL, result TEXT NOT NULL DEFAULT '{}');
          CREATE INDEX IF NOT EXISTS business_action_state ON business_actions(state, seq);
          CREATE TABLE IF NOT EXISTS business_audit (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL,
            event TEXT NOT NULL, at REAL NOT NULL,
            FOREIGN KEY(action_id) REFERENCES business_actions(id));
          CREATE TABLE IF NOT EXISTS business_records (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL, body TEXT NOT NULL, version INTEGER NOT NULL,
            updated REAL NOT NULL);
        """)
        # `deleted` arrived after installs already had the table. A finished
        # approval the user removed from the desk keeps its row (the audit
        # table points at it, and the export is the ledger) and gains a
        # stamp here; every desk-facing query filters on it.
        schema.ensure_columns(conn, "business_actions", {"deleted": "REAL"})


# An approval that can no longer do anything: it went out (or failed to),
# was refused, is in doubt, or lapsed unspent. Only these may be deleted from
# the desk; a live one is rejected instead, and that is recorded.
_FINISHED = ("(state IN ('submitted','failed','rejected','unknown') "
             "OR (state IN ('pending','approved') AND expires<=?))")


def _action(row):
    if row is None:
        raise ValueError("Action not found")
    value = dict(row)
    for key in ("payload", "result"):
        value[key] = json.loads(value[key])
    return value


def get_action(action_id):
    with closing(connect()) as conn:
        return _action(conn.execute("SELECT * FROM business_actions WHERE id=?", (action_id,)).fetchone())


def _encode(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical(payload):
    """The exact text a payload is stored as, and its digest computed over."""
    return _encode(payload)


def propose_digest(provider, operation, payload):
    """What `propose` WOULD store this call as, without storing it.

    The PreToolUse gate asks "has the user already approved exactly this
    call?" before deciding whether to stage a new one. It has to compute the
    same digest `propose` does, so the two share one formula here rather
    than spelling it out twice and drifting apart.
    """
    return hashlib.sha256(
        f"{provider}\n{operation}\n{_encode(payload)}".encode()).hexdigest()


def propose(provider, operation, payload):
    encoded = _encode(payload)
    digest = propose_digest(provider, operation, payload)
    now = time.time()
    action_id = str(uuid.uuid4())
    with closing(connect()) as conn, conn:
        conn.execute("INSERT INTO business_actions(id,provider,operation,payload,digest,state,created,expires,updated) "
                     "VALUES(?,?,?,?,?,'pending',?,?,?)",
                     (action_id, provider, operation, encoded, digest, now, now + 86400, now))
        conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,'proposed',?)", (action_id, now))
    action = get_action(action_id)
    for hook in list(ON_PROPOSED):
        try:
            hook(action)
        except Exception:
            log.warning("a proposal hook failed", exc_info=True)
    return action


def note(action_id, event):
    """One more line in a card's audit trail — where a decision came from
    (`via:whatsapp`) — without touching its state."""
    with closing(connect()) as conn, conn:
        conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,?,?)",
                     (action_id, event, time.time()))


def find_by_digest(digest, state):
    """The one action with this digest in this state, or None.

    Read-only, and the lookup the PreToolUse gate needs: "has the user
    already approved exactly this call?" Digest, not id, because the brain
    re-emits a call rather than remembering what it was told to stage.
    """
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT seq,id,provider,operation,payload,digest,state,created,expires,updated,result "
            "FROM business_actions WHERE digest=? AND state=? AND deleted IS NULL "
            "ORDER BY seq DESC LIMIT 1",
            (digest, state)).fetchone()
    return _action(row) if row else None


def latest_by_digest(digest):
    """The newest action with this digest in any state, or None.

    Read-only. The gate asks it "is a lapsed approval the last thing that
    happened to these bytes?", so the lapse is mentioned once, when the call
    is put to the user afresh, and not on every later retry. Deleted rows
    count, both ways: clearing a lapsed approval off the desk is not being
    told about it, and deleting the card that told the user does not un-say it.
    """
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT seq,id,provider,operation,payload,digest,state,created,expires,updated,result "
            "FROM business_actions WHERE digest=? ORDER BY seq DESC LIMIT 1",
            (digest,)).fetchone()
    return _action(row) if row else None


def sent_before(digest, before_seq=None):
    """The newest card with this digest that was released — `submitted` —
    before card `before_seq` (or at all), or None.

    Read-only. What makes a second card for the same bytes a REPEAT: on
    2026-10-01 an identical post was put to the owner three hours after the
    first, and nothing on the new card said so. Deleted cards count — the
    desk was cleared in between, and clearing a card does not unsend it.
    """
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT seq,id,provider,operation,payload,digest,state,created,expires,updated,result "
            "FROM business_actions WHERE digest=? AND state='submitted' AND seq<? "
            "ORDER BY seq DESC LIMIT 1",
            (digest, 9223372036854775807 if before_seq is None else int(before_seq))).fetchone()
    return _action(row) if row else None


def record_outcome(action_id, outcome):
    """Write what a released connector call came back with onto its card.

    Only a `submitted` card — one the gate let through — and never a state
    change: the card stays `submitted` (let through), and its result says
    what the connector made of it (`tool_outcome.summarise`). Returns the
    card, or None when it is not one this applies to."""
    now = time.time()
    with closing(connect()) as conn, conn:
        cursor = conn.execute(
            "UPDATE business_actions SET result=? WHERE id=? AND state='submitted' "
            "AND provider LIKE 'connector:%'",
            (json.dumps(outcome or {}, allow_nan=False, default=str), action_id))
        if cursor.rowcount != 1:
            return None
        status = str((outcome or {}).get("status") or "done")[:40]
        conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,?,?)",
                     (action_id, f"outcome:{status}", now))
    return get_action(action_id)


def add_link(action_id, provider, post_url, message, event):
    """Add a post's link to a sent card's result, keeping what is there —
    the hand-post's own delivery receipt. Only a `submitted` card of
    `provider`. Returns the card, or None."""
    now = time.time()
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT result FROM business_actions WHERE id=? AND provider=? AND state='submitted'",
                           (action_id, provider)).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row[0] or "{}")
        except ValueError:
            result = {}
        result.update({"post_url": post_url, "message": message})
        conn.execute("UPDATE business_actions SET result=? WHERE id=?",
                     (json.dumps(result, allow_nan=False), action_id))
        conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,?,?)",
                     (action_id, str(event)[:60], now))
    return get_action(action_id)


def released_cards(provider, since):
    """Connector cards on `provider` released since `since`, newest first,
    deleted ones included: where a link found afterwards may belong."""
    with closing(connect()) as conn:
        return [_action(row) for row in conn.execute(
            "SELECT seq,id,provider,operation,payload,digest,state,created,expires,updated,result "
            "FROM business_actions WHERE provider=? AND state='submitted' AND updated>=? "
            "ORDER BY seq DESC LIMIT 20", (provider, float(since)))]


def transition(action_id, digest, source, target, result=None):
    """Compare-and-swap authorization: a concurrent click can never execute twice."""
    now = time.time()
    with closing(connect()) as conn, conn:
        cursor = conn.execute("UPDATE business_actions SET state=?,updated=?,result=? "
                              "WHERE id=? AND digest=? AND state=? "
                              "AND (expires>? OR state!='pending')",
                              (target, now, json.dumps(result or {}, allow_nan=False),
                               action_id, digest, source, now))
        if cursor.rowcount != 1:
            raise ValueError("Action changed, expired, or was already submitted; refresh its receipt")
        conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,?,?)", (action_id, target, now))
    return get_action(action_id)


def recover_interrupted():
    """A crash after send may have charged money: NEVER retry automatically."""
    with closing(connect()) as conn, conn:
        rows = conn.execute("SELECT id FROM business_actions WHERE state='executing'").fetchall()
        for row in rows:
            conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,'unknown',?)", (row[0], time.time()))
        conn.execute("UPDATE business_actions SET state='unknown',updated=?,result=? WHERE state='executing'",
                     (time.time(), json.dumps({"message": "Interrupted. Check provider records before creating another action."})))


# A card that can still do something: waiting for the user, or approved and
# not yet spent, inside its window, and still on the desk. `_FINISHED`'s
# complement for the states that matter to the brain.
_LIVE = "(state IN ('pending','approved') AND expires>? AND deleted IS NULL)"

# What the brain is told about a card without being shown its payload. The
# payload is read only so business_api can say how long it is AS SHOWN
# (its display form is not the stored text's length), then dropped; and the
# outcome's message where one was recorded.
_SUMMARY = ("seq,id,provider,operation,state,created,expires,updated,payload,"
            "json_extract(result,'$.message') AS message")


def live_actions():
    """Every live card — approved ones first, then those waiting for the
    user, each newest first — the list the brain must always be able to see.
    Bounded by the 24-hour window rather than a LIMIT: a count that could be
    cut short is the failure this exists to prevent.

    Approved first because the loop of 2026-09-25/26 re-emitted slightly
    different calls, and the gate staged each as a new PENDING card: newest
    first put the one card the user had said yes to behind its own
    re-worded copies, where the cap could leave it off the first page."""
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(
            f"SELECT {_SUMMARY} FROM business_actions WHERE {_LIVE} "
            "ORDER BY CASE state WHEN 'approved' THEN 0 ELSE 1 END, seq DESC",
            (time.time(),))]


def action_summaries(before=9223372036854775807, limit=50):
    """Every card on the desk, newest first, for summarising."""
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(
            f"SELECT {_SUMMARY} FROM business_actions WHERE seq<? AND deleted IS NULL "
            "ORDER BY seq DESC LIMIT ?", (before, limit))]


def find_actions(ref, limit=6):
    """Cards on the desk whose id is `ref` or starts with it, newest first.

    `substr`, not LIKE: an id prefix is matched as the characters it is,
    so `%` and `_` are never wildcards. A deleted card is not found — the
    brain reads what the desk shows, and nothing the user took off it.
    """
    with closing(connect()) as conn:
        return [_action(row) for row in conn.execute(
            "SELECT seq,id,provider,operation,payload,state,created,expires,updated,result "
            "FROM business_actions WHERE deleted IS NULL AND substr(id,1,?)=? "
            "ORDER BY seq DESC LIMIT ?", (len(ref), ref, limit))]


def list_actions(before=9223372036854775807, limit=50):
    with closing(connect()) as conn:
        return [_action(row) for row in conn.execute(
            "SELECT seq,id,provider,operation,payload,digest,state,created,expires,updated,"
            "json_object('message',COALESCE(json_extract(result,'$.message'),'Open receipt for provider details')) AS result "
            "FROM business_actions WHERE seq<? AND deleted IS NULL ORDER BY seq DESC LIMIT ?",
            (before, limit))]


def delete_action(action_id):
    """Take a FINISHED approval off the desk. Soft: the row and its audit
    trail stay (the export is the ledger), it gains a `deleted` stamp and a
    `deleted` audit event, and the desk, the briefing's counts and the
    connector gate's digest lookups stop seeing it. Idempotent. A live one
    — pending, executing, or an unspent allowance — raises: reject it
    instead, which is recorded as an answer."""
    now = time.time()
    with closing(connect()) as conn, conn:
        row = conn.execute("SELECT state,expires,deleted FROM business_actions WHERE id=?",
                           (action_id,)).fetchone()
        if row is None:
            raise ValueError("Action not found")
        if row["deleted"] is not None:
            return get_action(action_id)
        finished = conn.execute(
            f"SELECT 1 FROM business_actions WHERE id=? AND {_FINISHED}", (action_id, now)).fetchone()
        if finished is None:
            raise ValueError("Action is still live; reject it rather than deleting it")
        conn.execute("UPDATE business_actions SET deleted=? WHERE id=? AND deleted IS NULL", (now, action_id))
        conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,'deleted',?)", (action_id, now))
    return get_action(action_id)


def clear_completed():
    """`delete_action` for every finished approval at once. Returns how many."""
    now = time.time()
    with closing(connect()) as conn, conn:
        ids = [row[0] for row in conn.execute(
            f"SELECT id FROM business_actions WHERE deleted IS NULL AND {_FINISHED}", (now,))]
        for action_id in ids:
            conn.execute("UPDATE business_actions SET deleted=? WHERE id=?", (now, action_id))
            conn.execute("INSERT INTO business_audit(action_id,event,at) VALUES(?,'deleted',?)", (action_id, now))
    return len(ids)


# A card finished longer ago than this is history. The desk keeps it; a new
# brain generation is told only about what it may still have to act on or
# answer for.
DESK_RECENT_SEC = 86400


# A card that can still do something, for the launch prompt: waiting for the
# user, approved and not yet spent inside the window the gate honours
# (`server.internal_pretool`'s `still_current`), or being sent right now.
# Wider than `_LIVE` above, which is business_status's list and leaves out a
# provider card mid-send; a brain that inherits one should know it is going.
_DESK_LIVE = "(state='executing' OR (state IN ('pending','approved') AND expires>?))"

# Within the live cards, the one the user has already said yes to leads: the
# loop this was written after re-emitted slightly different calls, and the
# gate staged each as a new PENDING card — live too, and newer — so newest
# first put the approved card behind its own re-worded copies.
_DESK_LIVE_RANK = "CASE state WHEN 'approved' THEN 0 WHEN 'executing' THEN 1 ELSE 2 END"


def desk_cards(now=None, recent_sec=DESK_RECENT_SEC, limit=12):
    """The cards a new brain generation should be told about: live ones
    first (approved, then executing, then pending, as `_DESK_LIVE_RANK` puts
    them), then any that changed in the last `recent_sec`, each group newest
    first. Deleted cards are gone. Live first, so a card the user approved and the brain
    has still to send is never pushed out of a short list by the refusals
    and sends that came after it.

    Names, states and times only — never the payload, the digest or the
    provider's result. The request is what a model composed, and it goes to
    the brain only on an explicit `business_action` read, where it arrives as
    tool output inside an untrusted block rather than as launch-prompt prose.

    A pending or approved card past its window is reported as `lapsed`:
    the gate will not honour it, and asking again stages a new one.
    """
    now = time.time() if now is None else now
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id,provider,operation,state,created,expires,updated FROM business_actions "
            f"WHERE deleted IS NULL AND ({_DESK_LIVE} OR updated>?) "
            f"ORDER BY CASE WHEN {_DESK_LIVE} THEN {_DESK_LIVE_RANK} ELSE 3 END, "
            "seq DESC LIMIT ?",
            (now, now - recent_sec, now, int(limit))).fetchall()
    cards = []
    for row in rows:
        card = dict(row)
        if card["state"] in ("pending", "approved") and card["expires"] <= now:
            card["state"] = "lapsed"
        cards.append(card)
    return cards


def audit(action_id):
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT event,at FROM business_audit WHERE action_id=? ORDER BY seq", (action_id,))]


def save_record(kind, body, record_id=None, version=None):
    encoded = json.dumps(body, allow_nan=False)
    with closing(connect()) as conn, conn:
        if record_id:
            cursor = conn.execute("UPDATE business_records SET body=?,version=version+1,updated=? "
                                  "WHERE id=? AND kind=? AND version=?",
                                  (encoded, time.time(), record_id, kind, version))
            if cursor.rowcount != 1:
                raise ValueError("Record changed or no longer exists; refresh before editing")
        else:
            record_id = str(uuid.uuid4())
            conn.execute("INSERT INTO business_records(id,kind,body,version,updated) VALUES(?,?,?,1,?)",
                         (record_id, kind, encoded, time.time()))
        row = dict(conn.execute("SELECT * FROM business_records WHERE id=?", (record_id,)).fetchone())
        row["body"] = json.loads(row["body"])
        return row


def list_records(kind, before=9223372036854775807, limit=100):
    with closing(connect()) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM business_records WHERE kind=? AND seq<? ORDER BY seq DESC LIMIT ?", (kind, before, limit))]
    for row in rows:
        row["body"] = json.loads(row["body"])
    return rows


class DamagedRecord(ValueError):
    """A record row that cannot be read as one: text that is not UTF-8, or a
    body that is not JSON or is nested past what the parser can take. A
    restored backup or a hand-edited database can hold one; nothing here
    writes one. Its message never quotes the row."""


def _readable(row):
    """A row fetched with `text_factory=bytes`, decoded strictly, its body
    parsed — or DamagedRecord. sqlite3's own decoding error QUOTES the text
    it could not decode ("Could not decode to UTF-8 column 'body' with text
    '…'"), and the brain's record tools are exempt from the untrusted-text
    mark, so the bytes are fetched raw and judged here instead."""
    try:
        row = {key: value.decode("utf-8") if isinstance(value, bytes) else value
               for key, value in dict(row).items()}
        row["body"] = json.loads(row["body"])
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        raise DamagedRecord("Record cannot be read") from None
    return row


def get_record(record_id):
    with closing(connect()) as conn:
        conn.text_factory = bytes
        row = conn.execute("SELECT * FROM business_records WHERE id=?", (record_id,)).fetchone()
    if row is None:
        raise ValueError("Record changed or no longer exists; refresh before editing")
    return _readable(row)


def iter_records(kind=None):
    """Every record (of one kind, or all), newest first, one row at a time.

    For a search that has to consider every record, not the newest page, in
    bounded memory. A row that cannot be read comes back as `{"damaged":
    True}` and nothing else of it, rather than stopping the scan or quoting
    itself in an exception."""
    query = "SELECT * FROM business_records" + (" WHERE kind=?" if kind else "") + " ORDER BY seq DESC"
    with closing(connect()) as conn:
        conn.text_factory = bytes
        for row in conn.execute(query, (kind,) if kind else ()):
            try:
                yield _readable(row)
            except DamagedRecord:
                yield {"damaged": True}


def count_records():
    """How many records of each kind there are; kinds with none are absent."""
    with closing(connect()) as conn:
        return {row[0]: row[1] for row in conn.execute(
            "SELECT kind,COUNT(*) FROM business_records GROUP BY kind")}


def export_rows():
    """Streaming export; never load the complete history into memory."""
    with closing(connect()) as conn:
        for table in ("business_actions", "business_audit", "business_records"):
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY seq"):
                yield json.dumps({"table": table, **dict(row)}, allow_nan=False) + "\n"


def briefing():
    """Scan records with bounded memory; never add amounts in different currencies."""
    from datetime import date
    today = date.today().isoformat()
    result = {"open_tasks": 0, "overdue_tasks": 0, "leads": 0, "receivables_minor": {}, "unpaid_expenses_minor": {}}
    with closing(connect()) as conn:
        for row in conn.execute("SELECT kind,body FROM business_records"):
            body = json.loads(row["body"])
            state = body.get("status")
            if row["kind"] == "task" and state == "open":
                result["open_tasks"] += 1
                if body.get("due") and body["due"] < today:
                    result["overdue_tasks"] += 1
            if row["kind"] == "contact" and state in {"lead", "qualified"}:
                result["leads"] += 1
            bucket = ("receivables_minor" if row["kind"] == "invoice" and state == "sent" else
                      "unpaid_expenses_minor" if row["kind"] == "expense" and state == "open" else None)
            if bucket:
                currency = body["currency"]
                result[bucket][currency] = result[bucket].get(currency, 0) + body["amount_minor"]
        result["actions"] = {row[0]: row[1] for row in conn.execute(
            "SELECT state,COUNT(*) FROM business_actions WHERE deleted IS NULL GROUP BY state")}
    # A computed snapshot has no row of its own to take a time from; the desk
    # shows a clock on everything, so the moment it was assembled goes with it.
    result["generated_at"] = time.time()
    return result
