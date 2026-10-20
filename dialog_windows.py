"""dialog.py's keypress on Windows: one key into one process's own console.

Run as a child process, never imported into the server: it has to detach
from whatever console it was born with (`FreeConsole`) and attach to the
target's (`AttachConsole`), and a process has at most one console. Doing
that in the server would take it off the terminal the user started it from.

The identity rule is dialog.py's, and it is kept exactly: the target is the
console that owns the session's pid, found by pid, never by focus. A
process that has no console of its own (the desktop app, an IDE extension,
the SDK) cannot be attached to, and that is `none`: press nothing. The
console is named by its window handle (`GetConsoleWindow`, which a ConPTY
console in Windows Terminal also has), and the press re-checks that name at
press time, so a pid whose console changed since the lookup gets nothing.

The key goes in as a pair of KEY_EVENT records (down, up) through
`WriteConsoleInputW` on that console's own input buffer. Focus is not
touched: no window comes forward, and the window the user is typing into
cannot receive it, because it is not addressed at a window at all.

The vocabulary is dialog.py's closed one (Return, Escape, 1-9), checked
again here: this process refuses anything else before attaching.

    python dialog_windows.py identify <pid>              -> console:0x... | none
    python dialog_windows.py press <pid> <key> <console> -> ok | gone | moved | refused
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value
_KEY_EVENT = 0x0001

# (virtual-key code, character) for each key in dialog.py's vocabulary.
_KEYS = {"return": (0x0D, "\r"), "escape": (0x1B, "\x1b")}
_KEYS.update({d: (ord(d), d) for d in "123456789"})


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", _KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


_kernel32.AttachConsole.argtypes = [wintypes.DWORD]
_kernel32.GetConsoleWindow.restype = wintypes.HWND
_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
_kernel32.WriteConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_INPUT_RECORD),
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_user32 = ctypes.WinDLL("user32")
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]


def _attach(pid: int) -> str | None:
    """Attach to `pid`'s console; its name, or None if it has none."""
    _kernel32.FreeConsole()
    if not _kernel32.AttachConsole(pid):
        return None
    hwnd = _kernel32.GetConsoleWindow()
    return f"console:{hwnd:#x}" if hwnd else None


def identify(pid: int) -> str:
    try:
        return _attach(pid) or "none"
    finally:
        _kernel32.FreeConsole()


def press(pid: int, key: str, expected: str) -> str:
    if key not in _KEYS:
        return "refused"
    try:
        console = _attach(pid)
        if console is None:
            return "gone"
        if console != expected:
            return "moved"
        conin = _kernel32.CreateFileW("CONIN$", _GENERIC_READ | _GENERIC_WRITE,
                                      _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                                      _OPEN_EXISTING, 0, None)
        if not conin or conin == _INVALID_HANDLE:
            return "gone"
        try:
            vk, char = _KEYS[key]
            records = (_INPUT_RECORD * 2)()
            for record, down in zip(records, (True, False)):
                record.EventType = _KEY_EVENT
                event = record.Event.KeyEvent
                event.bKeyDown = down
                event.wRepeatCount = 1
                event.wVirtualKeyCode = vk
                event.wVirtualScanCode = _user32.MapVirtualKeyW(vk, 0)
                event.uChar = char
            written = wintypes.DWORD(0)
            if not _kernel32.WriteConsoleInputW(conin, records, 2, ctypes.byref(written)) \
                    or written.value != 2:
                return "gone"
            return "ok"
        finally:
            _kernel32.CloseHandle(conin)
    finally:
        _kernel32.FreeConsole()


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 2 and argv[0] == "identify":
            print(identify(int(argv[1])))
            return 0
        if len(argv) == 4 and argv[0] == "press":
            print(press(int(argv[1]), argv[2], argv[3]))
            return 0
    except ValueError:
        pass
    print("refused")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
