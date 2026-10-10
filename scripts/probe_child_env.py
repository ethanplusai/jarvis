#!/usr/bin/env python3
"""Which inherited variables does a real `claude -p` act on, and does
`claude_env` keep each one it acts on from JARVIS's children?

    python scripts/probe_child_env.py [--claude PATH] [--rule FILE] [--json]

Runs the CLI against a fake Messages API on 127.0.0.1 with a fake key and a
throwaway CLAUDE_CONFIG_DIR: no login is read and no quota is spent. The
model calls go to the fake; the CLI's own telemetry does whatever it always
does. Each scenario sets one variable (or one family) on top of a clean
environment — `claude_env.child_env()` with nothing inherited — and is
compared with the same run without it:

    effort       `output_config.effort` of the main-loop request
    user agent   the User-Agent of every model request
    requests     how many model requests the turn took (and whether it ended)
    mcp start    when each stdio MCP server was started, and when the first
                 model call went out relative to the slowest one being ready
    telemetry    OTLP exports that reached the fake

and the CLI's own Bash child reports the variable as it saw it: overwritten
(the CLI set its own), passed on, or absent.

Exit status 1 when the CLI acts on a variable the rule lets through. Run it
after every CLI upgrade. `--rule` points at another copy of claude_env.py —
`git show <rev>:claude_env.py > old.py` — to see what an older rule missed.

Measured 2026-09-26 against CLI 2.1.270 and 2.1.280; see claude_env.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import claude_env  # noqa: E402  (the rule the clean baseline is built with)

FAKE_KEY = "sk-ant-api03-probe-not-a-real-key"


# ── the fake API ─────────────────────────────────────────────────────────────

class _State:
    lock = threading.Lock()
    requests: list[dict] = []
    api_delay = 0.0
    bash_command = "true"


def _sse(events: list[dict]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def _reply(model: str, bash: str | None) -> list[dict]:
    """A streamed answer: one Bash call when `bash` is given, else "ok"."""
    if bash is not None:
        block = {"type": "tool_use", "id": "toolu_probe", "name": "Bash", "input": {}}
        delta = {"type": "input_json_delta",
                 "partial_json": json.dumps({"command": bash, "description": "probe"})}
        stop = "tool_use"
    else:
        block, delta, stop = {"type": "text", "text": ""}, {"type": "text_delta", "text": "ok"}, "end_turn"
    usage = {"input_tokens": 10, "output_tokens": 1,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return [
        {"type": "message_start", "message": {
            "id": "msg_probe", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}},
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]


class _FakeApi(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        # A client that gave up first (the API_TIMEOUT_MS scenario) is the
        # point of the exercise, not an error.
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        with _State.lock:
            _State.requests.append({"t": time.time(), "path": self.path})
        self._send(404, b'{"type":"error","error":{"type":"not_found_error","message":"probe"}}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        results = []
        for message in body.get("messages") or []:
            content = message.get("content")
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    text = part.get("content")
                    if isinstance(text, list):
                        text = "\n".join(x.get("text", "") for x in text if isinstance(x, dict))
                    results.append(str(text))
        tools = [t.get("name") for t in body.get("tools") or [] if isinstance(t, dict)]
        with _State.lock:
            _State.requests.append({
                "t": time.time(), "path": self.path, "ua": self.headers.get("User-Agent"),
                "model": body.get("model"), "stream": bool(body.get("stream")),
                "effort": (body.get("output_config") or {}).get("effort"),
                "tools": tools, "tool_results": results})
        if not self.path.startswith("/v1/messages"):
            return self._send(200, b"{}")          # an OTLP export, or anything else
        if "count_tokens" in self.path:
            return self._send(200, b'{"input_tokens": 10}')
        if _State.api_delay:
            time.sleep(_State.api_delay)
        model = body.get("model") or "claude-probe"
        if body.get("stream"):
            bash = _State.bash_command if "Bash" in tools and not results else None
            return self._send(200, _sse(_reply(model, bash)), "text/event-stream")
        return self._send(200, json.dumps({
            "id": "msg_probe", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}).encode())


_MCP_SERVER = r'''
import json, sys, time
name, delay, log = sys.argv[1], float(sys.argv[2]), sys.argv[3]
def note(**kw):
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"server": name, **kw}) + "\n")
note(started=time.time())
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        time.sleep(delay)
        note(ready=time.time())
        result = {"protocolVersion": msg["params"].get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}}, "serverInfo": {"name": name, "version": "0"}}
    elif msg.get("method") == "tools/list":
        result = {"tools": [{"name": "ping", "description": "ping",
                             "inputSchema": {"type": "object", "properties": {}}}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
'''


# ── one run of the CLI ───────────────────────────────────────────────────────

@dataclass
class Run:
    ended: bool
    effort: str | None
    user_agents: list[str]
    requests: int
    exports: list[str]
    mcp_spread: float           # seconds between the first and last server start
    early_call: bool            # a model call went out before every server was ready
    bash: dict[str, str] = field(default_factory=dict)


@dataclass
class Scenario:
    label: str
    env: dict[str, str]
    base: dict[str, str] = field(default_factory=dict)
    servers: int = 1
    server_delay: float = 0.0
    api_delay: float = 0.0
    timeout: float = 90.0

    def options(self) -> tuple:
        return (tuple(sorted(self.base.items())), self.servers, self.server_delay,
                self.api_delay, self.timeout)


def _clean_env() -> dict[str, str]:
    return {k: v for k, v in claude_env.child_env().items() if not claude_env.is_scrubbed(k)}


def run_cli(claude: str, port: int, s: Scenario, extra: dict[str, str]) -> Run:
    names = sorted(extra)
    _State.bash_command = ("for n in " + " ".join(f"'{n}'" for n in names)
                           + '; do printf "%s=%s\\n" "$n" "$(printenv "$n" || echo "<unset>")"; done'
                           if names else "true")
    _State.api_delay = s.api_delay
    with _State.lock:
        _State.requests.clear()
    work = Path(tempfile.mkdtemp(prefix="probe-child-env-"))
    try:
        (work / "config").mkdir()
        (work / "server.py").write_text(_MCP_SERVER, encoding="utf-8")
        log = work / "mcp.log"
        (work / "mcp.json").write_text(json.dumps({"mcpServers": {
            f"slow{i}": {"type": "stdio", "command": sys.executable,
                         "args": [str(work / "server.py"), f"slow{i}", str(s.server_delay), str(log)]}
            for i in range(s.servers)}}), encoding="utf-8")
        env = _clean_env()
        env.update({"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_API_KEY": FAKE_KEY, "CLAUDE_CONFIG_DIR": str(work / "config")})
        env.update({k: v.replace("{PORT}", str(port)) for k, v in {**s.base, **extra}.items()})
        cmd = claude_env.split_command(claude) + [
            "-p", "--output-format", "stream-json", "--verbose", "--model", "sonnet",
            "--effort", "low", "--setting-sources", "project", "--strict-mcp-config",
            "--mcp-config", str(work / "mcp.json"), "--tools", "Bash",
            "--dangerously-skip-permissions", "Run the probe."]
        try:
            subprocess.run(cmd, cwd=work, env=env, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=s.timeout)
            ended = True
        except subprocess.TimeoutExpired:
            ended = False
        with _State.lock:
            requests = list(_State.requests)
        events = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
                  if l.strip()] if log.exists() else []
    finally:
        shutil.rmtree(work, ignore_errors=True)

    model_calls = [r for r in requests if r["path"].startswith("/v1/messages")
                   and "count_tokens" not in r["path"]]
    main = [r for r in model_calls if r["stream"] and r["tools"]]
    starts = [e["started"] for e in events if "started" in e]
    ready = [e["ready"] for e in events if "ready" in e]
    bash: dict[str, str] = {}
    for r in model_calls:
        for text in r["tool_results"]:
            for line in text.splitlines():
                name, sep, value = line.partition("=")
                if sep and name in extra:
                    bash[name] = value
    return Run(
        ended=ended,
        effort=main[0]["effort"] if main else None,
        user_agents=sorted({r["ua"] for r in model_calls if r.get("ua")}),
        requests=len(model_calls),
        exports=sorted({r["path"] for r in requests if not r["path"].startswith("/v1/messages")}),
        mcp_spread=round(max(starts) - min(starts), 1) if starts else 0.0,
        early_call=bool(main and ready and len(ready) == s.servers
                        and main[0]["t"] < max(ready)),
        bash=bash,
    )


def differences(base: Run, run: Run) -> list[str]:
    out = []
    if run.ended != base.ended:
        out.append("turn never finished" if not run.ended else "turn finished")
    if run.effort != base.effort:
        out.append(f"effort {base.effort} -> {run.effort}")
    if run.user_agents != base.user_agents:
        out.append(f"User-Agent {'; '.join(run.user_agents)}")
    if abs(run.requests - base.requests) > 1 or (run.requests != base.requests and not run.ended):
        out.append(f"model requests {base.requests} -> {run.requests}")
    if abs(run.mcp_spread - base.mcp_spread) > 1.5:
        out.append(f"MCP servers started over {base.mcp_spread}s -> {run.mcp_spread}s")
    if run.early_call != base.early_call:
        out.append("first model call before MCP was ready" if run.early_call
                   else "first model call waited for MCP")
    if run.exports != base.exports:
        out.append(f"exports to {', '.join(run.exports) or 'nothing'}")
    return out


# ── the scenarios ────────────────────────────────────────────────────────────

_OTEL = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:{PORT}",
         "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json", "OTEL_METRICS_EXPORTER": "otlp",
         "OTEL_LOGS_EXPORTER": "otlp", "OTEL_METRIC_EXPORT_INTERVAL": "500",
         "OTEL_LOGS_EXPORT_INTERVAL": "500"}

SCENARIOS = [
    Scenario("CLAUDE_EFFORT=xhigh", {"CLAUDE_EFFORT": "xhigh"}),
    Scenario("CLAUDE_CODE_EFFORT_LEVEL=high", {"CLAUDE_CODE_EFFORT_LEVEL": "high"}),
    Scenario("CLAUDE_PID=4242", {"CLAUDE_PID": "4242"}),
    # Not the parent's real value: that can be exactly what this CLI writes.
    Scenario("AI_AGENT=claude-code_0-0-0_agent", {"AI_AGENT": "claude-code_0-0-0_agent"}),
    Scenario("CLAUDE_AGENT_SDK_VERSION=0.3.280", {"CLAUDE_AGENT_SDK_VERSION": "0.3.280"}),
    Scenario("CLAUDE_CODE_ENTRYPOINT=claude-desktop", {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}),
    Scenario("MCP_CONNECTION_NONBLOCKING=true", {"MCP_CONNECTION_NONBLOCKING": "true"},
             server_delay=6.0),
    Scenario("MCP_SERVER_CONNECTION_BATCH_SIZE=8", {"MCP_SERVER_CONNECTION_BATCH_SIZE": "8"},
             servers=5, server_delay=3.0),
    Scenario("API_TIMEOUT_MS=1500", {"API_TIMEOUT_MS": "1500"}, api_delay=4.0, timeout=45.0),
    Scenario("OTEL_* (telemetry off)", _OTEL),
    Scenario("OTEL_* (telemetry on)", _OTEL, base={"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}),
]


def _load_rule(path: str | None):
    if not path:
        return claude_env
    spec = importlib.util.spec_from_file_location("claude_env_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scrubbed(rule, name: str) -> bool:
    if hasattr(rule, "is_scrubbed"):
        return rule.is_scrubbed(name)
    return name not in rule.child_env({name: "x"})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--claude", default=os.environ.get("JARVIS_CLAUDE_PATH") or shutil.which("claude"),
                    help="the CLI to probe (default: JARVIS_CLAUDE_PATH, else claude on PATH)")
    ap.add_argument("--rule", help="judge against this copy of claude_env.py instead")
    ap.add_argument("--json", action="store_true", help="one JSON object per scenario")
    ap.add_argument("only", nargs="*", help="run only scenarios whose label contains one of these")
    args = ap.parse_args(argv)
    if not args.claude:
        ap.error("no claude CLI found; pass --claude")
    rule = _load_rule(args.rule)
    version = subprocess.run(claude_env.split_command(args.claude) + ["--version"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
    scenarios = [s for s in SCENARIOS if not args.only or any(o in s.label for o in args.only)]

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeApi)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not args.json:
        print(f"probing {version or '?'} at {args.claude}")
        print(f"rule: {args.rule or 'claude_env.py'}\n")
    baselines: dict[tuple, Run] = {}
    leaks = 0
    try:
        for s in scenarios:
            key = s.options()
            if key not in baselines:
                baselines[key] = run_cli(args.claude, port, s, {})
            run = run_cli(args.claude, port, s, s.env)
            found = differences(baselines[key], run)
            scrubbed = all(_scrubbed(rule, name) for name in s.env)
            seen = {n: ("overwritten" if v not in (s.env[n].replace("{PORT}", str(port)), "<unset>")
                        else "passed on" if v != "<unset>" else "absent")
                    for n, v in run.bash.items()}
            leak = bool(found) and not scrubbed
            leaks += leak
            if args.json:
                print(json.dumps({"scenario": s.label, "honoured": found, "bash_child": seen,
                                  "scrubbed": scrubbed, "leak": leak}), flush=True)
            else:
                verdict = "LEAK" if leak else ("scrubbed" if scrubbed else "passes")
                child = "; ".join(f"{n} {w}" for n, w in sorted(seen.items())) or "-"
                print(f"{s.label}\n    acts on it: {'; '.join(found) or 'no'}\n"
                      f"    its Bash child: {child}\n    {verdict}\n", flush=True)
    finally:
        server.shutdown()
    if not args.json:
        print(f"{leaks} variable(s) the CLI acts on reach a JARVIS child." if leaks
              else "Every variable the CLI acts on is kept from JARVIS's children.")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
