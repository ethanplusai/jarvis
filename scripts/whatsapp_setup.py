"""Set up JARVIS's WhatsApp line, from the terminal.

    python scripts/whatsapp_setup.py numbers          # which phone_number_id to use
    python scripts/whatsapp_setup.py template         # create the jarvis_attention template
    python scripts/whatsapp_setup.py template-status  # has Meta approved it yet?
    python scripts/whatsapp_setup.py test [--voice] [text...]   # send yourself a message
    python scripts/whatsapp_setup.py status           # what the server would report

Reads `.env` the way the server does (envfile), so once KAPSO_API_KEY is
there the `numbers` command needs nothing else; it prints the id to put in
WHATSAPP_PHONE_NUMBER_ID. Everything here goes through whatsapp.py's own
client — the same code, the same owner-only rule — so a message this sends
is one the server could have sent.

Why a template. WhatsApp lets a business write freely to a person only for
24 hours after that person last wrote to it; outside the window only an
approved template goes through. JARVIS reaching you a day after you last
texted him is the normal case, so `template` creates a UTILITY template,
`jarvis_attention`, with one named parameter `message`, and `template-status`
polls Meta's verdict (usually minutes for a utility template). Set
WHATSAPP_TEMPLATE=jarvis_attention once it says APPROVED.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import envfile  # noqa: E402  (loads .env into os.environ on import)
import whatsapp  # noqa: E402

TEMPLATE_NAME = "jarvis_attention"
TEMPLATE_LANGUAGE = "en_US"
TEMPLATE_BODY = "JARVIS: {{message}}"
TEMPLATE_EXAMPLE = "The build in chitauri failed, sir."


def _platform_get(path: str, params: dict | None = None) -> dict:
    """One call to Kapso's Platform API (not the Meta proxy)."""
    import httpx
    cfg_key = whatsapp.env("KAPSO_API_KEY")
    if not cfg_key:
        raise SystemExit("KAPSO_API_KEY is not set (put it in .env or the environment)")
    base = (whatsapp.env("KAPSO_API_BASE_URL") or whatsapp.DEFAULT_BASE_URL).rstrip("/")
    with httpx.Client(base_url=base, timeout=20, headers={"X-API-Key": cfg_key}) as client:
        response = client.get("/platform/v1" + path, params=params)
    if response.status_code != 200:
        raise SystemExit(f"Kapso answered HTTP {response.status_code} for {path}: "
                         f"{response.text[:300]}")
    return response.json()


def cmd_numbers(_args) -> int:
    data = _platform_get("/whatsapp/phone_numbers")
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        print("No WhatsApp numbers on this Kapso project yet. Connect one at app.kapso.ai "
              "(the free plan provisions a US number in a minute), then run this again.")
        return 1
    print("Numbers on this project:\n")
    for row in rows:
        if not isinstance(row, dict):
            continue
        display = row.get("display_phone_number") or row.get("phone_number") or "?"
        pid = row.get("phone_number_id") or row.get("id") or "?"
        waba = row.get("whatsapp_business_account_id") or row.get("business_account_id") or ""
        status = row.get("status") or row.get("connection_status") or ""
        print(f"  {display:>18}   phone_number_id={pid}" + (f"   WABA={waba}" if waba else "")
              + (f"   [{status}]" if status else ""))
    print("\nPut the id in .env as WHATSAPP_PHONE_NUMBER_ID=<id>, and your own number as "
          "WHATSAPP_OWNER_NUMBER=+<country><number>.")
    return 0


def _waba_id() -> str:
    """The WhatsApp Business Account id behind the configured number."""
    pid = whatsapp.env("WHATSAPP_PHONE_NUMBER_ID")
    if not pid:
        raise SystemExit("Set WHATSAPP_PHONE_NUMBER_ID first (`numbers` prints it)")
    data = _platform_get("/whatsapp/phone_numbers")
    rows = data.get("data") if isinstance(data, dict) else data
    for row in rows or []:
        if isinstance(row, dict) and str(row.get("phone_number_id") or row.get("id")) == pid:
            waba = row.get("whatsapp_business_account_id") or row.get("business_account_id")
            if waba:
                return str(waba)
    raise SystemExit("Could not find the business account behind WHATSAPP_PHONE_NUMBER_ID; "
                     "check the id with `numbers`.")


async def _meta(method: str, path: str, **kwargs) -> dict:
    try:
        return await whatsapp._request(method, path, **kwargs)
    finally:
        await whatsapp.close()


def cmd_template(_args) -> int:
    waba = _waba_id()
    body = {
        "name": TEMPLATE_NAME, "language": TEMPLATE_LANGUAGE, "category": "UTILITY",
        "parameter_format": "NAMED",
        "components": [{
            "type": "BODY", "text": TEMPLATE_BODY,
            "example": {"body_text_named_params": [
                {"param_name": "message", "example": TEMPLATE_EXAMPLE}]},
        }],
    }
    try:
        reply = asyncio.run(_meta("POST", f"/{waba}/message_templates", json_body=body))
    except whatsapp.ChannelError as e:
        if "already exists" in str(e).lower() or (e.status == 400 and "exists" in str(e)):
            print(f"Template {TEMPLATE_NAME} already exists; run `template-status`.")
            return 0
        raise SystemExit(str(e))
    print(json.dumps(reply, indent=2))
    print(f"\nCreated {TEMPLATE_NAME} ({reply.get('status', 'PENDING')}). Meta reviews utility "
          f"templates in minutes to hours; `template-status` says when. Then set "
          f"WHATSAPP_TEMPLATE={TEMPLATE_NAME} in .env and restart JARVIS.")
    return 0


def cmd_template_status(args) -> int:
    waba = _waba_id()
    name = whatsapp.env("WHATSAPP_TEMPLATE") or TEMPLATE_NAME
    deadline = time.time() + (args.wait or 0)
    while True:
        try:
            reply = asyncio.run(_meta("GET", f"/{waba}/message_templates",
                                      params={"name": name, "limit": 10}))
        except whatsapp.ChannelError as e:
            raise SystemExit(str(e))
        rows = [row for row in (reply.get("data") or []) if isinstance(row, dict)]
        if not rows:
            print(f"No template called {name} on this account; run `template` first.")
            return 1
        for row in rows:
            print(f"  {row.get('name')} [{row.get('language')}]: {row.get('status')}"
                  + (f" — {row.get('rejected_reason')}" if row.get("rejected_reason") not in (None, "NONE") else ""))
        approved = any(row.get("status") == "APPROVED" for row in rows)
        if approved:
            print(f"\nApproved. Set WHATSAPP_TEMPLATE={name} in .env and restart JARVIS.")
            return 0
        if time.time() >= deadline:
            return 1
        time.sleep(30)


def cmd_test(args) -> int:
    cfg = whatsapp.config()
    if cfg is None:
        names = whatsapp.missing()
        raise SystemExit("Not configured: " + (", ".join(names) + " not set" if names
                                                else str(whatsapp.issue())))
    text = " ".join(args.text) or "Testing the line, sir — JARVIS here."

    async def go() -> dict:
        whatsapp.init_db()
        try:
            return await whatsapp.say(text, voice=False)
        finally:
            await whatsapp.close()

    try:
        receipt = asyncio.run(go())
    except whatsapp.WindowClosed as e:
        raise SystemExit(f"{e}\nSend JARVIS's number any message from your phone first "
                         f"(that opens the 24-hour window), or set WHATSAPP_TEMPLATE.")
    except whatsapp.ChannelError as e:
        raise SystemExit(str(e))
    print(f"Sent to {whatsapp.masked(cfg.owner)} via {receipt.get('via')} "
          f"(id {receipt.get('wamid') or '?'}).")
    if args.voice:
        print("A voice note needs the server's Fish voice; use Settings → WhatsApp → "
              "\"Send test message\" with voice ticked, or ask JARVIS to send one.")
    return 0


def cmd_status(_args) -> int:
    print(json.dumps(whatsapp.status(), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("numbers", help="list the numbers on the Kapso project").set_defaults(fn=cmd_numbers)
    sub.add_parser("template", help=f"create the {TEMPLATE_NAME} utility template").set_defaults(fn=cmd_template)
    status = sub.add_parser("template-status", help="Meta's verdict on the template")
    status.add_argument("--wait", type=int, default=0, help="keep polling for this many seconds")
    status.set_defaults(fn=cmd_template_status)
    test = sub.add_parser("test", help="send yourself a message")
    test.add_argument("--voice", action="store_true")
    test.add_argument("text", nargs="*")
    test.set_defaults(fn=cmd_test)
    sub.add_parser("status", help="what the server reports").set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
