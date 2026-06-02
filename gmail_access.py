"""
JARVIS Gmail — READ-ONLY access via the Gmail API (for the morning briefing).

Scope is gmail.readonly only: JARVIS can read but never send, delete or modify,
consistent with the project's read-only-mail rule. OAuth credentials live in
gmail_credentials.json; the user token is cached in gmail_token.json (both
gitignored). First use runs a one-time browser consent.
"""

import asyncio
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger("jarvis.gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE = Path(__file__).resolve().parent
CREDS_FILE = BASE / "gmail_credentials.json"
TOKEN_FILE = BASE / "gmail_token.json"


def _load_creds(interactive: bool = False) -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        return creds
    if not interactive:
        raise RuntimeError("Gmail not authorized yet (run the one-time auth).")
    # One-time interactive consent (opens a browser).
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def _service(interactive: bool = False):
    return build("gmail", "v1", credentials=_load_creds(interactive), cache_discovery=False)


def _header(headers, name):
    for h in headers:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _short_sender(value: str) -> str:
    # "Jane Doe <jane@x.com>" -> "Jane Doe"; else the address local part.
    if "<" in value:
        return value.split("<")[0].strip().strip('"') or value
    if "@" in value:
        return value.split("@")[0]
    return value


def _fetch_briefing() -> dict:
    svc = _service()
    # Exact inbox unread count.
    label = svc.users().labels().get(userId="me", id="INBOX").execute()
    unread_total = label.get("messagesUnread", 0)
    # "Important to reply to" = unread in the Primary category (real correspondence,
    # not promotions/social/updates).
    res = svc.users().messages().list(
        userId="me", q="is:unread in:inbox category:primary", maxResults=5
    ).execute()
    important = []
    for item in res.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=item["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        important.append({
            "from": _short_sender(_header(headers, "from")),
            "subject": _header(headers, "subject") or "(no subject)",
            "snippet": (msg.get("snippet", "") or "")[:140],
        })
    return {"ok": True, "unread_total": unread_total, "primary_unread": len(important), "important": important}


async def get_briefing_mail() -> dict:
    """Async wrapper used by the morning briefing."""
    try:
        return await asyncio.to_thread(_fetch_briefing)
    except Exception as e:
        log.warning(f"Gmail briefing fetch failed: {e}")
        return {"ok": False, "reason": str(e)}


if __name__ == "__main__":
    # One-time authorization: opens a browser for consent, caches the token.
    _load_creds(interactive=True)
    print("Gmail authorized — token saved to gmail_token.json")
    import json
    print(json.dumps(_fetch_briefing(), indent=2)[:800])
