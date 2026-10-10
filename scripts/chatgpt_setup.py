"""Set up JARVIS's ChatGPT fallback, from the terminal.

    python scripts/chatgpt_setup.py login [--device-auth]   # sign Codex in for JARVIS
    python scripts/chatgpt_setup.py status                  # can ChatGPT stand in?
    python scripts/chatgpt_setup.py logout                  # sign JARVIS's Codex out

The fallback runs Codex with a home of its own, `<data>/codex-home`
(`chatgpt_fallback.codex_home`), never your `~/.codex`: that one would put
your AGENTS.md, skills and plugins into every prompt, and JARVIS's threads
into Codex Desktop's history. So Codex has to be signed in there once, and
this is how. `login` runs Codex's own `codex login` in this terminal with
that home: the ChatGPT sign-in happens in your browser, and JARVIS never sees
a password or a token. Sign in with ChatGPT, not an API key — an API-key
login bills the key, and the fallback refuses to run on one.

`status` asks Codex what the server would ask it (`chatgpt_fallback.
check_readiness`: local commands only, no model is called), and says what
to do about anything wrong. The fallback itself is switched on with
`JARVIS_CHATGPT_FALLBACK=1` in `.env`; see docs/chatgpt-fallback.md.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import envfile  # noqa: E402,F401  (loads .env into os.environ on import)
import chatgpt_fallback  # noqa: E402


def _codex_or_explain() -> list[str] | None:
    command, version = chatgpt_fallback.find_command(chatgpt_fallback.child_env())
    if not command:
        print("The Codex CLI was not found. Install Codex Desktop, or run "
              "`npm install -g @openai/codex`, then try again. (Or point "
              f"{chatgpt_fallback.CODEX_PATH_ENV} at codex.exe.)")
        return None
    print(f"Using {command[0]} ({version or 'version unknown'}).")
    return command


def _run_here(argv: list[str]) -> int:
    """Run Codex in THIS terminal, with JARVIS's home and nothing of the
    caller's OpenAI or Codex variables — the same environment a fallback
    turn gets."""
    return subprocess.call(argv, env=chatgpt_fallback.child_env())


def cmd_login(args) -> int:
    command = _codex_or_explain()
    if command is None:
        return 1
    print(f"Signing Codex in for JARVIS, in {chatgpt_fallback.codex_home()}.")
    print("Choose \"Sign in with ChatGPT\". JARVIS never sees what you type.")
    extra = ["--device-auth"] if args.device_auth else []
    code = _run_here([*command, "login", *extra])
    if code != 0:
        print(f"codex login ended with exit code {code}.")
        return code
    return cmd_status(args)


def cmd_logout(args) -> int:
    command = _codex_or_explain()
    if command is None:
        return 1
    return _run_here([*command, "logout"])


def cmd_status(args) -> int:
    ready = chatgpt_fallback.check_readiness()
    if ready.ok:
        print(f"Ready: ChatGPT will stand in while Claude's limit holds "
              f"({ready.version or 'Codex'}, model {chatgpt_fallback.model()}).")
        return 0
    print(f"Not ready: {ready.reason}.")
    if ready.remedy:
        print(f"To fix it: {ready.remedy}.")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    login = sub.add_parser("login", help="sign Codex in for JARVIS")
    login.add_argument("--device-auth", action="store_true",
                       help="sign in with a code on another device instead of this browser")
    login.set_defaults(fn=cmd_login)
    sub.add_parser("status", help="can ChatGPT stand in?").set_defaults(fn=cmd_status)
    sub.add_parser("logout", help="sign JARVIS's Codex out").set_defaults(fn=cmd_logout)
    args = parser.parse_args(argv)
    # The readiness check says "switched off" first when it is; `login`
    # should still work before the switch is flipped.
    if args.cmd == "login" and not chatgpt_fallback.enabled():
        print(f"Note: the fallback is off until {chatgpt_fallback.ENABLE_ENV}=1 is in .env.")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
