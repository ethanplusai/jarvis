"""Set up JARVIS's Telegram line, from the terminal.

    python scripts/telegram_setup.py pair            # pair the bot with your phone
    python scripts/telegram_setup.py test [--voice] [text...]   # send yourself a message
    python scripts/telegram_setup.py status          # what the server would report

Reads `.env` the way the server does (envfile), so `TELEGRAM_BOT_TOKEN` from
@BotFather is all `pair` needs. Pairing is how `TELEGRAM_OWNER_ID` gets
filled in: the script prints a six-digit code, you send it to the bot from
your phone, and the sender becomes the owner — written to `.env` and used
from then on.

Two pollers on one bot token fight (Telegram answers 409 to the second), so
while JARVIS is running this script pairs THROUGH the server — it asks
`POST /api/telegram/pair` for a code with the tool token, then watches
`GET /api/telegram/status` until THAT code's outcome is in (its serial:
an existing owner is no answer, pairing again replaces him). With the
server down it polls the Bot API itself, and waits on the same outcome.
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
import telegram  # noqa: E402

PAIR_WAIT_SEC = 600

_ENDED = {
    "cancelled": "The code was withdrawn from Settings; run this again.",
    "lapsed": "The code lapsed; run this again.",
    "locked": ("Too many wrong codes were sent to the bot, so the code was withdrawn. "
               "Run this again — and if you did not send them, somebody else knows the "
               "bot's name."),
}


def _outcome(pairing: dict, serial: int) -> str:
    """How code number `serial` ended, from a pairing status: "" while it
    is live, and "lapsed" if a newer code has replaced it."""
    if int(pairing.get("serial") or 0) != serial:
        return "lapsed"
    return str(pairing.get("outcome") or "")


def _say_note(pairing: dict, outcome: str, said: str) -> str:
    """While the code is still live, print a new note once (the clock looks
    wrong, say). Returns the note now said."""
    note = str(pairing.get("note") or "")
    if not outcome and note and note != said:
        print(f"Note: {note}.")
        return note
    return said


def _server_url() -> str:
    import web_auth
    bind = web_auth.detect_bind()
    host = "127.0.0.1" if bind.host in ("0.0.0.0", "") else bind.host
    return f"{bind.scheme}://{host}:{bind.port}"


def _server_client():
    """An httpx client for the running server, or None when it is down.

    It carries the loopback tool token, so it goes straight to the server:
    `trust_env=False` stops httpx reading HTTP(S)_PROXY (or, on Windows,
    Internet Options), which it otherwise does for 127.0.0.1 too — and with
    `verify=False` a proxy could open the tunnel. Redirects are not followed
    (httpx's default, spelled out: one would re-send the token)."""
    import httpx
    import data_paths
    try:
        token = data_paths.ensure_tool_token()
    except Exception:
        return None
    client = httpx.Client(base_url=_server_url(), timeout=10, verify=False,
                          trust_env=False, follow_redirects=False,
                          headers={"Authorization": f"Bearer {token}"})
    try:
        if client.get("/api/health").status_code == 200:
            return client
    except Exception:
        pass
    client.close()
    return None


def cmd_pair(_args) -> int:
    if not telegram.token():
        raise SystemExit(telegram.issue() or "Set TELEGRAM_BOT_TOKEN in .env first "
                         "(from @BotFather: /newbot, then copy the token).")
    client = _server_client()
    if client is not None:
        with client:
            pairing = client.post("/api/telegram/pair")
            if pairing.status_code != 200:
                raise SystemExit(f"The server refused to start pairing: {pairing.text[:200]}")
            code = pairing.json()["code"]
            serial = int(pairing.json()["serial"])
            bot = pairing.json().get("bot_username") or ""
            print(f"Open the bot in Telegram on your phone{(' (@' + bot + ')') if bot else ''} "
                  f"and send it this code:\n\n    {code}\n\nWaiting up to ten minutes …")
            deadline = time.time() + PAIR_WAIT_SEC
            said = ""
            while time.time() < deadline:
                state = client.get("/api/telegram/status").json()
                pairing_state = state.get("pairing") or {}
                outcome = _outcome(pairing_state, serial)
                said = _say_note(pairing_state, outcome, said)
                if outcome == "paired":
                    note = pairing_state.get("note") or ""
                    print(f"Paired: TELEGRAM_OWNER_ID={state['owner_id']}. He will greet you "
                          f"on the phone." + (f" But: {note}." if note else ""))
                    return 0
                if outcome:
                    raise SystemExit(_ENDED.get(outcome, "The code ended; run this again."))
                time.sleep(2)
            raise SystemExit("Nobody sent the code in time; run this again.")

    # The server is down: poll the Bot API here, once every few seconds.
    pairing = telegram.new_pairing_code()
    serial = int(pairing["serial"])
    print(f"JARVIS is not running, so this script will read the bot itself.\n"
          f"Open the bot in Telegram on your phone and send it this code:\n\n"
          f"    {pairing['code']}\n\nWaiting up to ten minutes …")

    async def wait() -> int:
        telegram.init_db()
        try:
            await telegram._clear_webhook(telegram.token())
            deadline = time.time() + PAIR_WAIT_SEC
            said = ""
            while time.time() < deadline:
                try:
                    # Pairing only: with no server there is no brain, so the
                    # owner's other messages are told so, not taken as turns.
                    await telegram.poll_once(timeout=10, pairing_only=True)
                except telegram.Conflict:
                    raise SystemExit("Something else is polling this bot (JARVIS, or another "
                                     "script); stop it or pair from Settings → Telegram.")
                state = telegram.pairing_status()
                outcome = _outcome(state, serial)
                said = _say_note(state, outcome, said)
                if outcome == "paired":
                    owner = telegram.config().owner_id
                    if state.get("note"):
                        # This process is the only one that knew; it is ending.
                        print(f"Paired, but .env could not be written, and this script's "
                              f"pairing ends with it. Put TELEGRAM_OWNER_ID={owner} in .env "
                              f"by hand, then start JARVIS.")
                        return 1
                    print(f"Paired: TELEGRAM_OWNER_ID={owner} (written to .env). "
                          f"Start JARVIS and he will use it.")
                    return 0
                if outcome:
                    raise SystemExit(_ENDED.get(outcome, "The code ended; run this again."))
            raise SystemExit("Nobody sent the code in time; run this again.")
        finally:
            await telegram.close()

    return asyncio.run(wait())


def cmd_test(args) -> int:
    if not telegram.configured():
        names = telegram.missing()
        raise SystemExit("Not paired yet: run `pair` first." if names == ["TELEGRAM_OWNER_ID"]
                         else "Not configured: " + (", ".join(names) + " not set" if names
                                                     else str(telegram.issue())))
    text = " ".join(args.text) or "Testing the line, sir — JARVIS here."
    client = _server_client()
    if client is not None:
        with client:
            r = client.post("/api/telegram/test", json={"text": text, "voice": bool(args.voice)})
            if r.status_code != 200:
                raise SystemExit(f"The server could not send it: {r.text[:200]}")
            print("Sent through the running JARVIS" + (" with a voice note" if r.json().get("voice_note") else "") + ".")
            return 0

    async def go() -> dict:
        telegram.init_db()
        try:
            return await telegram.say(text, voice=False)
        finally:
            await telegram.close()

    try:
        receipt = asyncio.run(go())
    except telegram.ChannelError as e:
        raise SystemExit(e.report)
    print(f"Sent (message {receipt.get('message_id') or '?'}).")
    if args.voice:
        print("A voice note needs the server's Fish voice; start JARVIS and use "
              "Settings → Telegram → \"Send test message\" with voice ticked.")
    return 0


def cmd_status(_args) -> int:
    print(json.dumps(telegram.status(), indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pair", help="pair the bot with your phone").set_defaults(fn=cmd_pair)
    test = sub.add_parser("test", help="send yourself a message")
    test.add_argument("--voice", action="store_true")
    test.add_argument("text", nargs="*")
    test.set_defaults(fn=cmd_test)
    sub.add_parser("status", help="what the server reports").set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
