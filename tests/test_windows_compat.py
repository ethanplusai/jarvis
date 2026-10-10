"""JARVIS was written on macOS. These are the places Windows disagreed.

Each test here pins a behaviour that was measured broken on Windows 11 with
Python 3.14 and is now the same on every platform. They run everywhere: the
POSIX branches are the ones that were always right, and the assertions hold
there too, so a regression on either side shows up on both.
"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import claude_env
import procs
import session_watch as sw
import usage_scan as us

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = sys.platform == "win32"


# ── the Claude CLI path must survive being turned into argv ────────────────
#
# `shutil.which("claude")` returns `C:\Users\...\claude.EXE` on Windows, and
# `shlex.split` in POSIX mode strips every backslash out of it. The brain
# then tried to start `C:Users...claude.EXE` and JARVIS booted mute.

def test_a_windows_path_to_the_cli_is_kept_whole(tmp_path):
    exe = tmp_path / "claude.EXE"
    exe.write_bytes(b"")
    spec = str(exe)
    assert claude_env.split_command(spec) == [spec]


def test_a_path_with_backslashes_that_does_not_exist_is_still_not_mangled_on_windows():
    spec = r"C:\nowhere\claude.EXE"
    argv = claude_env.split_command(spec)
    if WINDOWS:
        assert argv == [spec]
    else:
        # POSIX shlex semantics are unchanged on POSIX: a backslash escapes.
        assert argv == ["C:nowhereclaude.EXE"]


def test_a_command_line_is_still_split_into_argv():
    assert claude_env.split_command("node /some/where/cli.js --flag") == [
        "node", "/some/where/cli.js", "--flag"]


def test_the_brain_and_the_run_executor_both_use_it():
    """Two copies of the split is the failure mode `claude_env` exists to
    prevent: the copy nobody updated is the one that breaks."""
    for name in ("brain.py", "run_executor.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert "claude_env.split_command(" in src, name
        assert "shlex.split(self._claude" not in src, name


# ── epoch bounds must not need a pre-1970 local timestamp ──────────────────
#
# `datetime(2, 1, 1).timestamp()` asks the C runtime for a local-time
# conversion of year 2, which Windows refuses (OSError 22) — at import, so
# `server.py` never got as far as its own first line.

def test_the_day_key_bounds_are_the_utc_epoch_seconds_of_year_2_and_9997():
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert us._DAY_MIN == (datetime(2, 1, 1, tzinfo=timezone.utc) - epoch).total_seconds()
    assert us._DAY_MAX == (datetime(9997, 1, 1, tzinfo=timezone.utc) - epoch).total_seconds()
    assert us._DAY_MIN < 0 < us._DAY_MAX


def test_a_normal_stamp_is_inside_the_bounds_and_year_1_is_not():
    assert us._epoch("2026-09-17T12:00:00.000Z") == pytest.approx(1789646400.0)
    assert us._epoch("0001-01-01T00:00:00.000Z") is None


# ── probing a pid must never signal it ─────────────────────────────────────
#
# On Windows `os.kill(pid, 0)` is CTRL_C_EVENT, not a probe: it reports
# nothing about liveness and can interrupt every process on the console.

def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def test_a_running_child_is_alive_and_the_probe_does_not_touch_it():
    child = _sleeper()
    try:
        assert procs.pid_alive(child.pid) is True
        time.sleep(0.2)
        assert child.poll() is None, "probing the pid killed or interrupted the child"
        assert procs.pid_alive(child.pid) is True
    finally:
        child.kill()
        child.wait()


def test_a_killed_and_reaped_child_is_dead():
    child = _sleeper()
    child.kill()
    child.wait()
    assert procs.pid_alive(child.pid) is False


def test_garbage_pids_are_dead_without_a_syscall():
    for pid in (None, "", "x", 0, -1, -os.getpid()):
        assert procs.pid_alive(pid) is False


def test_session_watch_delegates_to_the_shared_probe(monkeypatch):
    seen = []
    monkeypatch.setattr(procs, "pid_alive", lambda pid: seen.append(pid) or True)
    assert sw.pid_alive(4242) is True
    assert seen == [4242]


def _os_kill_zero_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value == 0):
            hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_no_runtime_module_probes_with_os_kill_zero():
    """`procs.py` owns the one POSIX probe; nothing else may grow its own."""
    hits = []
    for path in ROOT.glob("*.py"):
        if path.name != "procs.py":
            hits += _os_kill_zero_calls(path)
    assert hits == [], hits


# ── the tool token file must be adoptable without POSIX-only calls ─────────
#
# `os.O_NOFOLLOW`, `os.fchmod` and `os.getuid` do not exist on Windows; the
# second boot (the first with an existing token) died in `ensure_tool_token`.

def _fresh_data_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    return data_paths


def test_the_token_is_created_then_adopted_on_the_next_boot(monkeypatch, tmp_path):
    dp = _fresh_data_paths(monkeypatch, tmp_path)
    first = dp.ensure_tool_token()
    assert len(first) >= 32
    assert dp.ensure_tool_token() == first, "a restart must keep the same token"
    assert dp.tool_token_path().read_text(encoding="utf-8").strip() == first


def test_an_empty_token_file_is_filled_not_trusted(monkeypatch, tmp_path):
    dp = _fresh_data_paths(monkeypatch, tmp_path)
    path = dp.tool_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    token = dp.ensure_tool_token()
    assert token and path.read_text(encoding="utf-8").strip() == token


def test_a_symlink_at_the_token_path_is_refused(monkeypatch, tmp_path):
    dp = _fresh_data_paths(monkeypatch, tmp_path)
    path = dp.tool_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "somebody-elses-file"
    victim.write_text("not a token", encoding="utf-8")
    try:
        path.symlink_to(victim)
    except OSError as e:
        pytest.skip(f"cannot create symlinks here ({e})")
    with pytest.raises(OSError):
        dp.ensure_tool_token()
    assert victim.read_text(encoding="utf-8") == "not a token"


# ── a CRLF checkout of the persona is not an edit ──────────────────────────
#
# git's autocrlf hands Windows a CRLF `jarvis_home/CLAUDE.md`. The template
# was hashed after newline translation and the live copy before it, so the
# copy JARVIS had just written read as "edited" at the very next boot, and
# every persona improvement shipped after that would have been inert.

def test_the_seeded_persona_is_byte_identical_to_the_hashed_template(monkeypatch, tmp_path):
    dp = _fresh_data_paths(monkeypatch, tmp_path)
    assert dp.sync_persona() == "seeded"
    written = dp.persona_path().read_bytes()
    assert b"\r\n" not in written
    assert written == dp.persona_template_path().read_text(encoding="utf-8").encode("utf-8")
    assert dp.sync_persona() == "current"


def test_a_persona_rewritten_with_crlf_still_counts_as_unedited(monkeypatch, tmp_path):
    dp = _fresh_data_paths(monkeypatch, tmp_path)
    dp.sync_persona()
    path = dp.persona_path()
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert dp.sync_persona() == "current"


def test_a_real_edit_is_still_kept(monkeypatch, tmp_path, caplog):
    dp = _fresh_data_paths(monkeypatch, tmp_path)
    dp.sync_persona()
    path = dp.persona_path()
    path.write_bytes(path.read_bytes() + b"\r\nMy own rule.\r\n")
    assert dp.sync_persona() == "kept"
    assert path.read_bytes().endswith(b"My own rule.\r\n")


# ── a Windows path is a project path ───────────────────────────────────────
#
# The wall in front of the project map admitted only paths beginning with
# `/`, so on Windows `create_project` made a directory that JARVIS then
# could not see, and no project at all was startable.

def test_an_absolute_windows_path_passes_the_project_wall_and_a_relative_one_does_not(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import server
    assert server._project_path_speakable(r"C:\Users\tony\Projects\arcreactor")
    assert server._project_path_speakable("C:/Users/tony/Projects/arcreactor")
    assert server._project_path_speakable("/Users/tony/Projects/arcreactor")
    assert not server._project_path_speakable(r"Projects\arcreactor")
    assert not server._project_path_speakable("C:\\Users\\tony\\a\nb")
    assert not server._project_path_speakable(r"C:\Users\tony\<tag>")


# ── the smaller things the C runtime, PATH and the socket module differ on ──

def _server(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import server
    return server


def test_the_reset_time_is_formatted_without_glibc_only_codes(monkeypatch, tmp_path):
    """`%-I` and `%-d` raise "Invalid format string" on Windows, and the
    usage tool fell over on the seven-day reset time."""
    from datetime import timedelta
    server = _server(monkeypatch, tmp_path)
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    assert server._fmt_reset(now.timestamp()) == "10 AM"
    assert server._fmt_reset((now + timedelta(days=1)).timestamp()) == "tomorrow at 10 AM"
    far = server._fmt_reset((now + timedelta(days=10)).timestamp())
    assert far.endswith(" at 10 AM")
    assert far.split(" at ")[0].split()[1].isdigit()   # "Monday 28 September"
    afternoon = server._fmt_reset(now.replace(hour=13, minute=5).timestamp())
    assert afternoon.startswith("1:05")


def test_project_roots_are_separated_the_way_path_is(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    monkeypatch.setenv("JARVIS_PROJECT_ROOTS",
                       os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert server._scan_roots() == [tmp_path / "a", tmp_path / "b", server.project_maker.projects_root()]


def test_a_relative_path_inside_a_project_is_spoken_with_forward_slashes(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    root = tmp_path / "chitauri"
    (root / "src").mkdir(parents=True)
    assert server._repo_relative(root, root / "src" / "auth.ts") == "src/auth.ts"


def test_steering_without_unix_sockets_fails_rather_than_crashes(monkeypatch, tmp_path):
    import socket
    import session_steer
    import windows_inbox
    def unavailable(*args, **kwargs):
        raise OSError("Native transport unavailable")
    monkeypatch.setattr(windows_inbox, "send", unavailable)
    monkeypatch.delattr(socket, "AF_UNIX", raising=False)
    sock = tmp_path / "x.sock"
    sock.write_bytes(b"")          # present, so this is not the NOT_LIVE branch
    assert session_steer.post_to_session(str(sock), "hi") == session_steer.FAILED


# ── two documents written in the same instant still have a newest ──────────
#
# Windows stamps writes at the system-timer tick, so a plan written right
# after its spec shares its modification time more often than not, and a
# stable sort then answered "the newest document" with the spec.

def test_a_plan_that_ties_with_its_spec_on_mtime_is_the_newer_document(tmp_path):
    import builds
    import specs
    root = tmp_path / "chitauri"
    root.mkdir()
    spec_rel = builds.write_spec(str(root), "A thing\n\nOne paragraph about it.\n")
    plan = root / builds.PLAN_DIR / "2026-09-17-plan.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# Plan\n\n## Task 1: Do it\n\n- [ ] step\n", encoding="utf-8")
    same = 1_800_000_000
    os.utime(root / spec_rel, (same, same))
    os.utime(plan, (same, same))

    documents = specs.list_documents(str(root))

    assert [d["kind"] for d in documents] == ["plan", "spec"]
    assert documents[0]["modified"] == documents[1]["modified"]


# ── every text file JARVIS touches is UTF-8, and says so ───────────────────
#
# Windows opens text files in the ANSI code page unless told otherwise. A
# persona with an em dash in it, read as cp1252 and hashed, never matches
# the template again; a memory note with one in it is written as mojibake.

def _text_io_without_encoding(path: Path) -> list[str]:
    """Calls in `path` that open a text file and leave the encoding to the
    platform: `read_text()`, `write_text(...)`, `open(...)`, `Path.open(...)`
    and `os.fdopen(...)` with no `encoding=` and no binary mode."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
            owner = func.value.id if isinstance(func.value, ast.Name) else None
        elif isinstance(func, ast.Name):
            name, owner = func.id, None
        else:
            continue
        if name not in {"read_text", "write_text", "open", "fdopen"}:
            continue
        if name == "open" and owner in {"os", "webbrowser"}:
            continue                      # fd-level, and a URL: not text I/O
        if name == "fdopen" and owner != "os":
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        mode = None
        positional_mode = 1 if name in {"open", "fdopen"} else None
        if name == "open" and owner is None and len(node.args) > 1:
            mode = node.args[1]
        elif name in {"open", "fdopen"} and positional_mode is not None and len(node.args) > positional_mode:
            mode = node.args[positional_mode]
        for k in node.keywords:
            if k.arg == "mode":
                mode = k.value
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
            continue                      # binary: no encoding applies
        found.append(f"{path.name}:{node.lineno} {name}(...)")
    return found


def test_every_runtime_text_read_and_write_names_utf8():
    """Runtime modules AND the tests: a test that reads `server.py` with the
    ANSI code page fails for the wrong reason on a machine whose page cannot
    decode it, and the guard is only a guard if it walks everything."""
    offenders = []
    for path in sorted(list(ROOT.glob("*.py")) + list((ROOT / "tests").glob("*.py"))):
        offenders += _text_io_without_encoding(path)
    assert offenders == [], "\n".join(offenders)


def test_a_memory_note_with_non_ascii_survives_a_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    import jarvis_memory
    importlib.reload(data_paths)
    importlib.reload(jarvis_memory)
    text = "Tony asked for the café menu — with a dash and an accent"
    path = jarvis_memory.write_memory("Café", text)
    raw = path.read_bytes()
    assert "café".encode("utf-8") in raw.lower()
    assert path.read_text(encoding="utf-8").rstrip().endswith(text)
