#!/usr/bin/env python3
"""Does the installed `claude` still echo JARVIS's messages the way
`Brain._handle` tells a wake from a turn by?

    python scripts/probe_cli_echo.py [--claude PATH] [--verbose]

Runs the CLI in JARVIS's stream-json mode (`--replay-user-messages`,
`crossSessionInbound: "accept"`) against a fake Messages API on 127.0.0.1
with a fake key and a throwaway CLAUDE_CONFIG_DIR: no login is read and no
quota is spent. A message from another session is played the way a host
injects one on stdin, `<cross-session-message from="…">`, which the CLI
queues and echoes exactly as one that arrives from another session.

Checked, one CLI process per scenario:

    echo       a tagged message is echoed (`user`, `isReplay`, its `uuid`)
               before the turn's first output, and its result names it
    fold       a message sent while a tool runs is echoed mid-turn, after
               the tool's result; one result names both
    queued     a message sent while a wake runs is echoed after the wake's
               result; the wake's echo says `origin.kind == "peer"`, and its
               result names no message of ours
    into-wake  a message sent while a wake's tool runs is folded into the
               wake and echoed there; the result names it
    peer-in    a message from another session sent while our tool runs is
               echoed mid-turn with `origin.kind == "peer"`; the result
               names only ours
    once       a tag the CLI has seen is acknowledged, not run again

Exit status 1 when any check fails. Run it after every CLI upgrade: the
brain's attribution rests on each of these. Measured 2026-09-30 against
CLI 2.1.270; see docs/chatgpt-fallback.md, "Telling a wake from a turn".
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_env  # noqa: E402

FAKE_KEY = "sk-ant-api03-probe-not-a-real-key"
# How long the fake's Bash call runs: long enough to send a message into.
TOOL_SECONDS = 3
# How long to let a turn's last events land after its result.
SETTLE_SECONDS = 1.0


# ── the fake API ─────────────────────────────────────────────────────────────

def _sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type") == "text")


class _FakeApi(BaseHTTPRequestHandler):
    """Keyed off the last user message: a tool result is answered in
    words; `USE_TOOL` gets one Bash call; `SLOW` is streamed over ~3s;
    anything else is "ok"."""
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    def do_GET(self):
        self._send(404, b'{"type":"error","error":{"type":"not_found_error","message":"probe"}}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        if not self.path.startswith("/v1/messages"):
            return self._send(200, b"{}")
        if "count_tokens" in self.path:
            return self._send(200, b'{"input_tokens": 10}')
        model = body.get("model") or "claude-probe"
        users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
        last = users[-1] if users else {}
        content = last.get("content")
        answered = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        text = _text_of(last)
        usage = {"input_tokens": 10, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        if not body.get("stream"):
            return self._send(200, json.dumps({
                "id": "msg_probe", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": usage}).encode())
        start = {"type": "message_start", "message": {
            "id": "msg_probe", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}}
        if not answered and "USE_TOOL" in text:
            block = {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:16]}", "name": "Bash",
                     "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(
                {"command": f"sleep {TOOL_SECONDS}", "description": "probe"})}]
            stop = "tool_use"
        else:
            block = {"type": "text", "text": ""}
            words = ["slowly "] * 12 if (not answered and "SLOW" in text) else ["ok"]
            deltas = [{"type": "text_delta", "text": w} for w in words]
            stop = "end_turn"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(_sse([start, {"type": "content_block_start", "index": 0,
                                           "content_block": block}]))
            for d in deltas:
                self.wfile.write(_sse([{"type": "content_block_delta", "index": 0, "delta": d}]))
                self.wfile.flush()
                if len(deltas) > 1:
                    time.sleep(0.25)
            self.wfile.write(_sse([
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                 "usage": {"output_tokens": 1}},
                {"type": "message_stop"}]))
            self.wfile.flush()
        except OSError:
            pass


# ── one CLI process ──────────────────────────────────────────────────────────

class Cli:
    def __init__(self, claude: str, port: int, verbose: bool):
        self.work = Path(tempfile.mkdtemp(prefix="probe-cli-echo-"))
        (self.work / "config").mkdir()
        env = {k: v for k, v in claude_env.child_env().items() if not claude_env.is_scrubbed(k)}
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_API_KEY": FAKE_KEY, "CLAUDE_CONFIG_DIR": str(self.work / "config"),
                    "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        # The flags `Brain.command` runs the brain with that bear on this.
        cmd = claude_env.split_command(claude) + [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--replay-user-messages",
            "--model", "sonnet", "--effort", "low", "--setting-sources", "project",
            "--strict-mcp-config", "--tools", "Bash",
            "--settings", json.dumps({"crossSessionInbound": "accept"}),
            "--dangerously-skip-permissions"]
        self.proc = subprocess.Popen(cmd, cwd=self.work, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.verbose = verbose
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if self.verbose:
                print("   ", _brief(ev))
            with self.cond:
                self.events.append(ev)
                self.cond.notify_all()

    def send(self, text: str, *, tag: str | None = None, peer: bool = False) -> str:
        tag = tag or str(uuid.uuid4())
        if peer:
            text = f'<cross-session-message from="probe-peer">{text}</cross-session-message>'
        frame = {"type": "user", "uuid": tag, "message": {"role": "user", "content": text}}
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        self.proc.stdin.flush()
        return tag

    def wait(self, pred, count: int = 1, timeout: float = 60.0) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: sum(1 for e in self.events if pred(e)) >= count,
                                      timeout)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
        shutil.rmtree(self.work, ignore_errors=True)


def _brief(ev: dict) -> str:
    keep = {k: ev[k] for k in ("subtype", "uuid", "isReplay", "isSynthetic", "origin",
                               "user_message_uuid", "user_message_uuids") if k in ev}
    return f"{ev.get('type')} {json.dumps(keep)[:200]}"


def is_result(e):
    return e.get("type") == "result"


def is_echo(e, tag=None):
    return e.get("type") == "user" and e.get("isReplay") is True and (tag is None or e.get("uuid") == tag)


def is_output(e):
    return e.get("type") in ("assistant", "stream_event") or (
        e.get("type") == "user" and not e.get("isReplay"))


def is_tool_use(e):
    return e.get("type") == "assistant" and any(
        isinstance(b, dict) and b.get("type") == "tool_use"
        for b in (e.get("message") or {}).get("content") or [])


def index(events, pred, start=0):
    for i in range(start, len(events)):
        if pred(events[i]):
            return i
    return -1


def names(e, tag):
    return e.get("user_message_uuid") == tag or tag in (e.get("user_message_uuids") or [])


# ── the checks ───────────────────────────────────────────────────────────────

def check_echo(cli: Cli) -> list[str]:
    tag = cli.send("hello")
    if not cli.wait(is_result):
        return ["no result"]
    ev = cli.events
    e, o, r = index(ev, lambda x: is_echo(x, tag)), index(ev, is_output), index(ev, is_result)
    out = []
    if e < 0:
        out.append("never echoed")
    elif o >= 0 and o < e:
        out.append("output came before the echo")
    if not names(ev[r], tag):
        out.append("the result does not name the message")
    return out


def check_fold(cli: Cli) -> list[str]:
    first = cli.send("USE_TOOL please")
    if not cli.wait(is_tool_use):
        return ["no tool call"]
    second = cli.send("sent while the tool ran")
    if not cli.wait(lambda x: is_echo(x, second)) or not cli.wait(is_result):
        return ["the second message was never echoed, or no result"]
    time.sleep(SETTLE_SECONDS)
    ev = cli.events
    e2, r = index(ev, lambda x: is_echo(x, second)), index(ev, is_result)
    out = []
    if not (index(ev, lambda x: is_echo(x, first)) < e2 < r):
        out.append("not echoed mid-turn")
    if sum(1 for x in ev if is_result(x)) != 1:
        out.append("not one result for both")
    elif not (names(ev[r], first) and names(ev[r], second)):
        out.append("the result does not name both")
    return out


def check_queued(cli: Cli) -> list[str]:
    cli.send("SLOW peer talk", peer=True)
    if not cli.wait(lambda x: x.get("type") == "stream_event"):
        return ["the wake said nothing"]
    mine = cli.send("mine")
    if not cli.wait(is_result, 2):
        return ["fewer than two results"]
    ev = cli.events
    peer = index(ev, lambda x: is_echo(x) and x.get("uuid") != mine)
    first = index(ev, is_result)
    e = index(ev, lambda x: is_echo(x, mine))
    out = []
    if peer < 0 or (ev[peer].get("origin") or {}).get("kind") != "peer":
        out.append("the wake's echo does not say peer")
    if names(ev[first], mine):
        out.append("the wake's result names our message")
    if not (first < e):
        out.append("our echo came before the wake's result")
    if not names(ev[index(ev, is_result, first + 1)], mine):
        out.append("our result does not name our message")
    return out


def check_into_wake(cli: Cli) -> list[str]:
    cli.send("USE_TOOL peer", peer=True)
    if not cli.wait(is_tool_use):
        return ["the wake made no tool call"]
    mine = cli.send("mine")
    if not cli.wait(lambda x: is_echo(x, mine)) or not cli.wait(is_result):
        return ["never echoed, or no result"]
    time.sleep(SETTLE_SECONDS)
    ev = cli.events
    e, r = index(ev, lambda x: is_echo(x, mine)), index(ev, is_result)
    out = []
    if not e < r:
        out.append("not folded into the wake")
    if not names(ev[r], mine):
        out.append("the result does not name our message")
    return out


def check_peer_in(cli: Cli) -> list[str]:
    mine = cli.send("USE_TOOL mine")
    if not cli.wait(is_tool_use):
        return ["no tool call"]
    cli.send("peer interjects", peer=True)
    if not cli.wait(is_result):
        return ["no result"]
    time.sleep(SETTLE_SECONDS)
    ev = cli.events
    e = index(ev, lambda x: is_echo(x, mine))
    peer = index(ev, lambda x: is_echo(x) and x.get("uuid") != mine)
    r = index(ev, is_result)
    out = []
    if not (e < peer < r):
        out.append("the other session's message was not echoed mid-turn")
    elif (ev[peer].get("origin") or {}).get("kind") != "peer":
        out.append("its echo does not say peer")
    if not names(ev[r], mine) or len(ev[r].get("user_message_uuids") or []) != 1:
        out.append("the result does not name ours alone")
    return out


def check_once(cli: Cli) -> list[str]:
    tag = cli.send("hello")
    if not cli.wait(is_result):
        return ["no result"]
    cli.send("hello again", tag=tag)
    acked = cli.wait(lambda x: is_echo(x, tag), 2, timeout=15)
    ran = cli.wait(is_result, 2, timeout=5)
    out = []
    if not acked:
        out.append("a repeated tag was not acknowledged")
    if ran:
        out.append("a repeated tag was run again")
    return out


CHECKS = [("echo", check_echo), ("fold", check_fold), ("queued", check_queued),
          ("into-wake", check_into_wake), ("peer-in", check_peer_in), ("once", check_once)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--claude", default=shutil.which("claude") or "claude")
    ap.add_argument("--verbose", action="store_true", help="print every event")
    args = ap.parse_args(argv)
    version = subprocess.run(claude_env.split_command(args.claude) + ["--version"],
                             capture_output=True, text=True).stdout.strip()
    print(f"claude {version or '(version unknown)'}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    failed = 0
    for name, check in CHECKS:
        cli = Cli(args.claude, port, args.verbose)
        try:
            problems = check(cli)
        finally:
            cli.close()
        failed += bool(problems)
        print(f"{name:10s} {'ok' if not problems else 'FAILED: ' + '; '.join(problems)}")
    server.shutdown()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
