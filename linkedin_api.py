"""LinkedIn's official API, as a provider on the business desk.

Owner decision, 2026-10-08: post through LinkedIn's own API instead of
driving the owner's signed-in browser, which LinkedIn's rules forbid. This
is the `linkedin` provider of `business_providers`, so it inherits the desk's
gate whole: `business_propose` stages ONE card holding the exact request,
the owner's Approve sends it ONCE (`business_api.decide`, a compare-and-swap),
and the receipt — the post's own link — is written on the card. The interim
limits and the stop (`linkedin_guard`) are checked when a card is staged and
again before it is sent.

What it can do, verified against LinkedIn's Marketing API docs (2026-09):

  * a post on the owner's profile (Posts API, `w_member_social`, the
    self-serve "Share on LinkedIn" product), with one image (Images API) or
    one MP4 video (Videos API);
  * a post on the company page (`w_organization_social`), once LinkedIn
    has granted the Community Management API — a separate, vetted app;
  * a comment on a post, by its link.

The owner creates the app(s) and signs in himself (`authorize_url` ->
LinkedIn -> `complete`); JARVIS never sees his password. Access tokens last
60 days and LinkedIn issues refresh tokens only to selected partners, so he
reconnects when one lapses. Tokens are kept in JARVIS's private data folder
(`token_path`), never in a card, a receipt, a log or the repo.

Media comes from a local folder the owner named (`LINKEDIN_MEDIA_ROOTS`),
bound to the card by its sha256: what is uploaded is checked against the
digest the owner saw, and a changed file is not posted.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode

import httpx

import data_paths
import linkedin_guard

API = "https://api.linkedin.com"
AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
# The Marketing API version (`Linkedin-Version: YYYYMM`). Versions sunset
# about a year after release; the owner can pin another with
# LINKEDIN_API_VERSION.
DEFAULT_VERSION = "202609"
DEFAULT_REDIRECT = "https://localhost:8340/api/linkedin/callback"
MEMBER_SCOPES = ("openid", "profile", "w_member_social")
ORG_SCOPES = ("w_organization_social", "r_organization_social")

TEXT_MAX = 3000           # a post's commentary
COMMENT_MAX = 1250
ALT_MAX = 4086
TITLE_MAX = 100
IMAGE_MAX = 36_152_320    # pixels, per the Images API; bytes are bounded below
FILE_MAX = 500 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
VIDEO_TYPES = {".mp4": "video/mp4"}

# One whole post, uploads and waiting included. Longer than the desk's 18s
# for other providers: a video is uploaded in 4 MB parts and processed.
TIMEOUT_SEC = 240
POLL_SEC = 3.0
READY_WAIT_SEC = 120
# A post refused because its media is still processing is refused, so
# asking again cannot make a second post. Bounded all the same.
MEDIA_RETRIES = 4
# A request that could not connect (a failed name lookup, a refused
# connect) never reached LinkedIn, so every step before the one that
# publishes is asked again. The router's DNS failed this way for a few
# minutes on 2026-10-09 and two approved cards failed with it. A connect
# that timed out is not: four minute-long waits would outlast TIMEOUT_SEC.
CONNECT_RETRIES = 3
CONNECT_RETRY_SEC = 2.0

log = logging.getLogger("jarvis.linkedin")

_STATE_TTL = 600
_states: dict[str, tuple[str, float]] = {}

ACCOUNTS = ("member", "organization")
_ACTIVITY = re.compile(r"activity[-:](\d{10,25})")
_URN = re.compile(r"urn:li:(activity|share|ugcPost):(\d{1,25})")


# The desk's own error, so an uncertain send is recorded as `unknown`
# exactly as every other provider's is.
from business_providers import ProviderError  # noqa: E402


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def api_version() -> str:
    version = _env("LINKEDIN_API_VERSION") or DEFAULT_VERSION
    if not re.fullmatch(r"20\d{4}", version):
        raise ValueError("LINKEDIN_API_VERSION must be YYYYMM")
    return version


def redirect_uri() -> str:
    return _env("LINKEDIN_REDIRECT_URI") or DEFAULT_REDIRECT


def _client(account: str) -> tuple[str, str]:
    if account == "organization":
        return _env("LINKEDIN_ORG_CLIENT_ID"), _env("LINKEDIN_ORG_CLIENT_SECRET")
    return _env("LINKEDIN_CLIENT_ID"), _env("LINKEDIN_CLIENT_SECRET")


def organization_urn() -> Optional[str]:
    org = _env("LINKEDIN_ORGANIZATION_ID")
    return f"urn:li:organization:{org}" if re.fullmatch(r"\d{1,20}", org) else None


def media_roots() -> list[Path]:
    roots = []
    for raw in _env("LINKEDIN_MEDIA_ROOTS").split(os.pathsep):
        if raw.strip():
            try:
                roots.append(Path(raw.strip()).resolve())
            except OSError:
                continue
    return roots


# --- tokens, kept privately ------------------------------------------------------

def token_path() -> Path:
    return data_paths.data_dir() / "linkedin-tokens.json"


def _tokens() -> dict:
    try:
        value = json.loads(token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_token(account: str, token: dict) -> None:
    """Keep one account's token. Written whole and replaced, in the data
    folder JARVIS keeps private to the owner's account."""
    if account not in ACCOUNTS:
        raise ValueError("Unknown LinkedIn account")
    tokens = _tokens()
    tokens[account] = token
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh)
    os.replace(tmp, path)


def token_for(account: str) -> Optional[dict]:
    """The account's token while it is valid, else None."""
    token = _tokens().get(account)
    if not isinstance(token, dict) or not token.get("access_token"):
        return None
    try:
        if float(token.get("expires_at") or 0) <= time.time():
            return None
    except (TypeError, ValueError):
        return None
    return token


def status() -> dict:
    """What the desk shows: per account, connected or not, whose, and until
    when — never a token."""
    out = {}
    for account in ACCOUNTS:
        token = token_for(account)
        client_id, secret = _client(account)
        out[account] = {"app_configured": bool(client_id and secret),
                        "connected": token is not None,
                        "name": (token or {}).get("name"),
                        "expires_at": (token or {}).get("expires_at")}
    out["organization"]["page"] = organization_urn()
    out["version"] = _env("LINKEDIN_API_VERSION") or DEFAULT_VERSION
    out["media_roots"] = [str(root) for root in media_roots()]
    return out


# --- the owner's sign-in ---------------------------------------------------------

def authorize_url(account: str) -> str:
    """Where the owner's browser goes to sign in and consent, himself."""
    if account not in ACCOUNTS:
        raise ValueError("Unknown LinkedIn account")
    client_id, secret = _client(account)
    if not client_id or not secret:
        names = ("LINKEDIN_ORG_CLIENT_ID and LINKEDIN_ORG_CLIENT_SECRET" if account == "organization"
                 else "LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET")
        raise ValueError(f"Put {names} from your LinkedIn app in .env first")
    now = time.time()
    for stale in [key for key, (_a, at) in _states.items() if now - at > _STATE_TTL]:
        _states.pop(stale, None)
    state = secrets.token_urlsafe(32)
    _states[state] = (account, now)
    scopes = ORG_SCOPES if account == "organization" else MEMBER_SCOPES
    return AUTHORIZE + "?" + urlencode({"response_type": "code", "client_id": client_id,
                                        "redirect_uri": redirect_uri(), "state": state,
                                        "scope": " ".join(scopes)})


async def complete(state: str, code: str) -> str:
    """LinkedIn sent the owner back with `code`: exchange it, learn whose it
    is, keep the token. Only a state this process issued, once, in the last
    ten minutes. Returns the account connected."""
    issued = _states.pop(str(state or ""), None)
    if issued is None or time.time() - issued[1] > _STATE_TTL:
        raise ValueError("That sign-in's state is not one JARVIS issued, or it took too long; start it again")
    account = issued[0]
    if not isinstance(code, str) or not 1 <= len(code) <= 2000:
        raise ValueError("LinkedIn sent no code")
    client_id, secret = _client(account)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code, "client_id": client_id,
            "client_secret": secret, "redirect_uri": redirect_uri()})
        if response.status_code != 200:
            raise ValueError(f"LinkedIn refused the sign-in (HTTP {response.status_code})")
        grant = response.json()
        access = grant.get("access_token")
        if not isinstance(access, str) or not access:
            raise ValueError("LinkedIn returned no access token")
        token = {"access_token": access,
                 "expires_at": time.time() + float(grant.get("expires_in") or 0),
                 "scope": str(grant.get("scope") or "")}
        if account == "member":
            who = await client.get(API + "/v2/userinfo", headers={"Authorization": "Bearer " + access})
            if who.status_code != 200 or not who.json().get("sub"):
                raise ValueError("LinkedIn did not say whose account this is")
            info = who.json()
            token["person"] = "urn:li:person:" + str(info["sub"])
            token["name"] = str(info.get("name") or "")[:120]
        else:
            token["name"] = "company page"
    save_token(account, token)
    return account


# --- what a card may hold ----------------------------------------------------------

def post_urn(url: str) -> str:
    """The post a link names: `.../feed/update/urn:li:activity:N/`,
    `.../posts/slug-activity-N-abcd`, or a bare URN."""
    text = str(url or "")
    if not (text.startswith("https://www.linkedin.com/") or text.startswith("urn:li:")):
        raise ValueError("A comment needs the post's LinkedIn link")
    found = _URN.search(text)
    if found:
        return f"urn:li:{found.group(1)}:{found.group(2)}"
    found = _ACTIVITY.search(text)
    if found:
        return f"urn:li:activity:{found.group(1)}"
    raise ValueError("That link does not name a LinkedIn post")


def _media_file(media: dict) -> Path:
    path_text = media.get("path")
    if not isinstance(path_text, str) or not path_text:
        raise ValueError("Media needs a path in the media folder")
    try:
        path = Path(path_text).resolve(strict=True)
    except OSError:
        raise ValueError("That media file does not exist") from None
    roots = media_roots()
    if not roots or not any(path.is_relative_to(root) for root in roots):
        raise ValueError("Media must come from the LinkedIn media folder (LINKEDIN_MEDIA_ROOTS)")
    if not path.is_file():
        raise ValueError("Media must be a file")
    return path


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _check_media(media) -> None:
    if not isinstance(media, dict):
        raise ValueError("media must be an object")
    allowed = {"kind", "path", "sha256", "alt_text", "title"}
    if set(media) - allowed:
        raise ValueError(f"media has unknown fields: {sorted(set(media) - allowed)}")
    kind = media.get("kind")
    if kind not in ("image", "video"):
        raise ValueError("media kind must be image or video")
    path = _media_file(media)
    types = IMAGE_TYPES if kind == "image" else VIDEO_TYPES
    if path.suffix.lower() not in types:
        raise ValueError(f"A {kind} must be one of {sorted(types)}; check the media kind")
    size = path.stat().st_size
    if not 1 <= size <= FILE_MAX:
        raise ValueError("Media file size is outside LinkedIn's limits")
    digest = media.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("media needs the file's sha256")
    if _digest(path) != digest:
        raise ValueError("The media file does not match its sha256")
    alt = media.get("alt_text")
    if alt is not None and (kind != "image" or not isinstance(alt, str) or len(alt) > ALT_MAX):
        raise ValueError("alt_text is for an image, up to 4086 characters")
    title = media.get("title")
    if title is not None and (kind != "video" or not isinstance(title, str) or not 1 <= len(title) <= TITLE_MAX):
        raise ValueError("title is for a video, 1-100 characters")


def _check_text(value, limit: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A text is required")
    if len(value) > limit:
        raise ValueError(f"The text is over {limit} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
        raise ValueError("The text holds control characters")


def author(account: str) -> str:
    """Whose name a post goes out under; raises when that account cannot post."""
    if account == "organization":
        org = organization_urn()
        if org is None or token_for("organization") is None:
            raise ValueError("The company page is not connected: it needs LinkedIn's Community "
                             "Management API, LINKEDIN_ORGANIZATION_ID and its own sign-in")
        return org
    token = token_for("member")
    if token is None or not token.get("person"):
        raise ValueError("Connect LinkedIn first: the owner signs in from the Business desk")
    return token["person"]


def validate(operation: str, body) -> dict:
    """What a `linkedin` card may hold — exactly these fields, nothing else."""
    if not isinstance(body, dict):
        raise ValueError("Invalid LinkedIn request")
    account = body.get("account", "member")
    if account not in ACCOUNTS:
        raise ValueError("account must be member or organization")
    if operation == "post":
        allowed = {"account", "text", "media"}
        if set(body) - allowed:
            raise ValueError(f"A LinkedIn post has unknown fields: {sorted(set(body) - allowed)}")
        _check_text(body.get("text"), TEXT_MAX)
        if "media" in body:
            _check_media(body["media"])
    elif operation == "comment":
        allowed = {"account", "text", "post_url"}
        if set(body) - allowed:
            raise ValueError(f"A LinkedIn comment has unknown fields: {sorted(set(body) - allowed)}")
        _check_text(body.get("text"), COMMENT_MAX)
        post_urn(body.get("post_url"))
    else:
        raise ValueError("LinkedIn operations are post and comment")
    author(account)
    return body


def identity() -> dict:
    """What an approval is bound to: who posts. A card staged for one
    profile cannot be sent as another."""
    member = token_for("member")
    if member is None or not member.get("person"):
        raise ValueError("Connect LinkedIn first: the owner signs in from the Business desk")
    return {"member": member["person"], "organization": organization_urn(), "version": api_version()}


def configured() -> bool:
    try:
        identity()
    except ValueError:
        return False
    client_id, secret = _client("member")
    return bool(client_id and secret)


# --- sending -------------------------------------------------------------------------

_RESERVED = re.compile(r"([|{}@\[\]()<>\\*_~])|#(?![A-Za-z])")


def little(text: str) -> str:
    """The owner's words as plain text in LinkedIn's `little` format: every
    reserved character escaped, so `(` or `*` cannot become markup or cut
    the post short. A `#` before a letter stays a hashtag."""
    return _RESERVED.sub(lambda m: "\\" + (m.group(1) or "#"), text)


def _headers(token: dict, json_body: bool = True) -> dict:
    headers = {"Authorization": "Bearer " + token["access_token"], "Linkedin-Version": api_version(),
               "X-Restli-Protocol-Version": "2.0.0"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _stop_if_linkedin_objects(status: int, text: str) -> None:
    """401 (the sign-in failed or lapsed), 429 (too many), 999 (LinkedIn's
    bot refusal) or a challenge in the answer: stop all of LinkedIn."""
    objection = linkedin_guard.looks_like_challenge(text)
    if status in (401, 429, 999) or objection:
        linkedin_guard.halt(f"LinkedIn API answered HTTP {status}" + (f": {objection}" if objection else ""),
                            source="linkedin_api")


async def _send(client: httpx.AsyncClient, method: str, url: str, *, sent: bool = False, **kwargs) -> httpx.Response:
    """One request. `sent` marks the one that publishes: an unanswered one
    of those may have worked, so it is uncertain; anything earlier is not,
    and is asked again if it could not even connect."""
    for attempt in range(CONNECT_RETRIES + 1):
        try:
            response = await client.request(method, url, **kwargs)
            break
        except httpx.HTTPError as error:
            if isinstance(error, httpx.ConnectError) and not sent and attempt < CONNECT_RETRIES:
                log.warning("linkedin: %s %s could not connect (%s: %s); trying again",
                            method, _host(url), type(error).__name__, error)
                await asyncio.sleep(CONNECT_RETRY_SEC * (attempt + 1))
                continue
            log.warning("linkedin: %s %s failed: %s: %s", method, _host(url), type(error).__name__, error)
            raise ProviderError("LinkedIn did not answer; check the profile before sending again."
                                if sent else "Could not reach LinkedIn; nothing was posted.",
                                uncertain=sent) from None
    if not 200 <= response.status_code < 300:
        body = response.text[:2000]
        _stop_if_linkedin_objects(response.status_code, body)
        raise ProviderError(f"LinkedIn returned HTTP {response.status_code}"
                            + (" — every LinkedIn action is now stopped until the owner resumes it"
                               if linkedin_guard.halted() else "") + ".",
                            uncertain=sent and response.status_code >= 500)
    return response


def _host(url: str) -> str:
    """The host alone for the log: upload addresses carry signed query strings."""
    return httpx.URL(url).host


async def _upload_image(client, token, owner: str, data: bytes) -> str:
    init = await _send(client, "POST", f"{API}/rest/images?action=initializeUpload",
                       headers=_headers(token), json={"initializeUploadRequest": {"owner": owner}})
    value = init.json().get("value") or {}
    url, image = value.get("uploadUrl"), value.get("image")
    if not isinstance(url, str) or not url.startswith("https://") or not isinstance(image, str):
        raise ProviderError("LinkedIn did not give an image upload address; nothing was posted.")
    await _send(client, "PUT", url, content=data,
                headers={"Authorization": "Bearer " + token["access_token"],
                         "Content-Type": "application/octet-stream"})
    return image


async def _upload_video(client, token, owner: str, data: bytes) -> str:
    init = await _send(client, "POST", f"{API}/rest/videos?action=initializeUpload",
                       headers=_headers(token), json={"initializeUploadRequest": {
                           "owner": owner, "fileSizeBytes": len(data),
                           "uploadCaptions": False, "uploadThumbnail": False}})
    value = init.json().get("value") or {}
    video, parts = value.get("video"), value.get("uploadInstructions") or []
    if not isinstance(video, str) or not parts:
        raise ProviderError("LinkedIn did not give video upload addresses; nothing was posted.")
    etags = []
    for part in parts:
        first, last, url = int(part["firstByte"]), int(part["lastByte"]), part["uploadUrl"]
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ProviderError("LinkedIn gave a bad video upload address; nothing was posted.")
        answer = await _send(client, "PUT", url, content=data[first:last + 1],
                             headers={"Content-Type": "application/octet-stream"})
        etag = answer.headers.get("etag")
        if not etag:
            raise ProviderError("LinkedIn did not confirm a video part; nothing was posted.")
        etags.append(etag)
    await _send(client, "POST", f"{API}/rest/videos?action=finalizeUpload", headers=_headers(token),
                json={"finalizeUploadRequest": {"video": video, "uploadToken": value.get("uploadToken") or "",
                                                "uploadedPartIds": etags}})
    deadline = time.monotonic() + READY_WAIT_SEC
    while time.monotonic() < deadline:
        try:
            state = await client.get(f"{API}/rest/videos/{quote(video, safe='')}", headers=_headers(token, False))
        except httpx.HTTPError:
            break
        if state.status_code != 200:
            break          # a write-only token cannot read it back; the post's own retry covers it
        status = (state.json() or {}).get("status")
        if status == "AVAILABLE":
            break
        if status == "PROCESSING_FAILED":
            raise ProviderError("LinkedIn could not process the video; nothing was posted.")
        await asyncio.sleep(POLL_SEC)
    return video


def _media_bytes(media: dict) -> bytes:
    path = _media_file(media)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != media.get("sha256"):
        raise ProviderError("The media file changed since the card was approved; nothing was posted.")
    return data


async def _post(client, token, body: dict) -> dict:
    account = body.get("account", "member")
    owner = author(account)
    request = {"author": owner, "commentary": little(body["text"]), "visibility": "PUBLIC",
               "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                                "thirdPartyDistributionChannels": []},
               "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
    media = body.get("media")
    if media:
        data = _media_bytes(media)
        if media["kind"] == "image":
            media_id = await _upload_image(client, token, owner, data)
            request["content"] = {"media": {"id": media_id, **({"altText": media["alt_text"]}
                                                                if media.get("alt_text") else {})}}
        else:
            media_id = await _upload_video(client, token, owner, data)
            request["content"] = {"media": {"id": media_id, **({"title": media["title"]}
                                                                if media.get("title") else {})}}
    for attempt in range(MEDIA_RETRIES + 1):
        try:
            response = await _send(client, "POST", f"{API}/rest/posts", headers=_headers(token),
                                   json=request, sent=True)
            break
        except ProviderError as error:
            # A 400 for media still processing is LinkedIn refusing the post:
            # nothing was created, so asking again is safe.
            if media and "HTTP 400" in str(error) and attempt < MEDIA_RETRIES and not linkedin_guard.halted():
                await asyncio.sleep(POLL_SEC * (attempt + 1))
                continue
            raise
    urn = response.headers.get("x-restli-id") or ""
    if not _URN.fullmatch(urn):
        raise ProviderError("LinkedIn accepted the post but did not say which one; check the profile.",
                            uncertain=True)
    return {"post_urn": urn, "post_url": f"https://www.linkedin.com/feed/update/{urn}/",
            "account": account, "media": (media or {}).get("kind")}


async def _comment(client, token, body: dict) -> dict:
    account = body.get("account", "member")
    actor = author(account)
    target = post_urn(body["post_url"])
    response = await _send(client, "POST", f"{API}/rest/socialActions/{quote(target, safe='')}/comments",
                           headers=_headers(token), sent=True,
                           json={"actor": actor, "object": target, "message": {"text": body["text"]}})
    try:
        urn = (response.json() or {}).get("commentUrn")
    except ValueError:
        urn = None
    return {"comment_urn": urn or response.headers.get("x-restli-id"), "post": target,
            "post_url": body["post_url"], "account": account}


async def perform(operation: str, body: dict) -> dict:
    """Send one approved card. Raises ProviderError; `uncertain` only when
    the request that publishes went out and its answer did not come back."""
    stopped = linkedin_guard.halted()
    if stopped is not None:
        raise ProviderError(linkedin_guard.halted_reason(stopped))
    try:
        validate(operation, body)
    except ValueError as error:
        # Something changed since the card was approved — a media file, a
        # lapsed sign-in. Nothing has been sent.
        raise ProviderError(f"{error}; nothing was posted.") from None
    account = body.get("account", "member")
    token = token_for(account)
    if token is None:
        raise ProviderError("The LinkedIn sign-in for that account has lapsed; reconnect it on the desk.")
    async with asyncio.timeout(TIMEOUT_SEC), httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
        if operation == "post":
            return await _post(client, token, body)
        return await _comment(client, token, body)


def summary(receipt: dict, operation: str) -> str:
    """The sentence on the card, the phone and the desk."""
    if operation == "comment":
        return f"Commented on {receipt.get('post_url', 'the post')}."
    return f"Posted. {receipt.get('post_url', '')}".strip()
