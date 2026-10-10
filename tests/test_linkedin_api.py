"""LinkedIn's official API, as a provider on the business desk (`linkedin_api.py`).

Owner decision, 2026-10-08: move JARVIS's posting off the browser connector
and onto LinkedIn's own API — Posts API for the founder's profile
(`w_member_social`), the same for the company page once LinkedIn grants
Community Management access (`w_organization_social`), images and video
through the Images and Videos APIs — and keep it inside JARVIS's existing
gate: one card, one post, the approval spent once, the outcome and the post's
link on the card, and the interim limits and the stop (`linkedin_guard`).

The owner creates the LinkedIn app and does the OAuth sign-in himself; JARVIS
never sees his password. Tokens live in JARVIS's private data folder, never
in the repo, and never in a card, a receipt or a log.

Every request here goes to a fake LinkedIn (`httpx.MockTransport`).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import importlib
import json
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse, unquote

import httpx
import pytest

TEXT = "Your site ranks #1 on Google and ChatGPT has never heard of you.\n\nBoth are normal."
PERSON = "urn:li:person:abc123"
NOON = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()


class FakeLinkedIn:
    """Answers as LinkedIn's docs say it does (Marketing API 2026-09)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail: dict = {}          # (method, path prefix) -> list of responses to give first
        self.video_status = ["AVAILABLE"]

    def _queued(self, request):
        for (method, prefix), answers in self.fail.items():
            if request.method == method and request.url.path.startswith(prefix) and answers:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queued = self._queued(request)
        if queued is not None:
            return queued
        path, query = request.url.path, parse_qs(request.url.query.decode())
        if path == "/oauth/v2/accessToken":
            return httpx.Response(200, json={"access_token": "tok-" + "x" * 40, "expires_in": 5184000,
                                             "scope": "openid,profile,w_member_social"})
        if path == "/v2/userinfo":
            return httpx.Response(200, json={"sub": "abc123", "name": "Tony Stark"})
        if path == "/rest/images" and query.get("action") == ["initializeUpload"]:
            return httpx.Response(200, json={"value": {
                "uploadUrl": "https://www.linkedin.com/dms-uploads/IMG1/uploaded-image/0",
                "uploadUrlExpiresAt": 1, "image": "urn:li:image:IMG1"}})
        if path.startswith("/dms-uploads/IMG1"):
            return httpx.Response(201)
        if path == "/rest/videos" and query.get("action") == ["initializeUpload"]:
            size = json.loads(request.content)["initializeUploadRequest"]["fileSizeBytes"]
            parts, first = [], 0
            while first < size:
                last = min(first + 4194303, size - 1)
                parts.append({"uploadUrl": f"https://www.linkedin.com/dms-uploads/VID1/part{len(parts)}",
                              "firstByte": first, "lastByte": last})
                first = last + 1
            return httpx.Response(200, json={"value": {"video": "urn:li:video:VID1", "uploadToken": "",
                                                       "uploadUrlsExpireAt": 1, "uploadInstructions": parts}})
        if path.startswith("/dms-uploads/VID1/part"):
            return httpx.Response(200, headers={"etag": f"etag-{path.rsplit('part', 1)[1]}"})
        if path == "/rest/videos" and query.get("action") == ["finalizeUpload"]:
            return httpx.Response(200)
        if path.startswith("/rest/videos/"):
            status = self.video_status.pop(0) if len(self.video_status) > 1 else self.video_status[0]
            return httpx.Response(200, json={"id": "urn:li:video:VID1", "status": status})
        if path == "/rest/posts":
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7000000000000000001"})
        if path.startswith("/rest/socialActions/"):
            return httpx.Response(201, headers={"x-restli-id": "7123"},
                                  json={"commentUrn": "urn:li:comment:(urn:li:activity:1,7123)"})
        return httpx.Response(404, json={"message": "not in the fake"})

    def calls(self, method, path):
        return [r for r in self.requests if r.method == method and r.url.path == path]


@pytest.fixture
def li(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "client-member")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "member-secret-value-123")
    monkeypatch.delenv("LINKEDIN_ORG_CLIENT_ID", raising=False)
    monkeypatch.delenv("LINKEDIN_ORG_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("LINKEDIN_ORGANIZATION_ID", raising=False)
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("LINKEDIN_MEDIA_ROOTS", str(media))
    for name in ("LINKEDIN_POSTS_PER_DAY", "LINKEDIN_COMMENTS_PER_DAY", "LINKEDIN_MIN_POST_GAP_HOURS"):
        monkeypatch.delenv(name, raising=False)
    import data_paths
    importlib.reload(data_paths)
    import business_store
    importlib.reload(business_store)
    business_store.init_db()
    import linkedin_guard
    importlib.reload(linkedin_guard)
    import linkedin_api
    importlib.reload(linkedin_api)
    fake = FakeLinkedIn()
    factory = httpx.AsyncClient
    monkeypatch.setattr(linkedin_api.httpx, "AsyncClient",
                        lambda **kw: factory(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setattr(linkedin_api, "POLL_SEC", 0.0)
    monkeypatch.setattr(linkedin_api, "CONNECT_RETRY_SEC", 0.0)
    linkedin_api._fake = fake
    linkedin_api._media = media
    return linkedin_api


def _connect(li, account="member"):
    li.save_token(account, {"access_token": "tok-" + "y" * 40, "expires_at": time.time() + 86400 * 50,
                            "scope": "openid profile w_member_social", "person": PERSON, "name": "Tony Stark"})


def _file(li, name, data):
    path = li._media / name
    path.write_bytes(data)
    return str(path), hashlib.sha256(data).hexdigest()


def _run(coro):
    return asyncio.run(coro)


# --- what a card may ask for -----------------------------------------------------

def test_a_text_post_is_valid(li):
    _connect(li)
    assert li.validate("post", {"account": "member", "text": TEXT})


@pytest.mark.parametrize("body,needle", [
    ({"account": "member"}, "text"),
    ({"account": "member", "text": ""}, "text"),
    ({"account": "member", "text": "x" * 3001}, "3000"),
    ({"account": "page", "text": "hi"}, "account"),
    ({"account": "member", "text": "hi", "visibility": "CONNECTIONS"}, "unknown"),
    ({"account": "member", "text": "hi", "access_token": "abc"}, "unknown"),
])
def test_a_card_that_asks_for_anything_else_is_refused(li, body, needle):
    _connect(li)
    with pytest.raises(ValueError) as error:
        li.validate("post", body)
    assert needle in str(error.value).lower()


def test_media_must_come_from_the_media_folder_and_match_its_digest(li, tmp_path):
    _connect(li)
    path, digest = _file(li, "card.png", b"\x89PNG" + b"0" * 200)
    good = {"account": "member", "text": TEXT,
            "media": {"kind": "image", "path": path, "sha256": digest, "alt_text": "A chart"}}
    assert li.validate("post", good)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(b"\x89PNG")
    with pytest.raises(ValueError, match="media folder"):
        li.validate("post", {**good, "media": {**good["media"], "path": str(outside),
                                               "sha256": hashlib.sha256(b"\x89PNG").hexdigest()}})
    with pytest.raises(ValueError, match="sha256"):
        li.validate("post", {**good, "media": {**good["media"], "sha256": "0" * 64}})
    with pytest.raises(ValueError, match="kind"):
        li.validate("post", {**good, "media": {**good["media"], "kind": "video"}})


def test_the_company_page_needs_its_own_connection(li, monkeypatch):
    _connect(li)
    with pytest.raises(ValueError, match="company page"):
        li.validate("post", {"account": "organization", "text": TEXT})
    monkeypatch.setenv("LINKEDIN_ORGANIZATION_ID", "123456")
    _connect(li, "organization")
    assert li.validate("post", {"account": "organization", "text": TEXT})


def test_a_comment_names_its_post_by_link(li):
    _connect(li)
    body = {"account": "member", "text": "Two measurable things today.",
            "post_url": "https://www.linkedin.com/posts/stark-industries_ai-search-activity-7000000000000000002-Fb97"}
    assert li.validate("comment", body)
    assert li.post_urn(body["post_url"]) == "urn:li:activity:7000000000000000002"
    assert li.post_urn("https://www.linkedin.com/feed/update/urn:li:share:42/") == "urn:li:share:42"
    with pytest.raises(ValueError, match="post"):
        li.validate("comment", {**body, "post_url": "https://example.com/x"})


# --- doing it ---------------------------------------------------------------------

def test_a_text_post_is_one_request_with_the_exact_text(li):
    _connect(li)
    receipt = _run(li.perform("post", {"account": "member", "text": TEXT}))
    [request] = li._fake.calls("POST", "/rest/posts")
    body = json.loads(request.content)
    assert body["author"] == PERSON
    assert body["commentary"] == li.little(TEXT), "the exact approved text, nothing added"
    assert re.sub(r"\\(.)", r"\1", body["commentary"]) == TEXT, "and it reads back as the owner wrote it"
    assert body["visibility"] == "PUBLIC" and body["lifecycleState"] == "PUBLISHED"
    assert body["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert request.headers["Linkedin-Version"] == li.api_version()
    assert request.headers["Authorization"].startswith("Bearer tok-")
    assert receipt["post_urn"] == "urn:li:share:7000000000000000001"
    assert receipt["post_url"] == "https://www.linkedin.com/feed/update/urn:li:share:7000000000000000001/"
    assert "tok-" not in json.dumps(receipt), "a token never lands in a receipt"


def test_little_formats_reserved_characters_are_escaped(li):
    """The Posts API reads `commentary` as "little" text: an unescaped ( or #
    would turn the owner's words into markup or drop them."""
    _connect(li)
    _run(li.perform("post", {"account": "member", "text": "Rank #1 (really) @you [x]"}))
    [request] = li._fake.calls("POST", "/rest/posts")
    assert json.loads(request.content)["commentary"] == r"Rank \#1 \(really\) \@you \[x\]"


def test_an_image_post_uploads_then_posts_with_its_alt_text(li):
    _connect(li)
    data = b"\x89PNG" + b"1" * 500
    path, digest = _file(li, "card.png", data)
    _run(li.perform("post", {"account": "member", "text": TEXT, "media": {
        "kind": "image", "path": path, "sha256": digest, "alt_text": "Four failure modes"}}))
    [init] = [r for r in li._fake.requests if r.url.path == "/rest/images"]
    assert json.loads(init.content) == {"initializeUploadRequest": {"owner": PERSON}}
    [put] = [r for r in li._fake.requests if r.method == "PUT"]
    assert put.content == data
    [post] = li._fake.calls("POST", "/rest/posts")
    assert json.loads(post.content)["content"] == {"media": {"id": "urn:li:image:IMG1",
                                                             "altText": "Four failure modes"}}


def test_a_video_post_uploads_every_part_finalizes_and_waits_until_ready(li):
    _connect(li)
    data = b"\x00\x00\x00\x18ftypmp42" + b"v" * (5 * 1024 * 1024)
    path, digest = _file(li, "clip.mp4", data)
    li._fake.video_status = ["PROCESSING", "AVAILABLE"]
    _run(li.perform("post", {"account": "member", "text": TEXT, "media": {
        "kind": "video", "path": path, "sha256": digest, "title": "Four failure modes"}}))
    puts = [r for r in li._fake.requests if r.method == "PUT"]
    assert b"".join(p.content for p in puts) == data, "every byte, in order"
    [final] = [r for r in li._fake.requests if r.url.path == "/rest/videos"
               and "finalizeUpload" in r.url.query.decode()]
    assert json.loads(final.content)["finalizeUploadRequest"]["uploadedPartIds"] == ["etag-0", "etag-1"]
    [post] = li._fake.calls("POST", "/rest/posts")
    assert json.loads(post.content)["content"] == {"media": {"id": "urn:li:video:VID1",
                                                             "title": "Four failure modes"}}


def test_media_changed_since_the_card_is_not_posted(li):
    _connect(li)
    path, digest = _file(li, "card.png", b"\x89PNG" + b"1" * 50)
    (li._media / "card.png").write_bytes(b"\x89PNG" + b"2" * 50)
    with pytest.raises(li.ProviderError) as error:
        _run(li.perform("post", {"account": "member", "text": TEXT,
                                 "media": {"kind": "image", "path": path, "sha256": digest}}))
    assert not error.value.uncertain
    assert li._fake.calls("POST", "/rest/posts") == [], "nothing the owner did not see goes out"


def test_a_lost_answer_to_the_post_itself_is_unknown_not_failed(li):
    """The post may exist: the owner checks before anything is sent again."""
    _connect(li)
    li._fake.fail[("POST", "/rest/posts")] = [httpx.ConnectError("reset")]
    with pytest.raises(li.ProviderError) as error:
        _run(li.perform("post", {"account": "member", "text": TEXT}))
    assert error.value.uncertain


def test_a_failed_name_lookup_before_publishing_is_tried_again(li):
    """2026-10-09: the router's DNS failed for a few minutes and two approved
    cards failed, once at the image's upload address and once at the upload
    itself. No connection means nothing reached LinkedIn, so it is asked again."""
    _connect(li)
    path, digest = _file(li, "card.png", b"\x89PNG" + b"5" * 50)
    lookup = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    li._fake.fail[("POST", "/rest/images")] = [lookup]
    li._fake.fail[("PUT", "/dms-uploads/IMG1")] = [lookup, lookup]
    receipt = _run(li.perform("post", {"account": "member", "text": TEXT,
                                       "media": {"kind": "image", "path": path, "sha256": digest}}))
    assert receipt["post_urn"] == "urn:li:share:7000000000000000001"
    assert len(li._fake.calls("POST", "/rest/posts")) == 1


def test_a_lookup_that_keeps_failing_is_a_clean_failure_with_nothing_posted(li):
    _connect(li)
    path, digest = _file(li, "card.png", b"\x89PNG" + b"6" * 50)
    li._fake.fail[("POST", "/rest/images")] = [httpx.ConnectError("getaddrinfo failed")] * 10
    with pytest.raises(li.ProviderError) as error:
        _run(li.perform("post", {"account": "member", "text": TEXT,
                                 "media": {"kind": "image", "path": path, "sha256": digest}}))
    assert not error.value.uncertain
    assert "nothing was posted" in str(error.value)
    assert len(li._fake.calls("POST", "/rest/images")) == li.CONNECT_RETRIES + 1
    assert li._fake.calls("POST", "/rest/posts") == []


def test_a_connect_that_timed_out_is_not_tried_again(li):
    """Each try can wait a minute; four would outlast the whole post's 240 s
    and leave the card unknown instead of cleanly failed."""
    _connect(li)
    path, digest = _file(li, "card.png", b"\x89PNG" + b"7" * 50)
    li._fake.fail[("POST", "/rest/images")] = [httpx.ConnectTimeout("timed out")]
    with pytest.raises(li.ProviderError) as error:
        _run(li.perform("post", {"account": "member", "text": TEXT,
                                 "media": {"kind": "image", "path": path, "sha256": digest}}))
    assert not error.value.uncertain
    assert len(li._fake.calls("POST", "/rest/images")) == 1


def test_the_request_that_publishes_is_never_sent_twice(li):
    """Retrying stops before the post itself: that request is sent once."""
    _connect(li)
    li._fake.fail[("POST", "/rest/posts")] = [httpx.ConnectError("getaddrinfo failed")]
    with pytest.raises(li.ProviderError):
        _run(li.perform("post", {"account": "member", "text": TEXT}))
    assert len(li._fake.calls("POST", "/rest/posts")) == 1


def test_media_still_processing_is_waited_for_and_posted_once(li):
    """A 400 is LinkedIn refusing: nothing was created, so trying again is safe."""
    _connect(li)
    path, digest = _file(li, "card.png", b"\x89PNG" + b"3" * 50)
    li._fake.fail[("POST", "/rest/posts")] = [httpx.Response(
        400, json={"code": "MEDIA_ASSET_PROCESSING_FAILED", "message": "Media asset is waiting upload"})]
    li._fake.fail[("POST", "/rest/posts")][0] = httpx.Response(
        400, json={"code": "MEDIA_ASSET_WAITING_UPLOAD", "message": "Media asset is waiting upload"})
    receipt = _run(li.perform("post", {"account": "member", "text": TEXT,
                                       "media": {"kind": "image", "path": path, "sha256": digest}}))
    assert len(li._fake.calls("POST", "/rest/posts")) == 2
    assert receipt["post_urn"]


@pytest.mark.parametrize("status", [401, 429, 999])
def test_a_refusal_that_means_stop_halts_linkedin(li, status):
    _connect(li)
    li._fake.fail[("POST", "/rest/posts")] = [httpx.Response(status, json={"message": "no"})]
    with pytest.raises(li.ProviderError):
        _run(li.perform("post", {"account": "member", "text": TEXT}))
    import linkedin_guard
    assert linkedin_guard.halted() is not None


def test_a_permission_refusal_fails_without_halting(li):
    """403 ACCESS_DENIED is a missing scope, not LinkedIn objecting to automation."""
    _connect(li)
    li._fake.fail[("POST", "/rest/posts")] = [httpx.Response(403, json={"code": "ACCESS_DENIED"})]
    with pytest.raises(li.ProviderError) as error:
        _run(li.perform("post", {"account": "member", "text": TEXT}))
    assert not error.value.uncertain
    import linkedin_guard
    assert linkedin_guard.halted() is None


def test_a_comment_goes_to_the_posts_thread_as_the_member(li):
    _connect(li)
    receipt = _run(li.perform("comment", {
        "account": "member", "text": "Two measurable things today.",
        "post_url": "https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000002/"}))
    [request] = [r for r in li._fake.requests if r.url.path.startswith("/rest/socialActions/")]
    assert unquote(request.url.raw_path.decode()).startswith(
        "/rest/socialActions/urn:li:activity:7000000000000000002/comments")
    body = json.loads(request.content)
    assert body == {"actor": PERSON, "object": "urn:li:activity:7000000000000000002",
                    "message": {"text": "Two measurable things today."}}
    assert receipt["comment_urn"]


# --- the owner's own sign-in --------------------------------------------------------

def test_connecting_goes_to_linkedin_with_a_state_and_the_member_scopes(li):
    url = li.authorize_url("member")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "www.linkedin.com" and parsed.path == "/oauth/v2/authorization"
    assert query["client_id"] == ["client-member"]
    assert query["response_type"] == ["code"]
    assert set(query["scope"][0].split()) == {"openid", "profile", "w_member_social"}
    assert query["redirect_uri"] == [li.redirect_uri()]
    assert len(query["state"][0]) >= 32
    assert "member-secret" not in url, "the client secret never goes in a URL"


def test_the_callback_takes_only_a_state_it_issued_and_stores_the_token_privately(li):
    with pytest.raises(ValueError, match="state"):
        _run(li.complete("forged-state", "code-1"))
    state = parse_qs(urlparse(li.authorize_url("member")).query)["state"][0]
    account = _run(li.complete(state, "code-1"))
    assert account == "member"
    [exchange] = li._fake.calls("POST", "/oauth/v2/accessToken")
    form = parse_qs(exchange.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["code-1"]
    assert form["client_secret"] == ["member-secret-value-123"]
    token = li.token_for("member")
    assert token["person"] == PERSON and token["access_token"].startswith("tok-")
    import data_paths
    assert li.token_path().parent == data_paths.data_dir()
    with pytest.raises(ValueError):
        _run(li.complete(state, "code-2")), "a state is spent once"


def test_status_never_shows_a_token(li):
    _connect(li)
    status = li.status()
    assert status["member"]["connected"] is True and status["member"]["name"] == "Tony Stark"
    assert "tok-" not in json.dumps(status)


def test_an_expired_token_is_not_connected(li):
    li.save_token("member", {"access_token": "tok-old", "expires_at": time.time() - 1,
                             "person": PERSON})
    assert li.token_for("member") is None
    with pytest.raises(ValueError, match="[Cc]onnect"):
        li.validate("post", {"account": "member", "text": TEXT})


# --- inside the desk's gate ------------------------------------------------------

def _desk():
    import business_api
    importlib.reload(business_api)
    business_api.start()
    return business_api


def test_a_post_is_one_card_and_approval_posts_it_once(li):
    _connect(li)
    api = _desk()
    card = api.propose(api.Proposal(provider="linkedin", operation="post",
                                    payload={"account": "member", "text": TEXT}))
    assert card["state"] == "pending"
    assert card["payload"]["request"]["text"] == TEXT
    assert li._fake.requests == [], "staging a card sends nothing"
    done = _run(api.decide(card["id"], card["digest"], True))
    assert done["state"] == "submitted"
    assert done["result"]["receipt"]["post_url"].startswith("https://www.linkedin.com/feed/update/")
    assert "Posted" in done["result"]["message"]
    with pytest.raises(ValueError):
        _run(api.decide(card["id"], card["digest"], True))
    assert len(li._fake.calls("POST", "/rest/posts")) == 1


def test_the_interim_limit_is_kept_at_the_desk_too(li):
    _connect(li)
    api = _desk()
    first = api.propose(api.Proposal(provider="linkedin", operation="post",
                                     payload={"account": "member", "text": "one"}))
    second = api.propose(api.Proposal(provider="linkedin", operation="post",
                                      payload={"account": "member", "text": "two"}))
    _run(api.decide(first["id"], first["digest"], True))
    with pytest.raises(ValueError, match="LinkedIn limit"):
        _run(api.decide(second["id"], second["digest"], True))
    import business_store
    assert business_store.get_action(second["id"])["state"] == "pending", "not spent"
    with pytest.raises(ValueError, match="LinkedIn limit"):
        api.propose(api.Proposal(provider="linkedin", operation="post",
                                 payload={"account": "member", "text": "three"}))
    assert len(li._fake.calls("POST", "/rest/posts")) == 1


def test_a_halt_refuses_at_the_desk(li):
    _connect(li)
    api = _desk()
    card = api.propose(api.Proposal(provider="linkedin", operation="post",
                                    payload={"account": "member", "text": TEXT}))
    import linkedin_guard
    linkedin_guard.halt("captcha", source="test")
    with pytest.raises(ValueError, match="stopped"):
        _run(api.decide(card["id"], card["digest"], True))
    with pytest.raises(ValueError, match="stopped"):
        api.propose(api.Proposal(provider="linkedin", operation="post",
                                 payload={"account": "member", "text": "x"}))
    assert li._fake.calls("POST", "/rest/posts") == []


def test_the_brain_can_propose_a_linkedin_post(li):
    _connect(li)
    import jarvis_mcp
    [tool] = [t for t in jarvis_mcp.TOOL_SPECS if t["name"] == "business_propose"]
    assert "linkedin" in tool["inputSchema"]["properties"]["provider"]["enum"]
    assert "linkedin" in tool["description"].lower()


# --- the routes the owner's browser uses -----------------------------------------

@pytest.fixture
def app(li):
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()
    from fastapi.testclient import TestClient
    with TestClient(server_module.app, base_url="https://localhost:8340") as client:
        yield client


def test_connect_sends_the_browser_to_linkedin(app):
    r = app.get("/api/linkedin/connect?account=member", follow_redirects=False)
    assert r.status_code in (302, 303, 307), r.text
    assert r.headers["location"].startswith("https://www.linkedin.com/oauth/v2/authorization?")


def test_connect_without_an_app_says_what_to_do(app, monkeypatch):
    monkeypatch.delenv("LINKEDIN_CLIENT_ID")
    r = app.get("/api/linkedin/connect?account=member", follow_redirects=False)
    assert r.status_code == 400 and "LINKEDIN_CLIENT_ID" in r.text


def test_the_callback_connects_and_says_so_without_the_token(app, li):
    location = app.get("/api/linkedin/connect?account=member", follow_redirects=False).headers["location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    r = app.get(f"/api/linkedin/callback?code=abc&state={state}")
    assert r.status_code == 200 and "connected" in r.text.lower()
    assert "tok-" not in r.text
    assert li.token_for("member")["person"] == PERSON


def test_a_refused_or_forged_callback_connects_nothing(app, li):
    r = app.get("/api/linkedin/callback?error=user_cancelled_authorize&state=x")
    assert r.status_code == 400 and "<script" not in r.text
    r = app.get("/api/linkedin/callback?code=abc&state=forged")
    assert r.status_code == 400
    assert li.token_for("member") is None


def test_the_desk_status_includes_the_api(app, li):
    _connect(li)
    status = app.get("/api/linkedin/status").json()
    assert status["api"]["member"]["connected"] is True
    assert status["limits"]["posts_per_day"] == 1
    assert "tok-" not in json.dumps(status)


def test_a_company_page_post_goes_out_as_the_page(li, monkeypatch):
    """Built now, against the docs, so it works the day LinkedIn grants access."""
    _connect(li)
    monkeypatch.setenv("LINKEDIN_ORGANIZATION_ID", "123456")
    li.save_token("organization", {"access_token": "tok-" + "o" * 40,
                                   "expires_at": time.time() + 86400 * 50, "name": "company page"})
    _run(li.perform("post", {"account": "organization", "text": TEXT}))
    [request] = li._fake.calls("POST", "/rest/posts")
    assert json.loads(request.content)["author"] == "urn:li:organization:123456"
    assert request.headers["Authorization"] == "Bearer tok-" + "o" * 40, "the page's own token"


def test_a_hand_post_reads_as_the_post_on_the_phone(li, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAHfakeTokenTokenTokenTokenTokenToken00")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "424242001")
    import business_store
    import messaging
    card = business_store.propose("linkedin_hand", "post", {"target": {"delivery": "telegram"},
                                                            "request": {"account": "organization", "text": TEXT}})
    body = messaging.card_text(card, body_max=4000)
    assert TEXT in body and "target" not in body
