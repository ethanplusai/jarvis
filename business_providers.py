"""Bounded provider REST adapters. Credentials never enter proposals or receipts.

Native provider payloads are retained for review, rather than silently translating
currencies, targeting, creative, or budget semantics. All mutations need approval.
"""
import asyncio
import json
import os
import re
from xml.sax.saxutils import escape

import httpx

REQUIRED = {
    "google": ("GOOGLE_ADS_CUSTOMER_ID", "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN"),
    "meta": ("META_AD_ACCOUNT_ID", "META_ACCESS_TOKEN", "META_API_VERSION"),
    "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "JARVIS_OWNER_PHONE"),
    "chatgpt": ("OPENAI_ADS_API_KEY",),
    # LinkedIn's official API (`linkedin_api`): the owner's app, plus his own
    # sign-in, which is a token in the private data folder, not a variable.
    "linkedin": ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"),
    # A LinkedIn post sent to the owner to publish by hand (`linkedin_handpost`).
    "linkedin_hand": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"),
}
GOOGLE_RESOURCES = {"campaignBudgetOperation", "campaignOperation", "adGroupOperation",
                    "adGroupAdOperation", "adGroupCriterionOperation", "campaignCriterionOperation"}
META_RESOURCES = {"campaigns", "adsets", "adcreatives", "ads"}
OPENAI_RESOURCES = {"campaigns", "ad_groups", "ads", "upload"}


class ProviderError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def env(name):
    return os.environ.get(name, "").strip()


def identity(provider):
    if provider == "google":
        customer = env("GOOGLE_ADS_CUSTOMER_ID").replace("-", "")
        if not re.fullmatch(r"[0-9]{10}", customer):
            raise ValueError("Configure a ten-digit GOOGLE_ADS_CUSTOMER_ID")
        version = env("GOOGLE_ADS_API_VERSION") or "v25"
        if not re.fullmatch(r"v[0-9]{1,3}", version):
            raise ValueError("Invalid Google API version")
        return {"account": customer, "version": version}
    if provider == "meta":
        account = env("META_AD_ACCOUNT_ID").removeprefix("act_")
        version = env("META_API_VERSION")
        if not re.fullmatch(r"[0-9]+", account) or not re.fullmatch(r"v[0-9]{1,3}\.0", version):
            raise ValueError("Configure META_AD_ACCOUNT_ID and a supported META_API_VERSION (vNN.0)")
        return {"account": "act_" + account, "version": version}
    if provider == "twilio":
        account = env("TWILIO_ACCOUNT_SID")
        if not re.fullmatch(r"AC[0-9a-fA-F]{32}", account):
            raise ValueError("Configure TWILIO_ACCOUNT_SID")
        numbers = {"from": env("TWILIO_FROM_NUMBER"), "to": env("JARVIS_OWNER_PHONE")}
        if not all(re.fullmatch(r"\+[1-9][0-9]{7,14}", number) for number in numbers.values()):
            raise ValueError("Configure caller and owner phone numbers in E.164 format")
        return {"account": account, **numbers}
    if provider == "linkedin":
        import linkedin_api
        return linkedin_api.identity()
    if provider == "linkedin_hand":
        import linkedin_handpost
        return linkedin_handpost.identity()
    if provider == "chatgpt":
        # A non-secret key fingerprint binds approval to this account credential.
        import hashlib
        return {"credential_fingerprint": hashlib.sha256(env("OPENAI_ADS_API_KEY").encode()).hexdigest()[:16]}
    raise ValueError("Unsupported provider")


def capabilities():
    result = {}
    for provider, keys in REQUIRED.items():
        missing = [key for key in keys if not env(key)]
        issue = None
        try:
            identity(provider)
        except ValueError as error:
            # The LinkedIn routes explain themselves at length where it is
            # needed (a refused proposal); here, beside every other provider
            # in business_status, a word says it.
            issue = {"linkedin": "not connected", "linkedin_hand": "needs Telegram"}.get(provider, str(error))
        result[provider] = {"configured": not missing and not issue, "verified": False,
                            "missing": missing, "issue": issue, "approval_required": True}
    return result


def _safe_payload(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if any(word in key.lower() for word in ("token", "secret", "authorization", "password", "api_key")):
                raise ValueError("Credentials must be configured locally, never included in a proposal")
            _safe_payload(child)
    elif isinstance(value, list):
        for child in value:
            _safe_payload(child)


def validate(provider, operation, body):
    if provider not in REQUIRED or not isinstance(body, dict):
        raise ValueError("Unsupported provider or invalid payload")
    if len(json.dumps(body, allow_nan=False)) > 64000:
        raise ValueError("Proposal exceeds 64 KB")
    _safe_payload(body)
    if provider == "twilio":
        if operation == "call":
            if set(body) - {"message", "time_limit"}:
                raise ValueError("Calls can only go to the configured owner")
            message = body.get("message")
            if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
                raise ValueError("Call message must contain 1–2000 characters")
            limit = body.get("time_limit", 120)
            if type(limit) is not int or not 10 <= limit <= 300:
                raise ValueError("Call duration must be 10–300 seconds")
            if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in message):
                raise ValueError("Call message contains unsupported control characters")
        elif operation == "cancel":
            if set(body) != {"call_sid"} or not re.fullmatch(r"CA[0-9a-fA-F]{32}", str(body.get("call_sid", ""))):
                raise ValueError("A valid Twilio call SID is required")
        else:
            raise ValueError("Unsupported call operation")
    elif provider == "google":
        if operation != "mutate" or set(body) != {"mutateOperations"}:
            raise ValueError("Google accepts mutate with mutateOperations only")
        operations = body["mutateOperations"]
        if not isinstance(operations, list) or not 1 <= len(operations) <= 50:
            raise ValueError("Supply 1–50 atomic Google operations")
        for item in operations:
            if not isinstance(item, dict) or len(item) != 1 or not set(item) <= GOOGLE_RESOURCES:
                raise ValueError("Unsupported Google resource")
            change = next(iter(item.values()))
            if not isinstance(change, dict) or not (set(change) & {"create", "update", "remove"}):
                raise ValueError("Each resource requires create, update, or remove")
    elif provider == "linkedin":
        import linkedin_api
        linkedin_api.validate(operation, body)
    elif provider == "linkedin_hand":
        import linkedin_handpost
        linkedin_handpost.validate(operation, body)
    elif provider == "meta":
        resource, _, object_id = operation.partition("/")
        if resource not in META_RESOURCES or operation.endswith("/") or (object_id and not re.fullmatch(r"[0-9]+", object_id)):
            raise ValueError("Use campaigns, adsets, adcreatives, ads, or resource/id to update")
        if not body or set(body) & {"method", "batch", "relative_url", "access_token"}:
            raise ValueError("Invalid Meta mutation")
    else:
        resource, _, object_id = operation.partition("/")
        if resource not in OPENAI_RESOURCES or operation.endswith("/") or (object_id and
                (resource == "upload" or not re.fullmatch(r"[A-Za-z0-9_]+", object_id))):
            raise ValueError("Unsupported ChatGPT Ads resource")
        if not body:
            raise ValueError("Supply the provider request fields")
    return body


async def _request(client, method, url, **kwargs):
    """Do not follow redirects, expose server error bodies, or buffer unbounded responses."""
    try:
        async with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise ProviderError(f"Provider returned HTTP {response.status_code}. Check the provider console.",
                                    uncertain=response.status_code >= 500 or response.status_code == 408)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ProviderError("Provider response too large; check provider records.", uncertain=True)
            try:
                return json.loads(data)
            except (ValueError, UnicodeError):
                raise ProviderError("Provider returned an unreadable receipt; check provider records.", uncertain=True) from None
    except httpx.HTTPError:
        raise ProviderError("Provider connection failed; check provider records before retrying.", uncertain=True) from None


async def _google_headers(client):
    token = await _request(client, "POST", "https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token", "client_id": env("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": env("GOOGLE_ADS_CLIENT_SECRET"), "refresh_token": env("GOOGLE_ADS_REFRESH_TOKEN")})
    if not isinstance(token, dict) or not token.get("access_token"):
        raise ProviderError("Google authentication did not return an access token")
    headers = {"Authorization": "Bearer " + token["access_token"], "developer-token": env("GOOGLE_ADS_DEVELOPER_TOKEN")}
    manager = env("GOOGLE_ADS_LOGIN_CUSTOMER_ID").replace("-", "")
    if manager:
        if not re.fullmatch(r"[0-9]{10}", manager):
            raise ValueError("Invalid Google manager account")
        headers["login-customer-id"] = manager
    return headers


async def perform(provider, operation, payload, *, read=False):
    if not capabilities()[provider]["configured"]:
        raise ValueError("Provider is not configured; see Business connections")
    if provider == "linkedin":
        # Its own bounded client and budget: a video goes up in parts.
        import linkedin_api
        if read:
            return linkedin_api.status()
        return await linkedin_api.perform(operation, payload)
    if provider == "linkedin_hand":
        import linkedin_handpost
        if read:
            return {"delivery": "telegram"}
        return await linkedin_handpost.perform(operation, payload)
    current = identity(provider)
    async with asyncio.timeout(18), httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        if provider == "google":
            base = f"https://googleads.googleapis.com/{current['version']}/customers/{current['account']}"
            headers = await _google_headers(client)
            if read:
                query = ("SELECT campaign.id, campaign.name, campaign.status, campaign_budget.amount_micros, "
                         "customer.currency_code, metrics.impressions, metrics.clicks, metrics.cost_micros "
                         "FROM campaign WHERE segments.date DURING LAST_30_DAYS AND campaign.status != 'REMOVED' LIMIT 500")
                return await _request(client, "POST", base + "/googleAds:search", headers=headers, json={"query": query})
            return await _request(client, "POST", base + "/googleAds:mutate", headers=headers,
                                  json={**payload, "partialFailure": False})
        if provider == "meta":
            base = f"https://graph.facebook.com/{current['version']}"
            headers = {"Authorization": "Bearer " + env("META_ACCESS_TOKEN")}
            if read:
                return await _request(client, "GET", f"{base}/{current['account']}/campaigns", headers=headers,
                                      params={"fields": "id,name,status,daily_budget,lifetime_budget,insights.date_preset(last_30d){impressions,clicks,spend}", "limit": 100})
            resource, _, object_id = operation.partition("/")
            if object_id:
                target = await _request(client, "GET", f"{base}/{object_id}", headers=headers, params={"fields": "account_id"})
                if str(target.get("account_id")) != current["account"].removeprefix("act_"):
                    raise ValueError("Meta object does not belong to the configured ad account")
            path = object_id or f"{current['account']}/{resource}"
            return await _request(client, "POST", f"{base}/{path}", headers=headers,
                                  data={key: json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
                                        for key, value in payload.items()})
        if provider == "twilio":
            base = f"https://api.twilio.com/2010-04-01/Accounts/{current['account']}/Calls"
            auth = (current["account"], env("TWILIO_AUTH_TOKEN"))
            if read:
                return await _request(client, "GET", base + ".json", auth=auth, params={"To": current["to"], "PageSize": 50})
            if operation == "cancel":
                call = await _request(client, "GET", f"{base}/{payload['call_sid']}.json", auth=auth)
                if call.get("to") != current["to"]:
                    raise ValueError("Call is not addressed to the configured owner")
                return await _request(client, "POST", f"{base}/{payload['call_sid']}.json", auth=auth, data={"Status": "completed"})
            return await _request(client, "POST", base + ".json", auth=auth, data={
                "To": current["to"], "From": current["from"], "Timeout": "30",
                "TimeLimit": str(payload.get("time_limit", 120)),
                "Twiml": "<Response><Say>" + escape(payload["message"]) + "</Say><Hangup/></Response>"})
        headers = {"Authorization": "Bearer " + env("OPENAI_ADS_API_KEY")}
        base = "https://api.ads.openai.com/v1/"
        if read:
            return await _request(client, "GET", base + "campaigns", headers=headers, params={"limit": 100})
        return await _request(client, "POST", base + operation, headers=headers, json=payload)
