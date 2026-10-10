"""The PreToolUse hook: ask JARVIS before a user's MCP server acts.

Spawned by the Claude CLI, not by JARVIS, once per matching tool call. It
reads the CLI's hook payload on stdin, asks `/internal/pretool` over
loopback, and prints the answer back in the CLI's own hook shape.

Deliberately a pipe and nothing else. Every decision lives in
`pretool_gate.py` and the route, where it is testable without spawning a
process; this file exists because the CLI needs a command to run.

It FAILS CLOSED. If JARVIS cannot be reached, or answers something
unreadable, the call is denied. The whole point of the hook is that a
publish cannot happen without a human yes, and a hook that waves calls
through when the server is down would give exactly the wrong answer at
exactly the wrong moment. That includes the hook itself going wrong: by
Claude Code's hook contract an exit code other than 0 or 2 is a
non-blocking error and the call PROCEEDS, so nothing past argument parsing
may raise out of `main` — every failure is a printed deny and exit 0.

Its call goes straight to JARVIS (`loopback_http`): never through a proxy
from the environment, never on to wherever a redirect points. It carries the
loopback token, and a verdict from anywhere else is not JARVIS's.

Verified against CLI 2.1.270: a deny here stops the call even under
`--dangerously-skip-permissions` — that flag removes the PROMPT, not the
hook — and the user's MCP server never runs the tool.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys

# Longer than the server's own wait for the user (GATE_APPROVAL_WAIT_SEC,
# 120s) and shorter than the CLI's hook timeout (180s, set in
# brain.settings()). The innermost budget gives up first, so a hang is
# always attributed to the thing that actually hung. Held by
# tests/test_pretool_wait.py::test_the_budgets_nest_so_the_innermost_gives_up_first.
TIMEOUT_SEC = 150.0

VERDICTS = ("allow", "deny", "ask")


def _reply(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


def _ask(url: str, token_file: str) -> tuple[str, str]:
    """JARVIS's verdict on the call described on stdin. Raises on anything
    that is not a verdict; `main` turns that into a deny."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        with open(token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as e:
        return "deny", f"I could not check that against my own gate, sir ({e.__class__.__name__})."

    body = json.dumps({
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input"),
        "tool_use_id": payload.get("tool_use_id"),
    }).encode()
    # Imported here rather than at the top: this is the hook's one import
    # outside the standard library, found beside this file on sys.path[0].
    # Should it ever be missing, the ImportError is one more deny — at the
    # top of the file it would be a traceback, exit 1, and the call would run.
    import loopback_http
    # The certificate is JARVIS's own self-signed one on loopback, and the
    # bearer token is what actually authenticates this call.
    raw = loopback_http.post_json(url, body, token, timeout=TIMEOUT_SEC,
                                  context=ssl._create_unverified_context())
    verdict = json.loads(raw)["hookSpecificOutput"]
    decision = verdict["permissionDecision"]
    if decision not in VERDICTS:
        raise ValueError(decision)
    reason = verdict.get("permissionDecisionReason", "")
    # The CLI expects text here. A verdict it failed to parse would not be a
    # deny, so a reason that is anything else is dropped rather than passed on.
    return decision, reason if isinstance(reason, str) else ""


# A post-call report's answer is forwarded only up to this size. The record
# needs a status and a link; a read's whole answer (a feed, an inbox) is
# not worth the loopback trip, and is dropped rather than cut mid-JSON.
REPORT_RESPONSE_MAX = 64_000
REPORT_TIMEOUT_SEC = 10.0


def _report(url: str, token_file: str) -> None:
    """`--report`: hand the CLI's PostToolUse / PostToolUseFailure payload to
    `/internal/posttool`, which writes the outcome on the call's card.

    The opposite of the gate in one respect: a report cannot change what
    the call did, and a hook that blocked here would only stall the turn,
    so every failure is swallowed and nothing is printed. Only the fields
    the record needs leave this process — not the transcript path, not the
    working directory. Straight to JARVIS, as the gate's call is."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return
        with open(token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
        body = {key: payload.get(key) for key in
                ("hook_event_name", "tool_name", "tool_input", "tool_use_id", "error")}
        response = payload.get("tool_response")
        if len(json.dumps(response, default=repr)) <= REPORT_RESPONSE_MAX:
            body["tool_response"] = response
        import loopback_http
        loopback_http.post_json(url, json.dumps(body, default=repr).encode(), token,
                                timeout=REPORT_TIMEOUT_SEC,
                                context=ssl._create_unverified_context())
    except Exception:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--report", action="store_true",
                        help="a post-call report (PostToolUse): forward it, decide nothing")
    # A usage error exits 2, which the CLI treats as blocking.
    args = parser.parse_args()
    if args.report:
        _report(args.url, args.token_file)
        return 0
    try:
        decision, reason = _ask(args.url, args.token_file)
    except Exception as e:
        decision, reason = "deny", (f"My gate did not answer, sir, so I have not let "
                                    f"that run ({e.__class__.__name__}).")
    _reply(decision, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
