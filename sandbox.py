"""Running Python that JARVIS did not ship: evaluated code, learned skills.

Every execution is a separate `python -I -S -X utf8` child in an empty
temporary directory with a scrubbed environment, a wall-clock timeout, and
capped output. The child is never this process: an infinite loop, a segfault
or a `sys.exit` in generated code costs one child and nothing else.

Limits, per platform:

  POSIX    rlimits for CPU, address space, file size and core dumps; the child
           leads its own session, and that whole process group is killed when
           it finishes or times out.
  Windows  a Job Object: CPU time, committed memory, ONE process (the child
           cannot start another — no `cmd`, no `subprocess`), no crash dialog,
           and kill-on-close, so nothing it started outlives it. Windows has
           no file-size limit. The child joins the job right after it is
           created, milliseconds before the interpreter has read a line of the
           code it was given.

What this is NOT is a security boundary against hostile code. The child runs
as the user, can read their files and can open sockets — there is no portable
way to stop that without a dependency this project will not take. The gate
against hostile code is upstream of here: only the user's own turn can ask
for an execution, and a learned skill only runs once the user has approved it
(see learning.py). This module makes honest mistakes cheap; it does not make
malicious ones safe, and nothing should describe it as if it did.

`-I` isolates from the user's environment and site directory; `-S` drops
`site` altogether, so only the standard library is importable — a skill that
needs a package is a skill this system cannot evaluate, which is the point.
`-X utf8` is a flag rather than PYTHONUTF8 because `-I` ignores every PYTHON*
variable: without it a Windows child prints in the ANSI code page, and the
first "é" it prints cannot be decoded here.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SEC = 10.0
MAX_TIMEOUT_SEC = 15.0          # inside the MCP child's 20s budget, always
OUTPUT_CAP = 8000
MEMORY_MB = 512
FILE_MB = 16

# The harness prints its verdict on a line of its own, behind this marker and
# a nonce it reads from stdin before the code under test is loaded. Code that
# prints a verdict of its own and exits early cannot know the nonce, so it
# cannot be mistaken for the harness. (Code that goes looking for the nonce in
# the harness's frames can; see "NOT a security boundary" above.)
_MARKER = "\x1e__JARVIS_SANDBOX__"


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    duration: float

    def to_dict(self) -> dict:
        return asdict(self)


def _cap(text: str | None) -> str:
    text = text or ""
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + "\n… (truncated)"


def _limit_child() -> None:   # pragma: no cover - runs in the child
    """POSIX resource limits. Each one is best-effort: macOS refuses
    RLIMIT_AS outright, and a limit that cannot be set is not a reason to
    refuse to run."""
    import resource
    for which, value in ((resource.RLIMIT_CPU, (int(MAX_TIMEOUT_SEC) + 1,) * 2),
                         (resource.RLIMIT_AS, (MEMORY_MB * 2 ** 20,) * 2),
                         (resource.RLIMIT_FSIZE, (FILE_MB * 2 ** 20,) * 2),
                         (resource.RLIMIT_CORE, (0, 0))):
        try:
            resource.setrlimit(which, value)
        except (ValueError, OSError):
            pass


def _clean_env(home: str) -> dict:
    """Nothing of the server's environment: no API keys, no tool token path,
    no proxy credentials. Only what an interpreter needs to start — which on
    Windows includes SYSTEMROOT: without it Winsock cannot load its service
    providers and `socket.socket()` fails with WinError 10106."""
    if os.name == "nt":
        root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        return {"SYSTEMROOT": root, "WINDIR": root,
                "PATH": os.path.join(root, "System32"),
                "TEMP": home, "TMP": home, "USERPROFILE": home, "HOME": home}
    return {"PATH": "/usr/bin:/bin", "HOME": home, "TMPDIR": home, "LANG": "C.UTF-8"}


# ---------------------------------------------------------------------------
# Windows: one Job Object per child
# ---------------------------------------------------------------------------

class _Job:
    """Best-effort, like the rlimits: a job that cannot be made is not a
    reason to refuse to run. `close()` kills whatever is still in it."""

    def __init__(self, handle: int | None = None):
        self.handle = handle

    @classmethod
    def create(cls) -> "_Job":
        if os.name != "nt":
            return cls()
        try:
            return cls(_win_job())
        except OSError:
            return cls()

    def assign(self, pid: int) -> None:
        if self.handle is None:
            return
        k32 = _kernel32()
        process = k32.OpenProcess(0x0100 | 0x0001, False, pid)   # SET_QUOTA | TERMINATE
        if process:
            k32.AssignProcessToJobObject(self.handle, process)
            k32.CloseHandle(process)

    def close(self) -> None:
        if self.handle is not None:
            _kernel32().CloseHandle(self.handle)
            self.handle = None


def _kernel32():   # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    k32.SetInformationJobObject.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return k32


def _win_job() -> int:   # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic),
                    ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    basic = info.BasicLimitInformation
    basic.LimitFlags = (0x0002      # PROCESS_TIME
                        | 0x0008    # ACTIVE_PROCESS
                        | 0x0100    # PROCESS_MEMORY
                        | 0x0400    # DIE_ON_UNHANDLED_EXCEPTION: no WER dialog
                        | 0x2000)   # KILL_ON_JOB_CLOSE
    basic.PerProcessUserTimeLimit = int((MAX_TIMEOUT_SEC + 1) * 10_000_000)   # 100 ns
    basic.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = MEMORY_MB * 2 ** 20
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def _interpreter() -> str:
    """On Windows a venv's python.exe is a launcher that starts the base
    interpreter as a second process, which the job's one-process limit
    refuses. Under `-S` the two are the same interpreter anyway: neither
    sees the venv's site-packages."""
    if os.name == "nt":
        base = getattr(sys, "_base_executable", "")
        if base and os.path.isfile(base):
            return base
    return sys.executable


def _kill_group(proc: subprocess.Popen) -> None:
    """POSIX: the child and anything it started share its session's group."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def run_python(code: str, *, timeout: float = DEFAULT_TIMEOUT_SEC,
               stdin: str = "", files: dict[str, str] | None = None,
               script: str | None = None) -> SandboxResult:
    """Execute `code` (or `script`, with `code` written beside it as
    `subject.py`) and report what happened. Blocking — call it through
    `asyncio.to_thread` from the event loop."""
    timeout = max(0.5, min(float(timeout), MAX_TIMEOUT_SEC))
    with tempfile.TemporaryDirectory(prefix="jarvis-sandbox-") as work:
        root = Path(work)
        (root / "subject.py").write_text(code, encoding="utf-8")
        for name, content in (files or {}).items():
            (root / Path(name).name).write_text(content, encoding="utf-8")
        entry = root / "main.py"
        entry.write_text(script if script is not None else code, encoding="utf-8")
        started = time.monotonic()
        kwargs = dict(cwd=work, env=_clean_env(work), stdin=subprocess.PIPE,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      text=True, encoding="utf-8", errors="replace")
        if os.name == "posix":
            kwargs.update(start_new_session=True, preexec_fn=_limit_child)
        job = _Job.create()
        try:
            try:
                proc = subprocess.Popen(
                    [_interpreter(), "-I", "-S", "-X", "utf8", str(entry)], **kwargs)
            except OSError as e:
                return SandboxResult(False, "", f"could not start the interpreter: {e}",
                                     None, False, time.monotonic() - started)
            job.assign(proc.pid)
            try:
                stdout, stderr = proc.communicate(stdin, timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                job.close()
                proc.kill()
                stdout, stderr = proc.communicate()
                timed_out = True
            _kill_group(proc)       # anything it left running
        finally:
            job.close()
        duration = time.monotonic() - started
        if timed_out:
            return SandboxResult(False, _cap(stdout), _cap(stderr), None, True, duration)
        return SandboxResult(proc.returncode == 0, _cap(stdout), _cap(stderr),
                             proc.returncode, False, duration)


# ---------------------------------------------------------------------------
# Skills: a module defining `run(**kwargs)`, plus test cases for it
# ---------------------------------------------------------------------------

# Both harnesses read the nonce off stdin BEFORE the subject is loaded, and
# keep it in a function's locals rather than a global the subject shares.
_TEST_HARNESS = f"""
import json, sys, traceback
def _main(nonce):
    spec = json.load(open("cases.json"))
    ns = {{"__name__": "skill"}}
    results = []
    try:
        exec(compile(open("subject.py").read(), "skill.py", "exec"), ns)
        loaded = callable(ns.get("run"))
        load_error = "" if loaded else "the code defines no run() function"
    except BaseException as e:
        loaded, load_error = False, "".join(traceback.format_exception_only(type(e), e)).strip()
    for case in spec["tests"]:
        if not loaded:
            results.append({{"name": case.get("name", "?"), "ok": False, "error": load_error}})
            continue
        try:
            exec(compile(case["code"], "test_" + str(case.get("name", "case")), "exec"), dict(ns))
            results.append({{"name": case.get("name", "?"), "ok": True, "error": ""}})
        except BaseException as e:
            results.append({{"name": case.get("name", "?"), "ok": False,
                            "error": "".join(traceback.format_exception_only(type(e), e)).strip()[:400]}})
    sys.stdout.write("\\n" + {_MARKER!r} + nonce + json.dumps(results) + "\\n")
_main(sys.stdin.readline().strip())
"""

_CALL_HARNESS = f"""
import inspect, json, sys
def _main(nonce):
    ns = {{"__name__": "skill"}}
    exec(compile(open("subject.py").read(), "skill.py", "exec"), ns)
    kwargs = json.load(open("kwargs.json"))
    run = ns["run"]
    try:
        inspect.signature(run).bind(**kwargs)
    except TypeError as e:
        # The caller's mistake, not the skill's: reported apart, so it is
        # never recorded as an input the skill must be repaired against.
        sys.stdout.write("\\n" + {_MARKER!r} + nonce + json.dumps({{"bad_arguments": str(e)}}) + "\\n")
        return
    except ValueError:
        pass                    # no signature to check against
    value = run(**kwargs)
    try:
        encoded = json.dumps({{"value": value}})
    except TypeError:
        encoded = json.dumps({{"value": repr(value)}})
    sys.stdout.write("\\n" + {_MARKER!r} + nonce + encoded + "\\n")
_main(sys.stdin.readline().strip())
"""


def _verdict(stdout: str, nonce: str):
    """The harness line, parsed, and the stdout with it removed."""
    head, sep, tail = stdout.rpartition(_MARKER + nonce)
    if not sep:
        return None, stdout
    line, _, _ = tail.partition("\n")
    try:
        return json.loads(line), head
    except ValueError:
        return None, stdout


@dataclass
class SuiteReport:
    passed: int
    total: int
    failures: list[dict]
    timed_out: bool
    stderr: str

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total and not self.timed_out


def run_tests(code: str, tests: list[dict], *,
              timeout: float = DEFAULT_TIMEOUT_SEC) -> SuiteReport:
    """Each test is `{"name": ..., "code": "assert run(x=1) == 2"}`, executed
    in a fresh copy of the skill's namespace."""
    tests = [t for t in tests if isinstance(t, dict) and isinstance(t.get("code"), str)]
    if not tests:
        return SuiteReport(0, 0, [{"name": "-", "error": "no tests"}], False, "")
    nonce = secrets.token_hex(16)
    result = run_python(code, timeout=timeout, script=_TEST_HARNESS, stdin=nonce + "\n",
                        files={"cases.json": json.dumps({"tests": tests})})
    verdict, _ = _verdict(result.stdout, nonce)
    # A verdict only counts from a child that exited cleanly and reported on
    # every test it was given.
    if not (isinstance(verdict, list) and len(verdict) == len(tests) and result.ok):
        error = "timed out" if result.timed_out else (result.stderr.strip()[-400:]
                                                      or "the harness did not report")
        return SuiteReport(0, len(tests), [{"name": t.get("name", "?"), "error": error}
                                          for t in tests], result.timed_out, result.stderr)
    failures = [r for r in verdict if not r.get("ok")]
    return SuiteReport(len(verdict) - len(failures), len(tests), failures,
                      result.timed_out, result.stderr)


@dataclass
class CallResult:
    ok: bool
    value: object
    output: str
    error: str
    timed_out: bool
    # The arguments did not fit run()'s signature: the caller's mistake.
    bad_arguments: bool = False


def call_skill(code: str, kwargs: dict, *,
               timeout: float = DEFAULT_TIMEOUT_SEC) -> CallResult:
    """Call the skill's `run(**kwargs)` and return its JSON-able value."""
    nonce = secrets.token_hex(16)
    result = run_python(code, timeout=timeout, script=_CALL_HARNESS, stdin=nonce + "\n",
                        files={"kwargs.json": json.dumps(kwargs)})
    verdict, printed = _verdict(result.stdout, nonce)
    if result.ok and isinstance(verdict, dict):
        if "bad_arguments" in verdict:
            return CallResult(False, None, printed.strip(), str(verdict["bad_arguments"]),
                              False, bad_arguments=True)
        if "value" in verdict:
            return CallResult(True, verdict["value"], printed.strip(), "", False)
    error = "timed out" if result.timed_out else (
        result.stderr.strip().splitlines()[-1] if result.stderr.strip() else
        f"exited {result.returncode}")
    return CallResult(False, None, printed.strip(), error, result.timed_out)
