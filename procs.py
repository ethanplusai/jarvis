"""Is a process alive? One answer on every platform JARVIS runs on.

`session_watch` needs to know which Claude Code sessions in the roster still
have a process behind them, and the test-suite needs to prove a child JARVIS
killed is really gone. Both used to ask `os.kill(pid, 0)`, the POSIX idiom for
"probe without touching". On Windows that idiom is not a probe:

* signal 0 is `CTRL_C_EVENT` there, so `os.kill(pid, 0)` asks the console to
  deliver Ctrl+C — to every process sharing the console when it succeeds,
  and `WinError 87` when the pid is not a process-group leader — and never
  says whether the process exists. Measured on Python 3.14: a live child and
  a dead pid both "succeeded", and under pytest every liveness assertion
  raised instead.

So the Windows branch asks the kernel directly: open a query-only handle and
read the exit code. `STILL_ACTIVE` means running. Access denied means the
process exists and belongs to somebody else, which is still "alive". The
POSIX branch keeps the idiom that has always worked there.
"""

from __future__ import annotations

import os

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    def _alive(pid: int) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # No such process, or one we may not even query. Only the second
            # is a process at all.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            # A process that exited with code 259 is indistinguishable from a
            # running one; that is Windows' own documented caveat, not ours.
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

else:

    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True      # exists; not ours to signal
        except OSError:
            return False
        return True


def pid_alive(pid) -> bool:
    """True if the process exists. Never signals it, on any platform.

    `pid` must be a positive integer: 0 means "this process's group" and a
    negative pid means "that group" to `os.kill`, neither of which is a real
    process, so both are rejected before any syscall.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return _alive(pid)
