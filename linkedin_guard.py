"""How much JARVIS may do on LinkedIn, and the stop when LinkedIn objects.

Owner decision, 2026-10-08. LinkedIn's User Agreement forbids "bots or other
unauthorized automated methods" to create, comment on, like or share posts,
at risk of restriction or permanent closure. The connector JARVIS has drives
the owner's own signed-in browser, so until the official API route is live
the rate stays low and LinkedIn's first objection stops everything:

  * at most `posts_per_day` posts per account (default 1), and at least
    `min_post_gap_hours` between two posts on one account (default 6), even
    across midnight;
  * at most `comments_per_day` comments and replies (default 5);
  * on a challenge, captcha, "unusual activity" notice, restriction or failed
    login, every LinkedIn call is refused — reads too, since a read drives the
    same browser — until the owner resumes it. Nothing here ever tries to get
    past a challenge.

Enforced where calls are decided: the PreToolUse gate for the browser
connector (`server.internal_pretool`, before a card is staged AND again
before an approval is spent) and the business desk for the official API
(`business_api.propose` / `_execute`). Counted from the desk's own ledger —
every card that was let through, deleted or not, errored or not: a post that
errored may have gone out (2026-10-01).

The defaults are the interim rate. The owner steps them up once the API route
has run cleanly (`LINKEDIN_POSTS_PER_DAY`, `LINKEDIN_COMMENTS_PER_DAY`,
`LINKEDIN_MIN_POST_GAP_HOURS`); the brain cannot write the environment.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import data_paths

# The connector server(s) that are LinkedIn, by their name in connections.json.
DEFAULT_CONNECTOR_SERVERS = ("linkedin",)
# The official API's provider on the business desk (`business_providers`).
API_PROVIDER = "linkedin"

# Each acting tool of the browser connector, and the argument that makes it
# publish rather than rehearse.
_CONNECTOR_TOOLS = {
    "create_post": ("post", "confirm_post"),
    "comment_on_post": ("comment", "confirm_comment"),
    "reply_to_comment": ("comment", "confirm_reply"),
}
_API_OPERATIONS = {"post": "post", "comment": "comment"}

# What LinkedIn says when it objects. Matched only against a call's own
# status, message, error or URL — never against the content it read, which
# is somebody else's words (a feed post about captchas is not a captcha).
_CHALLENGE = re.compile(
    r"captcha|security (?:check|verification)|/checkpoint/|checkpoint/challenge|"
    r"unusual activity|suspicious activity|(?:temporarily |been |is )restricted|"
    r"account (?:is )?(?:restricted|suspended|locked)|verify (?:it'?s you|your identity)|"
    r"login window is open|sign[- ]?in (?:is )?required|not (?:signed|logged) in|"
    r"session (?:has )?expired|authwall|\bhttp 999\b|\bstatus 999\b|\b999 request denied",
    re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    next_at: Optional[float] = None


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default
    return max(low, min(high, value))


def limits() -> dict:
    return {"posts_per_day": _int_env("LINKEDIN_POSTS_PER_DAY", 1, 0, 10),
            "comments_per_day": _int_env("LINKEDIN_COMMENTS_PER_DAY", 5, 0, 50),
            "min_post_gap_hours": _int_env("LINKEDIN_MIN_POST_GAP_HOURS", 6, 0, 24)}


def connector_servers() -> frozenset[str]:
    raw = os.environ.get("LINKEDIN_CONNECTOR_SERVERS", "")
    names = [n.strip() for n in raw.split(",") if n.strip()] or list(DEFAULT_CONNECTOR_SERVERS)
    return frozenset(names)


def is_connector(server: str) -> bool:
    return str(server or "") in connector_servers()


def connector_kind(server: str, tool: str, payload) -> Optional[str]:
    """'post' / 'comment' for a call of the browser connector that publishes,
    None for anything else — a read, a dry run, another server."""
    if not is_connector(server) or not isinstance(payload, dict):
        return None
    kind, confirm = _CONNECTOR_TOOLS.get(str(tool or ""), (None, None))
    if kind is None:
        return None
    return kind if payload.get(confirm) is True else None


def provider_kind(operation: str) -> Optional[str]:
    return _API_OPERATIONS.get(str(operation or ""))


# --- the ledger ---------------------------------------------------------------

def _events(since: float) -> list[tuple[str, str, float]]:
    """(kind, account, at) for every LinkedIn publish let through since
    `since`: the gate's released connector cards and the desk's API cards,
    deleted ones included. `unknown` counts: it may have gone."""
    import business_store
    providers = [f"connector:{name}" for name in connector_servers()] + [API_PROVIDER]
    marks = ",".join("?" for _ in providers)
    with closing(business_store.connect()) as conn:
        rows = conn.execute(
            f"SELECT provider, operation, payload, updated FROM business_actions "
            f"WHERE provider IN ({marks}) AND state IN ('submitted','unknown','executing') "
            f"AND updated>=?", (*providers, float(since))).fetchall()
    out = []
    for provider, operation, payload, updated in rows:
        try:
            body = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if provider == API_PROVIDER:
            kind = provider_kind(operation)
            request = body.get("request") if isinstance(body, dict) else None
            account = (request or {}).get("account", "member") if isinstance(request, dict) else "member"
        else:
            server = provider[len("connector:"):]
            own = operation.split("__", 2)[2] if operation.count("__") >= 2 else operation
            kind = connector_kind(server, own, body)
            account = "member"
        if kind:
            out.append((kind, str(account), float(updated)))
    return out


def _day_start(at: float) -> float:
    return datetime.fromtimestamp(at).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def check(kind: str, account: str = "member", now: Optional[float] = None) -> Verdict:
    """May one more `kind` ('post' / 'comment') go out on `account` now?"""
    now = time.time() if now is None else now
    rule = limits()
    events = [(k, a, at) for k, a, at in _events(now - 2 * 86400)
              if k == kind and a == account and at <= now]
    today = [at for _k, _a, at in events if at >= _day_start(now)]
    tomorrow = _day_start(now) + 86400
    if kind == "post":
        cap, gap = rule["posts_per_day"], rule["min_post_gap_hours"] * 3600
        if len(today) >= cap:
            return Verdict(False, f"LinkedIn limit: at most {cap} post{'s' if cap != 1 else ''} a day "
                                  f"on this account, and today's {'is' if cap == 1 else 'are'} out.",
                           max(tomorrow, (max(today) + gap) if today else tomorrow))
        last = max((at for _k, _a, at in events), default=None)
        if last is not None and now - last < gap:
            return Verdict(False, f"LinkedIn limit: at least {rule['min_post_gap_hours']} hours "
                                  f"between posts on this account.", last + gap)
        return Verdict(True)
    if kind == "comment":
        cap = rule["comments_per_day"]
        if len(today) >= cap:
            return Verdict(False, f"LinkedIn limit: at most {cap} comments a day, and today's are out.",
                           tomorrow)
        return Verdict(True)
    return Verdict(True)


def when(at: Optional[float]) -> str:
    if not at:
        return "later"
    return datetime.fromtimestamp(at).strftime("%a %H:%M")


# --- the stop ------------------------------------------------------------------

def _halt_path():
    return data_paths.data_dir() / "linkedin-halt.json"


def looks_like_challenge(text) -> Optional[str]:
    """The phrase that says LinkedIn objected, or None."""
    match = _CHALLENGE.search(str(text or ""))
    return match.group(0) if match else None


def halted() -> Optional[dict]:
    try:
        record = json.loads(_halt_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("reason") else None


def halt(reason: str, *, source: str = "", now: Optional[float] = None) -> dict:
    """Stop every LinkedIn call until the owner resumes. The first cause is
    kept: it is the one he needs to read. Returns the record in force."""
    current = halted()
    if current is not None:
        return current
    record = {"reason": re.sub(r"\s+", " ", str(reason or "LinkedIn objected"))[:300],
              "source": str(source or "")[:120],
              "at": time.time() if now is None else now}
    path = _halt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)
    return record


def resume() -> None:
    try:
        _halt_path().unlink()
    except FileNotFoundError:
        pass


def halted_reason(record: dict) -> str:
    """The sentence a refused call gets back."""
    return (f"LinkedIn is stopped, sir: LinkedIn objected at {when(record.get('at'))} "
            f"(\"{record.get('reason', '')[:120]}\"). Nothing on LinkedIn runs until you "
            f"look at it yourself and press Resume on the Business desk.")


def status() -> dict:
    return {"limits": limits(), "halted": halted(), "connector_servers": sorted(connector_servers())}
