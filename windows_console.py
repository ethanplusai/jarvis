"""Isolated console input helper. Never types into the foreground window.

AttachConsole addresses the target process's console; inaccessible/ConPTY
targets that cannot be verified are refused. Only Return, Escape and digits
1–9 are accepted. This module is invoked in a short-lived helper process.
"""
import ctypes as c
from ctypes import wintypes as w
import json
import os
import sys


def operate(pid, key=None):
    if os.name != "nt" or pid <= 0 or (key is not None and key not in ("return", "escape", *"123456789")):
        return {"status": "bad_key" if key is not None else "no_tty"}
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole()
    kernel.AttachConsole.argtypes = [w.DWORD]
    if not kernel.AttachConsole(pid):
        return {"status": "no_tty"}
    kernel.SetConsoleCtrlHandler(None, True)
    try:
        pids = (w.DWORD * 256)()
        count = kernel.GetConsoleProcessList(pids, 256)
        if not count or count > 256 or pid not in pids[:count]:
            return {"status": "no_tty"}
        identity = "console:" + ",".join(str(n) for n in sorted(pids[:count]) if n != os.getpid())
        if key is None:
            return {"status": "ok", "tty": identity}
        class Key(c.Structure):
            _fields_ = [("down", w.BOOL), ("repeat", w.WORD), ("vk", w.WORD),
                        ("scan", w.WORD), ("char", w.WCHAR), ("control", w.DWORD)]
        class Event(c.Union):
            _fields_ = [("key", Key), ("padding", c.c_byte * 20)]
        class Record(c.Structure):
            _fields_ = [("kind", w.WORD), ("event", Event)]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p,
                                       w.DWORD, w.DWORD, w.HANDLE]
        handle = kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        if handle == c.c_void_p(-1).value:
            return {"status": "not_permitted"}
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.WriteConsoleInputW.argtypes = [w.HANDLE, c.POINTER(Record), w.DWORD, c.POINTER(w.DWORD)]
        try:
            records = (Record * 2)()
            char = "\r" if key == "return" else "\x1b" if key == "escape" else key
            for index, down in enumerate((True, False)):
                records[index].kind = 1  # KEY_EVENT
                records[index].event.key = Key(down, 1, ord(char), 0, char, 0)
            written = w.DWORD()
            ok = kernel.WriteConsoleInputW(handle, records, 2, c.byref(written))
            return {"status": "sent" if ok and written.value == 2 else "failed"}
        finally:
            kernel.CloseHandle(handle)
    finally:
        kernel.FreeConsole()


if __name__ == "__main__":
    try:
        result = operate(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
    except Exception:
        result = {"status": "failed"}
    print(json.dumps(result))
