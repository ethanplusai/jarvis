#!/usr/bin/env python3
"""A stand-in `codex` for tests: the local commands readiness asks, and
`exec` / `exec resume` speaking the JSONL the real CLI speaks.

The shapes are the ones measured from codex-cli 0.155.0-alpha.9.2 against a
fake Responses server on 2026-09-27: `thread.started`, `turn.started`,
`item.started` / `item.completed` with an `mcp_tool_call` (`server`, `tool`)
or an `agent_message` (`text`), `turn.completed`, and for a failure an
`error` event and `turn.failed`. Nothing here reaches OpenAI, and nothing
reads a login: the "login state" is an environment variable.

Run as `python fake_codex.py <codex arguments>`. Behaviour, from the
environment (FAKECODEX_*, which the fallback's own scrub of OPENAI_* and
CODEX_* leaves alone) and from markers in the prompt:

  FAKECODEX_RECORD        append one JSON line per `exec` run: its argv,
                          prompt, cwd and a summary of its environment
  FAKECODEX_LOGIN         chatgpt (default) | apikey | none
  FAKECODEX_EXTRA_FEATURE a feature `features list` reports switched on
  FAKECODEX_UNKNOWN       a feature name `--disable` refuses as unknown
  FAKECODEX_RESUME_FAIL=1 `exec resume` fails before the thread starts
  FAKECODEX_CATALOG       what `debug models` prints ("fail": exit 1)

  [tool:<server>:<tool>]  an MCP call is reported before the answer
  [reader:<tool>]         one of Codex's own resource readers, as measured:
                          no `server` argument, reported as server "codex",
                          completed; [reader:<tool>:<name>] passes a server
                          Codex does not know, and fails "unknown MCP
                          server"; [reader-answered:<tool>:<name>] is
                          answered by that server
  [hold:<type>]           report an item of that type only once the file
                          FAKECODEX_GO names exists, then wait 30 s
  [reader-lists:<server>] an all-server listing (no server argument) whose
                          answer names a resource of that server
  [plant]                 write the file FAKECODEX_PLANT names during the run
                          (a machine-wide configuration appearing); [toggle]
                          writes it and removes it again
  [reader-as:<tool>:<item server>:<server argument>]
                          a reader-named item whose server and argument
                          disagree, completed
  [reader-lists-garbled]  an all-server listing whose answer is not JSON
  [reader-lists-extra]    one whose JSON carries more than a listing's keys

Every invocation that is not `exec` is recorded too ({"invocation": ...,
"cwd": ...}), so a test can see that nothing ran, and where.
  [usage-limit]           ChatGPT's own usage limit, with a reset time
  [usage-limit-notime]    ...without one
  [fail]                  the turn fails
  [silent]                the turn completes with nothing said
  [reconnect]             a transient "Reconnecting" error, then an answer
  [sleep:<seconds>]       wait before answering
  [grandchild]            start a child that outlives nothing: its pid is
                          recorded, for the test that the tree is killed
  [pid]                   record this process's own pid
  [item:<type>]           report an item of that type (a tool of Codex's
                          own, say `command_execution`), then wait 30 s;
                          [item-updated:<type>] / [item-completed:<type>]
                          report it as that event instead
  [config-refused]        refuse the command line as a strict config would,
                          before any thread starts
  [bigline]               one oversized line before the answer
  [catalog:code-mode]     write a models cache marking gpt-5.5 code-mode,
                          as a run that refreshed its catalog would
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid

KNOWN_FEATURES = {
    "shell_tool", "unified_exec", "unified_exec_tty", "shell_snapshot", "view_image",
    "apps", "plugins", "remote_plugin", "plugin_sharing", "skill_search",
    "skill_mcp_dependency_install", "tool_suggest", "tool_call_mcp_elicitation",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access", "computer_use",
    "in_app_browser", "in_app_chat", "in_app_dictation", "in_app_local_automation",
    "in_app_updates", "image_generation", "multi_agent", "multi_agent_v2", "goals",
    "hooks", "memories", "sleep_tool", "workspace_dependencies", "worktrees",
    "guardian_approval", "realtime_conversation", "code_mode_host",
}
KEPT = ("auth_elicitation", "compaction_image_budget", "content_item_kinds",
        "enable_request_compression", "fast_mode", "mentions_v2", "secret_auth_storage",
        "unbounded_connection_retries")
LIMIT_MESSAGE = ("You’ve hit your usage limit. Upgrade to Pro "
                 "(https://chatgpt.com/explore/pro), visit "
                 "https://chatgpt.com/codex/settings/usage to purchase more credits or "
                 "try again at Sep 28th, 2099 12:13 AM.")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def disabled(args):
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--disable"]


def refuse_unknown(args):
    unknown = os.environ.get("FAKECODEX_UNKNOWN")
    for name in disabled(args):
        if name == unknown or (name not in KNOWN_FEATURES):
            print(f"Error: Unknown feature flag: {name}", file=sys.stderr)
            sys.exit(2)


def features_list(args):
    _record({"features_list": args, "cwd": os.getcwd()})
    refuse_unknown(args)
    off = set(disabled(args))
    for name in sorted(KNOWN_FEATURES):
        print(f"{name:40} stable       {'false' if name in off else 'true'}")
    for name in KEPT:
        print(f"{name:40} stable       true")
    print(f"{'old_removed_thing':40} removed      true")
    extra = os.environ.get("FAKECODEX_EXTRA_FEATURE")
    if extra:
        print(f"{extra:40} experimental true")


def login(args):
    if "status" not in args:
        return 0
    state = os.environ.get("FAKECODEX_LOGIN", "chatgpt")
    if state == "chatgpt":
        print("Logged in using ChatGPT", file=sys.stderr)
        return 0
    if state == "apikey":
        print("Logged in using an API key - sk-proj-***", file=sys.stderr)
        return 0
    print("Not logged in", file=sys.stderr)
    return 1


def record(args, prompt):
    path = os.environ.get("FAKECODEX_RECORD")
    if not path:
        return
    env = os.environ
    entry = {"argv": args, "prompt": prompt, "cwd": os.getcwd(),
             "codex_home": env.get("CODEX_HOME"),
             "leaked": sorted(k for k in env if k.startswith(("OPENAI_", "ANTHROPIC_", "CLAUDE_CODE_"))
                              or (k.startswith("CODEX_") and k != "CODEX_HOME"))}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _record(entry):
    path = os.environ.get("FAKECODEX_RECORD")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


def run_exec(args):
    rest = args[args.index("exec") + 1:]
    resuming = bool(rest) and rest[0] == "resume"
    refuse_unknown(rest)
    prompt = sys.stdin.read()
    record(args, prompt)
    if "[pid]" in prompt:
        _record({"pid": os.getpid()})
    if "[config-refused]" in prompt:
        print("Error: unknown configuration field `tools.experimental_request_user_input` "
              "in -c/--config override", file=sys.stderr)
        return 1
    if resuming and os.environ.get("FAKECODEX_RESUME_FAIL") == "1":
        print("Error: thread not found", file=sys.stderr)
        return 1
    thread = rest[-2] if resuming else str(uuid.uuid4())
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    m = re.search(r"\[sleep:(\d+(?:\.\d+)?)\]", prompt)
    if "[grandchild]" in prompt:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        _record({"grandchild": child.pid})
    if "[catalog:code-mode]" in prompt:
        home = os.environ.get("CODEX_HOME")
        if home:
            with open(os.path.join(home, "models_cache.json"), "w", encoding="utf-8") as fh:
                json.dump({"models": [{"slug": "gpt-5.5", "tool_mode": "code_mode_only"}]}, fh)
    for event, kind in re.findall(r"\[item(?:-(updated|completed))?:([a-z_]+)\]", prompt):
        emit({"type": f"item.{event or 'started'}",
              "item": {"id": "item_x", "type": kind, "status": "in_progress"}})
        time.sleep(30)
    for kind in re.findall(r"\[hold:([a-z_]+)\]", prompt):
        go = os.environ.get("FAKECODEX_GO", "")
        deadline = time.time() + 30
        while go and not os.path.exists(go) and time.time() < deadline:
            time.sleep(0.02)
        emit({"type": "item.started",
              "item": {"id": "item_h", "type": kind, "status": "in_progress"}})
        time.sleep(30)
    for name in re.findall(r"\[reader-lists:([^\]]+)\]", prompt):
        item = {"id": "item_list", "type": "mcp_tool_call", "server": "codex",
                "tool": "list_mcp_resources", "arguments": {}, "result": None, "error": None,
                "status": "in_progress"}
        emit({"type": "item.started", "item": item})
        listing = json.dumps({"resources": [{"server": name, "uri": "x://y", "name": "y"}]})
        emit({"type": "item.completed", "item": dict(
            item, status="completed", result={"content": [{"type": "text", "text": listing}]})})
    for tool, item_server, arg in re.findall(r"\[reader-as:([a-z_]+):([^:\]]*):([^\]]*)\]",
                                             prompt):
        item = {"id": "item_as", "type": "mcp_tool_call", "server": item_server, "tool": tool,
                "arguments": {"server": arg} if arg else {}, "result": None,
                "error": None, "status": "in_progress"}
        emit({"type": "item.started", "item": item})
        emit({"type": "item.completed", "item": dict(
            item, status="completed",
            result={"content": [{"type": "text", "text": '{"contents": []}'}]})})
    for marker, text in (("[reader-lists-garbled]", "Wall time: 0.01 seconds"),
                         ("[reader-lists-extra]", json.dumps({"resources": [], "contents": [
                             {"text": "do this"}]}))):
        if marker in prompt:
            item = {"id": "item_lg", "type": "mcp_tool_call", "server": "codex",
                    "tool": "list_mcp_resources", "arguments": {}, "result": None,
                    "error": None, "status": "in_progress"}
            emit({"type": "item.started", "item": item})
            emit({"type": "item.completed", "item": dict(
                item, status="completed", result={"content": [{"type": "text", "text": text}]})})
    if "[toggle]" in prompt and os.environ.get("FAKECODEX_PLANT"):
        os.makedirs(os.path.dirname(os.environ["FAKECODEX_PLANT"]), exist_ok=True)
        with open(os.environ["FAKECODEX_PLANT"], "w", encoding="utf-8") as fh:
            fh.write("x = 1" + chr(10))
        time.sleep(0.05)
        os.remove(os.environ["FAKECODEX_PLANT"])
    if "[plant]" in prompt and os.environ.get("FAKECODEX_PLANT"):
        os.makedirs(os.path.dirname(os.environ["FAKECODEX_PLANT"]), exist_ok=True)
        with open(os.environ["FAKECODEX_PLANT"], "w", encoding="utf-8") as fh:
            fh.write('openai_base_url = "http://127.0.0.1:9/elsewhere"' + chr(10))
    for answered, tool, name in re.findall(
            r"\[reader(-answered)?:([a-z_]+)(?::([^\]]+))?\]", prompt):
        args_ = {"server": name, "uri": "x://y"} if name else {}
        item = {"id": f"item_{tool}", "type": "mcp_tool_call", "server": name or "codex",
                "tool": tool, "arguments": args_, "result": None, "error": None,
                "status": "in_progress"}
        emit({"type": "item.started", "item": item})
        if name and not answered:
            emit({"type": "item.completed", "item": dict(
                item, status="failed",
                error={"message": f"resources/read failed: unknown MCP server '{name}'"})})
        else:
            emit({"type": "item.completed", "item": dict(
                item, status="completed",
                result={"content": [{"type": "text", "text": '{"resources":[]}'}]})})
    if "[bigline]" in prompt:
        # One line far past any stream limit a test sets, then business as
        # usual: the reader must skip it and go on.
        emit({"type": "item.completed", "item": {"id": "item_r", "type": "reasoning",
                                                 "text": "x" * 20000}})
    if "[reconnect]" in prompt:
        emit({"type": "error", "message": "Reconnecting... 1/5"})
    # Tools first, then any wait: a turn that runs out of time has usually
    # called something, and what it called has to be reported.
    for server, tool in re.findall(r"\[tool:([^:\]]+):([^\]]+)\]", prompt):
        item = {"id": f"item_{tool}", "type": "mcp_tool_call", "server": server, "tool": tool,
                "arguments": {}, "status": "in_progress"}
        emit({"type": "item.started", "item": item})
        emit({"type": "item.completed", "item": dict(item, status="completed")})
    if m:
        time.sleep(float(m.group(1)))
    if "[usage-limit" in prompt:
        message = LIMIT_MESSAGE if "[usage-limit]" in prompt else "You've hit your usage limit."
        emit({"type": "error", "message": message})
        emit({"type": "turn.failed", "error": {"message": message}})
        return 1
    if "[fail]" in prompt:
        emit({"type": "turn.failed", "error": {"message": "stream disconnected before completion"}})
        return 1
    if "[silent]" not in prompt:
        emit({"type": "item.completed",
              "item": {"id": "item_msg", "type": "agent_message",
                       "text": f"ChatGPT here: {prompt.strip()[-80:]}"}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    return 0


def main(args):
    if "exec" not in args:
        _record({"invocation": args[:4], "cwd": os.getcwd()})
    if args == ["--version"]:
        print("codex-cli 0.155.0-alpha.9.2-fake")
        return 0
    if "exec" in args:
        return run_exec(args)
    if "features" in args and "list" in args:
        features_list(args)
        return 0
    if "login" in args:
        return login(args)
    if "logout" in args:
        return 0
    if "debug" in args and "models" in args:
        # FAKECODEX_CATALOG: JSON to print instead ("fail" exits 1).
        catalog = os.environ.get("FAKECODEX_CATALOG")
        if catalog == "fail":
            print("Error: could not load the model catalog", file=sys.stderr)
            return 1
        print(catalog or json.dumps({"models": [
            {"slug": "gpt-5.5"}, {"slug": "gpt-6-sol", "tool_mode": "code_mode_only"}]}))
        return 0
    print(f"fake codex: unhandled {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
