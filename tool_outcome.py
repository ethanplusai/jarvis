"""What a released call on a user's own MCP server came back with, for its card.

The gate decides BEFORE a call; this is what happened AFTER it. The Claude
CLI hands both to a hook (`pretool_hook.py --report`), and `/internal/
posttool` writes this module's summary onto the card that released the call.

Without it a card's last word was `submitted`, which means "let through",
not "posted". Measured live, 2026-10-01: the first confirmed post failed
mid-typing with a bare `Error calling tool 'create_post'`, the card said
`submitted`, and three hours later the identical bytes were put to the
owner as a brand-new card. What the connector said is the one thing that
tells a retry from a duplicate, and it was nowhere JARVIS could read it back.

Pure: no I/O, cheap to test. Verified against CLI 2.1.270: `PostToolUse`
carries `tool_response` as the call's content blocks, `PostToolUseFailure`
carries `error` as a string.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

# Bounded: an outcome is a receipt, not a transcript.
MESSAGE_MAX = 400
FIELD_MAX = 300
URL_MAX = 500

# A connector's status, and whether it means something reached other
# people. `posted`/`sent`/`commented`/`replied` say so outright; a field
# named the same way (`posted: true`) says so too.
_DONE_FLAGS = ("posted", "sent", "commented", "replied", "connected", "published")


def _text_of(response: Any) -> str:
    """The text a call answered with: its text blocks, joined."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        if isinstance(response.get("content"), list):
            return _text_of(response["content"])
        return json.dumps(response, ensure_ascii=False, default=repr)
    if isinstance(response, list):
        parts = [b.get("text", "") for b in response
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _answer(response: Any) -> Optional[dict]:
    """The connector's answer as an object, when it is one."""
    if isinstance(response, dict) and not isinstance(response.get("content"), list):
        return response
    try:
        value = json.loads(_text_of(response))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _clip(value: Any, limit: int = FIELD_MAX) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _url(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith("https://") and len(value) <= URL_MAX:
        return value
    return None


def _flag(answer: dict, key: str) -> Optional[bool]:
    value = answer.get(key)
    return value if isinstance(value, bool) else None


def summarise(event: str, response: Any, error: Optional[str], *, now: Optional[float] = None) -> dict:
    """One bounded receipt: `status`, `posted` (True / False / None when it
    cannot be told), `retry_safe`, `url`, `post_url`, `message` — a
    sentence for the owner — and `at`."""
    out: dict = {"status": "done", "posted": None, "retry_safe": None,
                 "at": time.time() if now is None else now}
    if event == "PostToolUseFailure" or error is not None:
        detail = _clip(error or "no detail", 200)
        out.update(status="error", error=detail, message=_clip(
            f"The connector reported an error: {detail}. It may or may not have gone "
            f"out — check before sending it again.", MESSAGE_MAX))
        return out
    answer = _answer(response)
    if answer is None:
        said = _clip(_text_of(response), 200) or "nothing"
        out["message"] = _clip(f"Done. The connector said: {said}", MESSAGE_MAX)
        return out
    status = _clip(answer.get("status") or "done", 40).lower()
    out["status"] = status
    out["retry_safe"] = _flag(answer, "retry_safe")
    done = [_flag(answer, key) for key in _DONE_FLAGS if _flag(answer, key) is not None]
    out["posted"] = (True if any(done) else False) if done else (True if status in _DONE_FLAGS else None)
    for key in ("url", "post_url"):
        if _url(answer.get(key)):
            out[key] = answer[key]
    said = _clip(answer.get("message") or answer.get("reason") or "", 240)
    if status in _DONE_FLAGS or out["posted"] is True:
        where = out.get("post_url") or ""
        message = f"{status.capitalize() if status in _DONE_FLAGS else 'Done'}." + (f" {where}" if where else "")
    elif status == "failed":
        message = f"It failed: {said or 'no detail'}. Check before trying again; it may have gone through."
    elif status == "rehearsed":
        message = "Rehearsed only; nothing was published."
    elif status == "refused":
        message = f"The connector refused it, and nothing was sent: {said or 'no reason given'}."
    else:
        message = f"Done ({status})." + (f" {said}" if said else "")
    out["message"] = _clip(message, MESSAGE_MAX)
    return out


def with_permalink(outcome: dict, post_url: str) -> dict:
    """An outcome with the post's own link found afterwards, kept beside
    what the connector first said rather than instead of it."""
    merged = dict(outcome or {})
    merged["post_url"] = post_url
    base = _clip(merged.get("message") or "Posted.", MESSAGE_MAX - len(post_url) - 8)
    merged["message"] = base if post_url in base else f"{base} Link: {post_url}"
    return merged


def found_permalink(response: Any) -> Optional[str]:
    """The post link a read call FOUND (`status: found`, `post_url`), or None."""
    answer = _answer(response)
    if not answer or str(answer.get("status") or "").lower() != "found":
        return None
    return _url(answer.get("post_url"))


def _normal(text: str) -> str:
    text = str(text or "").replace("…", " ").casefold()
    text = re.sub(r"[‘’“”\"'`]", "", text)
    text = re.sub(r"[‐-―-]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def quotes(quote: Any, text: Any) -> bool:
    """Whether `quote` is copied from `text`, as the connector matches one:
    case, quotes, dashes and whitespace aside. Twenty characters at least,
    so a stray word never ties a link to the wrong post."""
    q = _normal(quote)
    return len(q) >= 20 and q in _normal(text)
