"""Business operations boundary: reviewable proposals, approved execution, local CRM."""
import asyncio
from datetime import datetime
import functools
import json
import logging
import os
import re
import time
import unicodedata
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import business_providers as providers
import business_store as store
import data_paths
import web_auth

router = APIRouter(prefix="/api/business", tags=["business"])
log = logging.getLogger("jarvis")
_closing = False
_inflight = set()


def start():
    global _closing
    _closing = False


async def shutdown():
    """Stop provider work before the server releases its database runtime lock."""
    global _closing
    _closing = True
    pending = [task for task in _inflight if task is not asyncio.current_task() and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["google", "meta", "twilio", "chatgpt", "linkedin", "linkedin_hand"]
    operation: str = Field(min_length=1, max_length=100)
    payload: dict


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approve: bool


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["task", "contact", "invoice", "expense"]
    title: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=10000)
    status: Literal["open", "done", "lead", "qualified", "won", "lost", "draft", "sent", "paid", "void"] = "open"
    due: str = Field(default="", pattern=r"^(?:\d{4}-\d{2}-\d{2})?$")
    amount_minor: int = Field(default=0, ge=0, le=10**12, strict=True)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    contact: str = Field(default="", max_length=300)
    id: str | None = None
    version: int | None = Field(default=None, ge=1)


def _linkedin_allows(operation, request):
    """LinkedIn's interim limits and stop (`linkedin_guard`), at the desk:
    raised as the refusal the owner or the brain reads. Asked when a card is
    staged and again before it is sent, so an approval never spends past
    the limit — the card stays pending until the limit allows."""
    import linkedin_guard
    stopped = linkedin_guard.halted()
    if stopped is not None:
        raise ValueError(linkedin_guard.halted_reason(stopped))
    kind = linkedin_guard.provider_kind(operation)
    if kind:
        verdict = linkedin_guard.check(kind, (request or {}).get("account", "member"))
        if not verdict.ok:
            raise ValueError(f"{verdict.reason} It can go after {linkedin_guard.when(verdict.next_at)}.")


def propose(model: Proposal):
    providers.validate(model.provider, model.operation, model.payload)
    if model.provider == "linkedin":
        _linkedin_allows(model.operation, model.payload)
    target = providers.identity(model.provider)
    return store.propose(model.provider, model.operation, {"target": target, "request": model.payload})


def public_result(value):
    """Some providers echo credentials inside paging URLs. Never persist those."""
    if isinstance(value, dict):
        return {key: public_result(child) for key, child in value.items()
                if key not in {"paging", "next_page_uri", "previous_page_uri"}
                and not any(word in key.lower() for word in ("token", "secret", "authorization", "password"))}
    if isinstance(value, list):
        return [public_result(child) for child in value]
    if isinstance(value, str):
        for keys in providers.REQUIRED.values():
            for key in keys:
                if any(word in key for word in ("TOKEN", "SECRET", "KEY")):
                    secret = providers.env(key)
                    if secret:
                        value = value.replace(secret, "[redacted]")
    return value


async def execute(action_id, digest):
    if _closing:
        raise ValueError("JARVIS is shutting down; refresh after restart")
    task = asyncio.current_task()
    _inflight.add(task)
    try:
        return await _execute(action_id, digest)
    finally:
        _inflight.discard(task)


async def _execute(action_id, digest):
    action = store.get_action(action_id)
    provider, operation = action["provider"], action["operation"]
    request = action["payload"]["request"]
    providers.validate(provider, operation, request)
    if providers.identity(provider) != action["payload"]["target"]:
        raise ValueError("Provider account or call destination changed. Create a new proposal.")
    if not providers.capabilities()[provider]["configured"]:
        raise ValueError("Provider credentials are missing; configure them before approving")
    if provider == "linkedin":
        _linkedin_allows(operation, request)
    store.transition(action_id, digest, "pending", "executing")
    try:
        receipt = await providers.perform(provider, operation, request)
    except providers.ProviderError as error:
        return store.transition(action_id, digest, "executing", "unknown" if error.uncertain else "failed",
                                {"message": str(error)})
    except BaseException as error:
        store.transition(action_id, digest, "executing", "unknown",
                         {"message": "Execution interrupted. Check provider records before creating another action."})
        if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return store.get_action(action_id)
    result = {"receipt": public_result(receipt)}
    if provider == "linkedin":
        import linkedin_api
        # Submitted is a post here, not a provider's queue: say it, with its link.
        result["message"] = linkedin_api.summary(receipt, operation)
    elif provider == "linkedin_hand":
        import linkedin_handpost
        result["message"] = linkedin_handpost.summary(receipt)
    return store.transition(action_id, digest, "executing", "submitted", result)


@router.get("/connections")
def connections():
    return {"providers": providers.capabilities(), "approval_required": True,
            "spending": "Every change requires review. Provider budgets are not a guaranteed total spend cap.",
            "calls": "Calls go only to JARVIS_OWNER_PHONE; max 300 seconds. Provider charges apply."}


@router.post("/proposals")
def create_proposal(model: Proposal):
    try:
        return propose(model)
    except (ValueError, TypeError) as error:
        raise HTTPException(422, str(error)) from None


# The newest-first cursor's starting point: past every row. Annotated rather
# than `= Query(default=...)` so these routes still work called as plain
# functions — tool_business_status once called them that way, and there a
# Query default is the Query object itself, which SQLite refuses ("type
# 'Query' is not supported", measured live). The tools now read the store
# directly; tests/test_business.py keeps the plain-function call pinned.
NEWEST = 9223372036854775807
Cursor = Annotated[int, Query(ge=1)]


@router.get("/actions")
def actions(before: Cursor = NEWEST):
    return {"items": [{**item, "repeat_note": repeat_note(item)}
                      for item in store.list_actions(before)]}


@router.get("/actions/{action_id}")
def action(action_id: str):
    try:
        found = store.get_action(action_id)
        return {**found, "repeat_note": repeat_note(found), "audit": store.audit(action_id)}
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


def repeat_note(action, *, for_brain=False):
    """A sentence when exactly this request was already let through on an
    earlier card — when, and what the connector said then — or None.

    Measured live, 2026-10-01: the first confirmed post failed with a bare
    error, and three hours later the identical bytes were put to the owner
    as a new card that looked like any other. Approval is spent once per
    card, as it should be, so a retry after a real failure has to stay
    possible. The owner decides it knowing what the first attempt did; every
    surface a card is shown on carries this (desk, phone, the gate's reason
    to the brain). Only a card still to be decided needs it.

    `for_brain` leaves out what the connector said: that is somebody else's
    text, and the gate's reason reaches the brain as JARVIS's own words."""
    try:
        if not isinstance(action, dict) or action.get("state") not in ("pending", "approved"):
            return None
        prior = store.sent_before(action.get("digest"), action.get("seq"))
    except Exception:
        log.warning("could not look for an earlier send of a card", exc_info=True)
        return None
    if prior is None:
        return None
    try:
        at = datetime.fromtimestamp(float(prior["updated"])).strftime("%a %d %b %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        at = "earlier"
    said = str((prior.get("result") or {}).get("message") or "").strip()
    if for_brain:
        said = "What it did then is written on that card."
    else:
        said = redact(said, secret_values(), set()) if said else (
            "What it did then was not recorded — check before approving.")
    return _clip(f"Already sent once ({at}, card {prior['id'][:8]}): {said} "
                 f"Approving this card sends it again.", 600)


async def decide(action_id: str, digest: str, approve: bool, *, via: str = "desk"):
    """One decision on one card, by whichever door it came through.

    Two doors exist: the desk's route below, guarded by the browser's
    `Origin`, and the owner's phone (`whatsapp._decide`), guarded by the
    sender's number and a button that carries this exact digest. Both come
    HERE, so what a yes does can never differ between them. Raises
    ValueError from the store's compare-and-swap when the card has moved on.

    `via` is written to the card's audit trail when it is not the desk, so
    the ledger says which door a decision came through.
    """
    if not approve:
        row = store.transition(action_id, digest, "pending", "rejected")
    else:
        # A connector action is not ours to send. It was staged by the
        # PreToolUse gate because the brain tried to call a tool on a server
        # the USER declared, and only that server can perform it — JARVIS
        # cannot call it at all. Approving it lifts the refusal for exactly
        # that call, once, the next time the brain asks. Routing it into
        # `execute` would reach `providers.identity`, which raises on
        # anything outside its four, and report a failure for a post that is
        # sitting there waiting to be allowed.
        current = store.get_action(action_id)
        if str(current.get("provider", "")).startswith("connector:"):
            row = store.transition(action_id, digest, "pending", "approved")
        else:
            row = await execute(action_id, digest)
    if via != "desk":
        store.note(action_id, f"via:{via}")
    return row


@router.post("/actions/{action_id}/decision")
async def decision(action_id: str, model: Decision, request: Request):
    # Deliberately no approval tool. The MCP bearer token cannot approve itself.
    if request.headers.get("authorization") or not web_auth.origin_allowed(request.headers.get("origin")):
        raise HTTPException(403, "Review and approve this action in the Business UI")
    try:
        return await decide(action_id, model.digest, model.approve)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


def _a_human_in_the_browser(request: Request, what: str) -> None:
    """The same door as `decision`: a browser JARVIS serves from, never the
    MCP bearer token. Evidence of what the brain asked for is not the
    brain's to remove."""
    if request.headers.get("authorization") or not web_auth.origin_allowed(request.headers.get("origin")):
        raise HTTPException(403, f"{what} in the Business UI")


@router.delete("/actions/{action_id}")
def delete_action(action_id: str, request: Request):
    """Take one FINISHED approval off the desk — soft: see `store.delete_action`."""
    _a_human_in_the_browser(request, "Delete approvals")
    try:
        return store.delete_action(action_id)
    except ValueError as error:
        raise HTTPException(404 if "not found" in str(error) else 409, str(error)) from None


@router.post("/actions/clear-completed")
def clear_completed(request: Request):
    _a_human_in_the_browser(request, "Clear approvals")
    return {"deleted": store.clear_completed()}


@router.get("/reports/{provider}")
async def report(provider: Literal["google", "meta", "twilio", "chatgpt", "linkedin"]):
    try:
        return {"provider": provider, "data": public_result(await providers.perform(provider, "report", {}, read=True)),
                "scope": "Bounded recent provider results; use provider console for complete historical reporting."}
    except (ValueError, providers.ProviderError) as error:
        raise HTTPException(503, str(error)) from None
    except TimeoutError:
        raise HTTPException(504, "Provider report timed out") from None


# Which statuses each kind of record may have. `Record.status` admits their
# union; this is the per-kind half of the check, and what `business_find`
# holds a stored status to before it shows one.
RECORD_STATES = {"task": ("open", "done"), "contact": ("lead", "qualified", "won", "lost"),
                 "invoice": ("draft", "sent", "paid", "void"), "expense": ("open", "paid", "void")}


def save_record(model: Record):
    from datetime import date
    if model.due:
        date.fromisoformat(model.due)
    if model.status not in RECORD_STATES[model.kind]:
        raise ValueError("Status is not valid for this record type")
    return store.save_record(model.kind, model.model_dump(exclude={"id", "version", "kind"}), model.id, model.version)


@router.post("/records")
def record(model: Record):
    try:
        return save_record(model)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None


@router.get("/records/{kind}")
def records(kind: Literal["task", "contact", "invoice", "expense"],
            before: Cursor = NEWEST):
    return {"items": store.list_records(kind, before)}


@router.get("/export")
def export():
    return StreamingResponse(store.export_rows(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="business.jsonl"'})


@router.get("/briefing")
def briefing():
    return store.briefing()


# ---------------------------------------------------------------------------
# What the brain reads: assembled to fit, never cut to fit
# ---------------------------------------------------------------------------
#
# Measured live, 2026-09-26 01:16-01:26 +04: the user had approved a Paperclip
# comment (card c0602703) and asked for it to be sent, and JARVIS refused three
# times because it could not see what he had approved. `business_status` was
# the whole ledger as one JSON string, about 2,177 characters; `/internal/tool`
# cuts every result at server.TOOL_RESULT_CAP (1,500) with "… (truncated — ask
# for more)"; the cut landed inside the card's `operation`; and there was no
# tool to ask for more.
#
# So a tool result here is ASSEMBLED under the cap rather than cut by it. The
# live cards go first and are always counted; what does not fit is counted as
# `not_listed` and reached by paging with `kind`/`before`, never lost off the
# end of a string. One card in full — payload included — is `business_action`
# (rendered in server.py, which owns the untrusted block it goes in).

# server.TOOL_RESULT_CAP. Not imported: server imports this module, and run as
# `python server.py` it is `__main__`, so importing it back would start a
# second server. Pinned by tests/test_business_action.py instead.
TOOL_TEXT_BUDGET = 1500

LIVE_STATES = ("pending", "approved")
RECORD_KINDS = ("task", "contact", "invoice", "expense")
STATUS_KINDS = ("approval",) + RECORD_KINDS
MONEY_MAPS = ("receivables_minor", "unpaid_expenses_minor")
# Read from the store per list before fitting: more than ever fits in one
# reply. A listing reads one more, to know whether there is another page.
LIST_FETCH = 50
NAME_LIMIT = 100          # a provider or operation name, in a summary
NOTES_PREVIEW = 200       # a record's notes, in a page of one list
# ... and in the overview, where every list has to find room beside the
# others. A task with long notes at 200 characters did not fit beside two
# history cards, and the overview listed no task at all.
OVERVIEW_NOTES_PREVIEW = 60
# What a pathological record is clipped to when even alone it will not fit
# a page (see `_listing`).
CLIPPED = 60

LIVE_DETAIL = "business_action with a card's id shows it in full, payload included."
MORE_DETAIL = ("Where not_listed appears, business_status with kind approval, task, contact, "
               "invoice or expense lists the rest, newest first.")
PAGE_DETAIL = "Pass next_before back as before for the older ones."
CURRENCY_DETAIL = ("Totals for the currencies not listed are on the Business desk; "
                   "business_status kind invoice or expense lists the records behind them.")


def when(epoch):
    """A time the brain can say: local ISO 8601, to the second, with offset."""
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# Characters `json.dumps(ensure_ascii=False)` lets through raw that must not
# reach the brain raw, found among everything outside printable ASCII (what
# is below U+0020 `json.dumps` escapes itself):
#   * line breaks to `str.splitlines()` — U+0085, U+2028, U+2029 — which let
#     a record's title start a line of its own, outside any block, reading
#     as JARVIS;
#   * what the brain cannot see, so cannot copy — a zero-width space, a
#     no-break space, a variation selector, a combining accent, a joiner, a
#     tag character. Shown raw, a payload holding one would be re-sent
#     without it and miss the approval's digest; as `\uXXXX` it is visible
#     and exact;
#   * a lone surrogate: valid JSON, not valid UTF-8, fatal on the way out of
#     /internal/tool.
_BEYOND_ASCII = re.compile("[\x7f-\U0010ffff]")
_INVISIBLE = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Mn", "Me"}


def _escape(match):
    char = match.group()
    category = unicodedata.category(char)
    if category not in _INVISIBLE and not (category == "Zs" and char != " "):
        return char
    point = ord(char)
    if point <= 0xFFFF:
        return "\\u%04x" % point
    point -= 0x10000                              # JSON spells it as a pair
    return "\\u%04x\\u%04x" % (0xD800 + (point >> 10), 0xDC00 + (point & 0x3FF))


def brain_json(value, **kwargs):
    """JSON for the brain: characters as themselves (an escape per character
    would make a Cyrillic record six times longer against the budget, and a
    payload's "—" something to decode by hand), except the ones above, which
    become their `\\uXXXX` escapes. Lossless: `json.loads` gives back exactly
    `value`."""
    text = json.dumps(value, ensure_ascii=False, **kwargs)
    return text if text.isascii() and "\x7f" not in text else _BEYOND_ASCII.sub(_escape, text)


def display_payload(payload):
    """A payload as the brain is shown it: sorted keys, no spaces — the
    stored canonical form — with characters as themselves. It parses to
    exactly the approved value, which is what the gate's digest is over."""
    return brain_json(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(doc):
    return brain_json(doc)


def _fits(doc):
    return len(_text(doc)) <= TOOL_TEXT_BUDGET


def _clip(value, limit):
    value = str(value or "")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def notes_preview(notes, limit):
    return notes[:limit] + "…"


def _is_live(row, now):
    return row["state"] in LIVE_STATES and row["expires"] > now


def card_summary(row, now, secrets, listing=False):
    """A card without its payload: that it exists, what it is, where it
    stands. One time, the one that says when it got there — staged, for a
    pending card; decided, for the rest — and its deadline while live. The
    full set is `business_action`'s; a summary has to fit beside the rest.
    Its foreign text is redacted exactly as `business_action` redacts it."""
    found = set()
    item = {"id": row["id"], "state": row["state"],
            "provider": _clip(redact(row["provider"], secrets, found), NAME_LIMIT),
            "operation": _clip(redact(row["operation"], secrets, found), NAME_LIMIT)}
    if row["state"] == "pending":
        item["created"] = when(row["created"])
    else:
        item["updated"] = when(row["updated"])
    if _is_live(row, now):
        item["expires"] = when(row["expires"])
        if listing:
            item["live"] = True
    elif row["state"] in LIVE_STATES:
        item["lapsed"] = True
    if row["state"] in ("failed", "unknown") and row.get("message"):
        item["outcome"] = _clip(redact(row["message"], secrets, found), 160)
    # As business_action will count it, so the two never disagree.
    item["payload_chars"] = len(display_payload(redact(json.loads(row["payload"]), secrets, found)))
    return item


def record_summary(row, secrets, preview=NOTES_PREVIEW, overview=False):
    """A record with the id and version `business_record` needs to update it.
    Long notes are previewed and SAY so; an update may leave notes out (they
    are kept) or add to them with `append_notes`, and the desk shows them
    whole. In the overview the save time is left to the list pages."""
    body = row.get("body") or {}
    found = set()
    text = lambda value, limit: _clip(redact(str(value or ""), secrets, found), limit)
    item = {"id": row["id"], "version": row["version"],
            "title": text(body.get("title"), 200), "status": body.get("status", "")}
    if body.get("due"):
        item["due"] = body["due"]
    if body.get("amount_minor"):
        item["amount_minor"] = body["amount_minor"]
    item["currency"] = body.get("currency", "")
    if body.get("contact"):
        item["contact"] = text(body["contact"], 300)
    notes = redact(str(body.get("notes") or ""), secrets, found)
    if len(notes) > preview:
        item["notes"] = notes_preview(notes, preview)
        item["notes_chars"] = len(notes)
    elif notes:
        item["notes"] = notes
    if not overview:
        item["updated"] = when(row["updated"])
    return item


def _brief_connections():
    """Which providers are configured; and, to use if it fits, the rest."""
    current = connections()
    providers_now = current["providers"]
    # Only the configured ones, joined: this is placed whole in a
    # 1,500-character overview, and listing every provider that is NOT set
    # up took room from the approval history each time one was added
    # (LinkedIn, its hand-post route). The rest are the tool's own enum,
    # and the per-provider detail below still goes in when it fits.
    short = {"configured": ", ".join(sorted(p for p, c in providers_now.items() if c["configured"])) or "none"}
    full = {}
    for name, state in providers_now.items():
        entry = {"configured": state["configured"]}
        if state["missing"]:
            entry["missing"] = state["missing"]
        if state["issue"]:
            entry["issue"] = state["issue"]
        full[name] = entry
    return short, {"providers": full, "approval_required": True,
                   "spending": current["spending"], "calls": current["calls"]}


def _as_sent(doc):
    """The overview exactly as it will be sent, which is what every fitting
    step measures: `not_listed` wherever a list shows fewer than its count,
    a money map's currencies that did not fit counted beside it, the paging
    hint only if anything is not listed, and an empty record kind as its
    bare count. Nothing is reserved for text that may not come."""
    sent = {key: value for key, value in doc.items() if not key.startswith("_")}
    brief = sent["briefing"] = {}
    currencies_hidden = False
    for key, value in doc["briefing"].items():
        if key in MONEY_MAPS:
            brief[key] = dict(value)
            hidden = doc["_currencies"][key] - len(value)
            if hidden > 0:
                brief[key.replace("_minor", "_currencies_not_listed")] = hidden
                currencies_hidden = True
        else:
            brief[key] = value
    if currencies_hidden:
        brief["currencies_detail"] = CURRENCY_DETAIL
    live = sent["live_approvals"] = dict(doc["live_approvals"])
    history = sent["approval_history"] = dict(doc["approval_history"])
    records = sent["records"] = {kind: dict(value) for kind, value in doc["records"].items()}
    hidden = False
    for section, key in [(live, "cards"), (history, "cards")] + [(r, "items") for r in records.values()]:
        missing = section["count"] - len(section[key])
        if missing > 0:
            section["not_listed"] = missing
            hidden = True
        elif not section["count"]:
            del section[key]
    if not live["count"]:
        del live["detail"]
    if hidden:
        sent["more"] = MORE_DETAIL
    return sent


def _fit(doc, lanes):
    """Append each lane's items in turn while the overview, as sent, fits.
    A lane closes at its first item that does not, so every list shows its
    NEWEST (or, for money, its LARGEST) contiguously, and one long list
    cannot starve the others."""
    lanes = [(target, iter(items)) for target, items in lanes]
    while lanes:
        for lane in list(lanes):
            target, items = lane
            item = next(items, None)
            if item is not None:
                target.append(item)
                if _fits(_as_sent(doc)):
                    continue
                target.pop()
            lanes.remove(lane)


def _overview():
    now = time.time()
    secrets = secret_values()
    live = store.live_actions()
    summary = store.briefing()
    history = [row for row in store.action_summaries(limit=LIST_FETCH) if not _is_live(row, now)]
    counts = store.count_records()
    # The per-state counts are the two approval sections' counts, split
    # further; the lists page by state. Room here goes to the lists.
    finished = sum(summary.pop("actions").values()) - len(live)
    summary["as_of"] = when(summary.pop("generated_at"))
    short, full = _brief_connections()
    # The money maps have one entry per currency, and nothing bounds how
    # many currencies there are: filled like any other list, largest first,
    # rather than assumed to fit. Placed whole, forty of them pushed the
    # live cards out and then the overview past the cap.
    money = {key: sorted(summary[key].items(), key=lambda pair: (-pair[1], pair[0]))
             for key in MONEY_MAPS}
    for key in MONEY_MAPS:
        summary[key] = []

    doc = {"live_approvals": {"count": len(live), "cards": [], "detail": LIVE_DETAIL},
           "briefing": summary,
           "connections": short,
           "approval_history": {"count": max(len(history), finished), "cards": []},
           "records": {kind: {"count": counts.get(kind, 0), "items": []} for kind in RECORD_KINDS},
           "_currencies": {key: len(money[key]) for key in MONEY_MAPS}}

    # Live cards first, every one that fits: nothing else here matters as
    # much, and a live card not listed is still counted.
    _fit(doc, [(doc["live_approvals"]["cards"], [card_summary(row, now, secrets) for row in live])])
    # Then the rest, a turn each: the largest sums owed, the newest of
    # everything else.
    _fit(doc, [(summary[key], money[key]) for key in MONEY_MAPS]
         + [(doc["approval_history"]["cards"], [card_summary(row, now, secrets) for row in history])]
         + [(doc["records"][kind]["items"],
             [record_summary(row, secrets, OVERVIEW_NOTES_PREVIEW, overview=True)
              for row in store.list_records(kind, limit=LIST_FETCH)])
            for kind in RECORD_KINDS if counts.get(kind)])
    # The per-provider detail, if there is room left for it.
    doc["connections"] = full
    if not _fits(_as_sent(doc)):
        doc["connections"] = short
    sent = _as_sent(doc)
    if not _fits(sent):
        # Unreachable: everything above that can grow is fitted, and what is
        # placed whole is bounded. If that is ever wrong, the reply is still
        # never cut: it says how many live cards there are, and where the
        # rest is.
        sent = {"live_approvals": {"count": len(live), "detail": LIVE_DETAIL}, "more": MORE_DETAIL}
    return _text(sent)


def _status_args(args):
    kind, before = args.get("kind"), args.get("before")
    if kind in (None, ""):
        if before not in (None, ""):
            raise ValueError("before pages through one list; give kind too: "
                             + ", ".join(STATUS_KINDS))
        return None, None
    if kind not in STATUS_KINDS:
        raise ValueError("kind is one of " + ", ".join(STATUS_KINDS))
    if before in (None, ""):
        return kind, NEWEST
    if isinstance(before, str) and before.strip().isdigit():
        before = int(before)
    if isinstance(before, bool) or not isinstance(before, int) or before < 1:
        raise ValueError("before is the next_before number from the previous page")
    return kind, before


def _listing(kind, before):
    """One page of one list, newest first, as many as fit."""
    now = time.time()
    secrets = secret_values()
    if kind == "approval":
        rows = store.action_summaries(before, limit=LIST_FETCH + 1)
        items = [card_summary(row, now, secrets, listing=True) for row in rows[:LIST_FETCH]]
    else:
        rows = store.list_records(kind, before, limit=LIST_FETCH + 1)
        items = [record_summary(row, secrets) for row in rows[:LIST_FETCH]]
    beyond = len(rows) > LIST_FETCH

    def page(shown):
        more = shown < len(items) or beyond
        doc = {"kind": kind, "items": items[:shown],
               "next_before": rows[shown - 1]["seq"] if more and shown else None}
        if more:
            doc["detail"] = PAGE_DETAIL
        return doc

    shown = 0
    while shown < len(items) and _fits(page(shown + 1)):
        shown += 1
    if items and not shown:
        # A page always moves on, or paging would ask for the same one for
        # ever. Only a pathological item gets here (every field is already
        # clipped); it is shown with its long values clipped harder, and
        # says so. What an update must send back — the title, the status —
        # is left whole, so it cannot be written back shortened; a clipped
        # contact sent back is refused (`_merged_update`).
        keep = {"id", "version", "title", "status", "currency", "state"}
        items[0] = {**{key: _clip(value, CLIPPED) if isinstance(value, str) and key not in keep
                       else value for key, value in items[0].items()}, "clipped": True}
        shown = 1
    return _text(page(shown))


async def tool_business_status(args):
    kind, before = _status_args(args)
    return _listing(kind, before) if kind else _overview()


# --- one card, in full ------------------------------------------------------

# The fields a card is shown with, and nothing else: no digest, no receipt,
# no audit trail, no neighbouring row.
CARD_FIELDS = ("id", "provider", "operation", "state", "created", "updated", "expires", "payload")
# The ones somebody else's text can be in. id, state and the times are
# JARVIS's own, and redacting inside them would only corrupt them.
_FOREIGN_CARD_FIELDS = ("provider", "operation", "payload")
_CARD_REF = re.compile(r"[0-9a-f-]{4,36}")
# A variable whose NAME says it holds a credential. Its value is never
# handed back inside a card, whatever the card says. Loose on purpose: a
# false positive shows `[redacted]` and says so, a false negative is a leak.
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|PASSCODE|CREDENTIAL|KEY|AUTH"
                          r"|(?:^|_)PIN(?:_|$)", re.IGNORECASE)
# Most credentials are long, and a short value would match ordinary text
# ("1234" in an issue number). A PIN or a password is the exception: short
# by nature, and exactly what must not come back.
_SHORT_SECRET_NAME = re.compile(r"PASSWORD|PASSWD|PASSCODE|(?:^|_)PIN(?:_|$)", re.IGNORECASE)
_SECRET_MIN, _SHORT_SECRET_MIN = 8, 4
# A flag's value, not a credential, whatever the variable is called.
_NOT_SECRETS = {"true", "false", "none", "null", "yes", "no", "on", "off", "enabled", "disabled"}
REDACTED = "[redacted]"


def secret_values():
    """Every secret on this machine a card must not carry back: credential
    environment variables (the provider keys among them) and JARVIS's own
    tool token. Longest first, so one never hides inside another."""
    values = set()
    for name, value in os.environ.items():
        value = value.strip()
        if not _SECRET_NAME.search(name) or value.lower() in _NOT_SECRETS:
            continue
        if len(value) >= (_SHORT_SECRET_MIN if _SHORT_SECRET_NAME.search(name) else _SECRET_MIN):
            values.add(value)
    try:
        token = data_paths.tool_token_path().read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if len(token) >= _SECRET_MIN:
        values.add(token)
    return sorted(values, key=len, reverse=True)


def _numeric_forms(secret):
    """How a secret made of digits can appear once stored as a number: a
    PIN of 01234567 is the integer 1234567."""
    forms = {secret}
    if secret.isdigit() and len(secret.lstrip("0")) >= _SHORT_SECRET_MIN:
        forms.add(secret.lstrip("0"))
    return forms


def redact(value, secrets, found):
    """`value` with every secret in `secrets` replaced — in keys, in strings,
    and in numbers (a PIN is a secret stored as an integer) — adding what it
    met to `found`. Two keys that redact to the same text are kept apart
    (`[redacted]#2`), so an argument is never silently lost from the view."""
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            name = redact(key, secrets, found)
            if name in out:
                base, n = name, 2
                while f"{base}#{n}" in out:
                    n += 1
                name = f"{base}#{n}"
            out[name] = redact(child, secrets, found)
        return out
    if isinstance(value, list):
        return [redact(child, secrets, found) for child in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        met = [secret for secret in secrets
               if any(form in str(value) for form in _numeric_forms(secret))]
        if met:
            found.update(met)
            return REDACTED
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                found.add(secret)
                value = value.replace(secret, REDACTED)
    return value


def action_card(ref):
    """One card for the brain, or why not.

    Returns one of:
      {"card": {...CARD_FIELDS}, "payload_text": str, "redacted": n, "lapsed": bool}
      {"matches": [summary, ...]}     — `ref` is the start of more than one id
      {"problem": "missing" | "malformed" | "unknown", "ref": str}

    `payload_text` is `display_payload` of the stored payload — it parses to
    exactly the approved value — unless a secret had to be taken out of it,
    which `redacted` counts.
    """
    if not isinstance(ref, str) or not ref.strip():
        return {"problem": "missing", "ref": ""}
    ref = ref.strip().lower()
    if not _CARD_REF.fullmatch(ref):
        return {"problem": "malformed", "ref": ""}
    rows = store.find_actions(ref)
    if not rows:
        return {"problem": "unknown", "ref": ref}
    now = time.time()
    if len(rows) > 1:
        return {"matches": [{"id": row["id"], "state": row["state"], "created": when(row["created"]),
                             "lapsed": row["state"] in LIVE_STATES and row["expires"] <= now}
                            for row in rows]}
    row = rows[0]
    found = set()
    secrets = secret_values()
    card = {field: (redact(row[field], secrets, found) if field in _FOREIGN_CARD_FIELDS
                    else row[field]) for field in CARD_FIELDS}
    for stamp in ("created", "updated", "expires"):
        card[stamp] = when(row[stamp])
    return {"card": card, "payload_text": display_payload(card["payload"]),
            "redacted": len(found),
            "lapsed": row["state"] in LIVE_STATES and row["expires"] <= now}


def split_parts(text, size):
    """`text` as consecutive (start, end) spans of at most `size` characters,
    never ending inside a JSON escape or between the halves of something a
    reader sees as one character — a reader joining them in order gets
    `text` back, and no single part ends on a lone backslash, half a
    `\\uXXXX`, half a flag or an emoji without its skin tone.
    Deterministic: the same text and size give the same parts."""
    size = max(int(size), 16)
    spans, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            end = _safe_end(text, start, end)
        spans.append((start, end))
        start = end
    return spans or [(0, 0)]


_HIGH_HALF = re.compile(r"\\u[dD][89abAB][0-9a-fA-F]{2}")


def _regional(char):
    return "\U0001F1E6" <= char <= "\U0001F1FF"


def _safe_end(text, start, end):
    """Move `end` back off anything a cut would break. Combining marks,
    joiners and variation selectors never reach here raw (`brain_json`
    escapes them), so what is left is the escape itself and the two emoji
    joins that are ordinary visible characters."""
    for back in range(1, 13):                      # a pair escape is 12 long
        at = end - back
        if at <= start:
            break
        if text[at] != "\\":
            continue
        run = 0
        while at - run - 1 >= 0 and text[at - run - 1] == "\\":
            run += 1
        if run % 2:
            continue                               # this backslash is itself escaped
        length = 6 if text[at + 1:at + 2] == "u" else 2
        if (length == 6 and 0xD800 <= int(text[at + 2:at + 6] or "0", 16) <= 0xDBFF
                and text[at + 6:at + 8] == "\\u"):
            length = 12                            # a surrogate pair is one character
        if at + length > end:
            if (length == 6 and 0xDC00 <= int(text[at + 2:at + 6], 16) <= 0xDFFF
                    and at - 6 > start and _HIGH_HALF.fullmatch(text[at - 6:at])):
                return at - 6                      # not between a pair's halves either
            return at
        break
    if end - 1 > start and "\U0001F3FB" <= text[end] <= "\U0001F3FF":
        end -= 1                                   # not between an emoji and its skin tone
    if _regional(text[end]):
        # A flag is a PAIR of regional indicators, and a run of flags is a run
        # of pairs: cutting after an odd count of them cuts a flag in half.
        run = end
        while run > 0 and _regional(text[run - 1]):
            run -= 1
        if (end - run) % 2 and end - 1 > start:
            end -= 1
    return end


async def tool_business_propose(args):
    return json.dumps(propose(Proposal.model_validate(args)))


# --- a record to point at, not to read ------------------------------------
#
# `business_record` is a DURABLE writer (server.DURABLE_WRITERS): it is
# refused for the rest of a brain generation that has read anything JARVIS
# did not write, because a record outlives the turn that wrote it. And
# `business_status` taints, rightly — it shows a record's title, notes and
# contact, which the user may have pasted in from a client's email, and a
# card's payload, which the brain composed out of whatever it had read. An
# update needed the id and version, and `business_status` was the only place
# they were: so every update followed a tainting read and was refused, and
# after the rotation the refusal asked for, the only way back to the id was
# the same read. JARVIS could create a record and never change one (found by
# the adversarial review of the business_action change, 2026-09-26).
#
# The answer is not to trust what a record says. It is to stop needing to
# read it. `business_find` takes the USER'S words for a record and does the
# matching here, and what it hands back is a HANDLE: the id and version an
# update needs, and the fields that let the brain say which one — kind,
# status, due date, amount, currency, when it was saved — each re-checked
# against the closed set or the exact shape `Record` admits, so a row no
# validator saw (a restored backup, a hand-edited database) cannot smuggle
# words through them either. Never a title, notes or a contact: nothing in
# it anybody typed. That is why it may skip the taint
# (server.TAINT_EXEMPT_TOOLS), and tests/test_business_find.py holds the
# claim against rows with an instruction in every column.
#
# `business_record` replies with the same handle, for the same reason. It
# is exempt from the taint because it writes; its reply used to be the whole
# saved row, so a status-only update put the STORED notes in front of the
# brain with nothing marked, and the write after it would have passed.

_HANDLE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# ASCII digits, not `\d`: `\d` admits Arabic-Indic and every other script's
# digits, which `date.fromisoformat` then refuses by quoting the value.
_HANDLE_DUE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HANDLE_CURRENCY = re.compile(r"[A-Z]{3}")
_AMOUNT_MAX = 10 ** 12


class Refused(ValueError):
    """A refusal worded in this module.

    `business_find` and `business_record` are exempt from the untrusted-text
    mark, so whatever they say when they fail reaches the brain as JARVIS's
    own words. That has to be a sentence written here — never an exception's
    own text, which can quote the row that caused it: sqlite3 refusing a row
    that is not UTF-8 says "Could not decode … with text '<the row>'", and
    before `_worded_here` that went to the brain unmarked (found by the
    adversarial review of this change)."""


FIND_NONE = "Nothing matches all of those words. Try fewer of them, or ask the user which record."
FIND_MORE = ("Not every match is listed; not_listed counts the rest. Narrow it with more of "
             "the user's words, a kind or a status.")
FIND_UNREADABLE = ("unreadable counts records too damaged to read safely, or to point at; they "
                   "are neither matched nor listed.")


def _find_detail(none, more, unreadable):
    return " ".join(text for flag, text in ((none, FIND_NONE), (more, FIND_MORE),
                                            (unreadable, FIND_UNREADABLE)) if flag) or None


# Every `detail` business_find can say — each sentence, and each join of them.
FIND_DETAILS = frozenset(filter(None, (_find_detail(n, m, u) for n in (False, True)
                                       for m in (False, True) for u in (False, True))))
FIND_REPLY_KEYS = ("found", "records", "not_listed", "unreadable", "detail")

# What an update must be told when its handle no longer points at anything.
HANDLE_GONE = "No record has that id now. Find it again with business_find."
HANDLE_STALE = ("That record has changed since business_find read it. Find it again for its "
                "current version, then update it.")
HANDLE_NO_VERSION = "An update needs the version business_find gave with the id."
HANDLE_UNREADABLE = "That record is too damaged to read safely, so it was not changed."
LEDGER_TROUBLE = ("The ledger could not be read or written just now, so nothing was changed. "
                  "Try once more, or use the Business desk.")
# A record's body: everything but the three a handle carries.
_BODY_FIELDS = tuple(field for field in Record.model_fields if field not in ("id", "version", "kind"))


def _worded_here(tool):
    """Let a record tool fail only in `Refused` sentences: its own, a
    validation failure named field by field (`_invalid`), or LEDGER_TROUBLE
    for anything it did not word itself — a locked database, a driver
    error, a body nested past the parser. The real cause goes to the log."""
    @functools.wraps(tool)
    async def worded(args):
        try:
            return await tool(args)
        except Refused:
            raise
        except ValidationError as error:
            raise Refused(_invalid(error)) from None
        except Exception:
            log.warning("%s failed; the brain is told only that the ledger was unavailable",
                        tool.__name__, exc_info=True)
            raise Refused(LEDGER_TROUBLE) from None
    return worded


def _is_count(value, low=1, high=None):
    return (isinstance(value, int) and not isinstance(value, bool) and value >= low
            and (high is None or value <= high))


def _is_date(value):
    from datetime import date
    if not isinstance(value, str) or not _HANDLE_DUE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def record_handle(row):
    """A record as something to point at: id, version, kind, and what tells
    one record from another without anybody's words — status, due date,
    amount and currency, when it was saved. Each field is held to the shape
    `Record` admits and left out if it fails; a record whose id, version or
    kind fails cannot be pointed at at all, and is None."""
    kind = row.get("kind")
    if (kind not in RECORD_STATES or not isinstance(row.get("id"), str)
            or not _HANDLE_ID.fullmatch(row["id"]) or not _is_count(row.get("version"))):
        return None
    body = row.get("body") if isinstance(row.get("body"), dict) else {}
    item = {"id": row["id"], "version": row["version"], "kind": kind}
    if body.get("status") in RECORD_STATES[kind]:
        item["status"] = body["status"]
    if _is_date(body.get("due")):
        item["due"] = body["due"]
    currency = body.get("currency")
    if (_is_count(body.get("amount_minor"), 1, _AMOUNT_MAX) and isinstance(currency, str)
            and _HANDLE_CURRENCY.fullmatch(currency)):
        item["amount_minor"], item["currency"] = body["amount_minor"], currency
    saved = when(row.get("updated"))
    if saved:
        item["updated"] = saved
    return item


def _folded(text):
    """Text as a search compares it: compatibility forms unified, accents
    and case dropped — speech gives "cafe" for "Café", and "acme" for
    "ＡＣＭＥ" or "ACME"."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _words(text):
    return re.findall(r"\w+", _folded(text))


def _find_args(args):
    kind, status, match = args.get("kind"), args.get("status"), args.get("match")
    if kind in (None, ""):
        kind = None
    elif not isinstance(kind, str) or kind not in RECORD_STATES:
        raise Refused("kind is one of " + ", ".join(RECORD_KINDS))
    if status in (None, ""):
        status = None
    else:
        allowed = RECORD_STATES[kind] if kind else sorted({s for states in RECORD_STATES.values()
                                                           for s in states})
        if not isinstance(status, str) or status not in allowed:
            raise Refused("status is one of " + ", ".join(allowed)
                          + (" for that kind" if kind else ""))
    if match in (None, ""):
        return kind, status, []
    if not isinstance(match, str):
        raise Refused("match is the user's own words for the record, as text")
    words = _words(match)
    if not words:
        raise Refused("match needs a word from the record's title or contact")
    return kind, status, words


def _is_match(body, status, words):
    """Every word the START of a word somewhere in the title or the contact
    — the two things a person calls a record by — and the status, if one was
    asked for. The start, not anywhere: "art" is not Stuart, and "retain" is
    still the retainer."""
    if status is not None and body.get("status") != status:
        return False
    named = _words(body.get("title")) + _words(body.get("contact"))
    return all(any(token.startswith(word) for token in named) for word in words)


@_worded_here
async def tool_business_find(args):
    """Records matching the user's words, as handles, most recently created
    first — as many as fit, the rest counted. A record too damaged to match,
    or matched and impossible to point at, is counted as unreadable and
    nothing else of it is said."""
    kind, status, words = _find_args(args)
    found, unreadable, handles = 0, 0, []
    for row in store.iter_records(kind):
        body = row.get("body")
        if row.get("damaged") or not isinstance(body, dict):
            unreadable += 1
            continue
        if not _is_match(body, status, words):
            continue
        handle = record_handle(row)
        if handle is None:
            unreadable += 1
            continue
        found += 1
        if len(handles) < LIST_FETCH:
            handles.append(handle)

    def reply(shown):
        doc = {"found": found, "records": handles[:shown]}
        if found > shown:
            doc["not_listed"] = found - shown
        if unreadable:
            doc["unreadable"] = unreadable
        detail = _find_detail(not found, found > shown, unreadable)
        if detail:
            doc["detail"] = detail
        return doc

    shown = 0
    while shown < len(handles) and _fits(reply(shown + 1)):
        shown += 1
    return _text(reply(shown))


def _invalid(error):
    """A refused record in words the brain can act on — which field, and
    what it must be — and never the value. pydantic quotes the value it
    refused, and on an update that is the MERGED record: the stored notes,
    which the brain has not been shown, arrived in the error."""
    problems = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        field = item["loc"][0] if item.get("loc") else None
        name = field if field in Record.model_fields else "an argument this tool does not take"
        problems.append(f"{name}: {item['msg']}")
    return "That record was not saved. " + "; ".join(problems)


def _checked(model):
    """What `save_record` would refuse, refused first and in words of this
    module: its own refusals quote the value (`date.fromisoformat`), and on
    an update the value can be the stored one."""
    states = RECORD_STATES[model.kind]
    if model.status not in states:
        raise Refused("That record was not saved. status: a " + model.kind + " is one of "
                      + ", ".join(states))
    if model.due and not _is_date(model.due):
        raise Refused("That record was not saved. due: a real date as YYYY-MM-DD, or empty")
    return model


def _merged_update(args, append):
    """The brain's update of a record, laid over what is stored.

    `Record` fills every field it is not given with a default, so an update
    that named only the new status wrote empty notes and USD over the record
    — and the brain may only ever have seen a PREVIEW of the notes
    (`record_summary`), or, from `business_find`, nothing of them at all. So
    a field the brain leaves out keeps its stored value — the title and the
    status included, so an update needs only the handle and what changes;
    `append` adds to the stored notes without the brain ever needing the
    whole of them; and notes that start with a preview of the stored ones
    (the preview sent back, with or without more added) are refused rather
    than written over the real ones. The same for a title or a contact sent
    back as the clipped form a page showed. A stored key `Record` does not
    have is dropped, as the desk's own save drops it: refusing instead would
    make that record impossible to change by voice again. The desk always
    posts the whole record, so it keeps the plain replace (`save_record`)."""
    try:
        stored = store.get_record(args["id"])
    except store.DamagedRecord:
        raise Refused(HANDLE_UNREADABLE) from None
    except ValueError:
        raise Refused(HANDLE_GONE) from None
    # Only the body's own fields: a stored row must never be what supplies
    # the id, the version or the kind the brain left out.
    raw = stored["body"] if isinstance(stored["body"], dict) else {}
    body = {key: value for key, value in raw.items() if key in _BODY_FIELDS}
    model = Record.model_validate({**body, **args})
    if stored["kind"] != model.kind:
        # The stored kind is named only if it IS a kind: this reply reaches
        # the brain unmarked, and a hand-edited row can hold anything.
        if stored["kind"] in RECORD_STATES:
            raise Refused(f"That record is a {stored['kind']}, not a {model.kind}")
        raise Refused(HANDLE_UNREADABLE)
    if model.version is None:
        raise Refused(HANDLE_NO_VERSION)
    if model.version != stored["version"]:
        raise Refused(HANDLE_STALE)
    sent = set(args) - {"id", "version", "kind"}
    notes = str(body.get("notes") or "")
    if "notes" in sent:
        if append is not None:
            raise Refused("Send notes or append_notes, not both")
        for limit in (NOTES_PREVIEW, OVERVIEW_NOTES_PREVIEW, CLIPPED - 1):
            if (len(notes) > limit and model.notes.startswith(notes[:limit])
                    and not model.notes.startswith(notes)):
                raise Refused(
                    f"Those notes begin with the preview business_status showed, and the "
                    f"stored notes are {len(notes)} characters: sending them would cut the "
                    f"rest. Use append_notes to add to them, or leave notes out to keep them")
    for field in ("title", "contact"):
        value, kept = getattr(model, field), str(body.get(field) or "")
        if field in sent and len(kept) > CLIPPED and value == _clip(kept, CLIPPED):
            raise Refused(f"That {field} is the clipped form business_status showed; "
                          f"leave it out, or send it whole")
    if append is not None:
        return Record.model_validate({**model.model_dump(),
                                      "notes": f"{notes}\n{append}" if notes else append})
    return model


@_worded_here
async def tool_business_record(args):
    args = dict(args)
    append = args.pop("append_notes", None)
    if append is not None and (not isinstance(append, str) or not append.strip()):
        raise Refused("append_notes is the text to add to the notes")
    if args.get("id") not in (None, ""):
        if not isinstance(args["id"], str):
            raise Refused(HANDLE_GONE)
        model, saved = _checked(_merged_update(args, append)), "updated"
    else:
        if append is not None:
            if "notes" in args:
                raise Refused("Send notes or append_notes, not both")
            args["notes"] = append
        model, saved = _checked(Record.model_validate(args)), "created"
    try:
        row = save_record(model)
    except ValueError:
        # `_checked` has already refused all `save_record` would, so what is
        # left is the store's compare-and-swap: changed since it was read.
        raise Refused(HANDLE_STALE) from None
    return _text({"saved": saved, **(record_handle(row) or {})})


async def tool_business_report(args):
    provider = args.get("provider")
    if provider not in providers.REQUIRED:
        raise ValueError("Unsupported provider")
    return json.dumps(await report(provider))


TOOL_HANDLERS = {"business_status": tool_business_status, "business_find": tool_business_find,
                 "business_propose": tool_business_propose, "business_record": tool_business_record,
                 "business_report": tool_business_report}
