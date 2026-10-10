"""screen.py's two capabilities on Windows: the window list, and one picture.

Same contract as the macOS half — `screen.capture_screen` and
`screen.list_windows` call in here and return the same `Shot` and `Window` —
built on user32/gdi32 through `ctypes`, so it costs no dependency and spawns
no process.

**The same camera, the same rules** (see screen.py's docstring): nothing here
runs unless the user asked on this turn. And this half is stricter about one
of them: the capture never touches the disk at all. GDI draws the display
into a memory bitmap, already shrunk to `SHOT_MAX_EDGE`; the pixels are
encoded to PNG in memory, and the bitmap is freed in a `finally`.

Windows asks no permission for either capability, so there is no probe. What
it can do instead is hand back nothing useful in silence: a locked
workstation, or the secure desktop of a UAC prompt, captures as solid black.
The blank-frame check is kept for exactly that reason.

DPI: an unaware thread sees a 4K display at 150% scaling as a 2560x1440
screen and captures only its top-left part. Each call runs with the thread's
DPI awareness set to per-monitor (v2), restored afterwards. The thread's,
not the process's: the server has other threads, and this one is borrowed
from `asyncio.to_thread` for one call.
"""
from __future__ import annotations

import ctypes
import logging
import os
import struct
import zlib
from ctypes import wintypes

from screen import (BLANK_SAMPLE_EDGE, MAX_SHOT_BYTES, MAX_WINDOWS,
                    SHOT_MAX_EDGE, ScreenError, Shot, Window, _is_blank)

log = logging.getLogger("jarvis.screen")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")
_version = ctypes.WinDLL("version")

_DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_MONITORINFOF_PRIMARY = 1
_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_HALFTONE = 4
_DIB_RGB_COLORS = 0
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# The desktop and the taskbar are windows with titles too ("Program Manager"),
# and neither is something the user has open.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# UWP apps all draw inside this one host, so its name says nothing; the
# window's own title ("Settings", "Calculator") is the app.
_FRAME_HOST = "applicationframehost.exe"


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# Handles are pointer-sized: without these, ctypes passes them as 32-bit ints.
_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
_gdi32.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_gdi32.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                          ctypes.c_void_p, wintypes.DWORD]
_version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
_version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                         wintypes.DWORD, ctypes.c_void_p]
_version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                    ctypes.POINTER(ctypes.c_void_p),
                                    ctypes.POINTER(wintypes.UINT)]


class _dpi_aware:
    """Per-monitor DPI awareness for this thread, for the duration of a call."""

    def __enter__(self):
        self._previous = None
        setter = getattr(_user32, "SetThreadDpiAwarenessContext", None)
        if setter is not None:            # Windows 10 1607+; older: unaware, as before
            setter.restype = ctypes.c_void_p
            setter.argtypes = [ctypes.c_void_p]
            self._previous = setter(_DPI_PER_MONITOR_AWARE_V2)
            self._setter = setter
        return self

    def __exit__(self, *exc):
        if self._previous:
            self._setter(ctypes.c_void_p(self._previous))


# ── displays ───────────────────────────────────────────────────────────────

def order_displays(monitors: list[tuple[tuple[int, int, int, int], bool]]
                   ) -> list[tuple[int, int, int, int]]:
    """Number the displays as the user would: the primary is 1, then the rest
    left to right (top to bottom on a tie). Rects are (left, top, right, bottom)."""
    primary = [r for r, is_primary in monitors if is_primary]
    others = sorted((r for r, is_primary in monitors if not is_primary),
                    key=lambda r: (r[0], r[1]))
    return primary + others


def _displays() -> list[tuple[int, int, int, int]]:
    found: list[tuple[tuple[int, int, int, int], bool]] = []

    def collect(hmonitor, _hdc, _rect, _data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(((r.left, r.top, r.right, r.bottom),
                          bool(info.dwFlags & _MONITORINFOF_PRIMARY)))
        return True

    callback = _MONITORENUMPROC(collect)  # kept alive for the call's duration
    _user32.EnumDisplayMonitors(None, None, callback, 0)
    return order_displays(found)


# ── the picture ────────────────────────────────────────────────────────────

def fit(width: int, height: int, edge: int = SHOT_MAX_EDGE) -> tuple[int, int]:
    """The size to draw at: the longest edge at most `edge`, aspect kept."""
    scale = min(1.0, edge / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """A truecolour PNG from top-down 32-bit BGRA rows, with only zlib.

    Filter 0 on every row: the per-row filters would compress a little better,
    but they need a Python loop per byte, and a screenshot of flat UI
    compresses well without them.
    """
    n = width * height
    rgb = bytearray(n * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    row = width * 3
    raw = b"".join(b"\x00" + rgb[y * row:(y + 1) * row] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def sample_pixels(width: int, height: int, bgra: bytes,
                  edge: int = BLANK_SAMPLE_EDGE) -> list[tuple[int, int, int]]:
    """An `edge` x `edge` grid of (b, g, r) triples, for screen._is_blank."""
    xs = [min(width - 1, (i * width) // edge) for i in range(edge)]
    ys = [min(height - 1, (j * height) // edge) for j in range(edge)]
    out = []
    for y in ys:
        for x in xs:
            i = (y * width + x) * 4
            out.append((bgra[i], bgra[i + 1], bgra[i + 2]))
    return out


def _grab(rect: tuple[int, int, int, int], width: int, height: int) -> bytes:
    """The display at `rect`, drawn into a `width` x `height` memory bitmap."""
    left, top, right, bottom = rect
    screen_dc = _user32.GetDC(None)
    if not screen_dc:
        raise ScreenError("I couldn't get a picture of your screen, sir")
    mem_dc = bitmap = old = None
    try:
        mem_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not mem_dc or not bitmap:
            raise ScreenError("I couldn't get a picture of your screen, sir")
        old = _gdi32.SelectObject(mem_dc, bitmap)
        # HALFTONE averages the pixels it drops, so shrunk text stays text;
        # the default mode just deletes rows and columns.
        _gdi32.SetStretchBltMode(mem_dc, _HALFTONE)
        _gdi32.SetBrushOrgEx(mem_dc, 0, 0, None)
        if not _gdi32.StretchBlt(mem_dc, 0, 0, width, height, screen_dc,
                                 left, top, right - left, bottom - top,
                                 _SRCCOPY | _CAPTUREBLT):
            log.warning(f"StretchBlt failed: winerror {ctypes.get_last_error()}")
            raise ScreenError("I couldn't get a picture of your screen, sir")
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height          # negative: rows top-down
        header.biPlanes = 1
        header.biBitCount = 32
        pixels = ctypes.create_string_buffer(width * height * 4)
        # The bitmap must not be selected into a DC while GetDIBits reads it.
        _gdi32.SelectObject(mem_dc, old)
        old = None
        if _gdi32.GetDIBits(mem_dc, bitmap, 0, height, pixels,
                            ctypes.byref(header), _DIB_RGB_COLORS) != height:
            raise ScreenError("I couldn't get a picture of your screen, sir")
        return pixels.raw
    finally:
        if old:
            _gdi32.SelectObject(mem_dc, old)
        if bitmap:
            _gdi32.DeleteObject(bitmap)
        if mem_dc:
            _gdi32.DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)


def capture_screen(display: int | None = None) -> Shot:
    """screen.capture_screen on Windows. Blocking: call it in a thread."""
    with _dpi_aware():
        displays = _displays()
        if not displays:
            raise ScreenError("I couldn't find a display to look at, sir")
        index = (display or 1) - 1
        if not 0 <= index < len(displays):
            count = len(displays)
            raise ScreenError(f"I can only see {count} display"
                              f"{'' if count == 1 else 's'}, sir")
        rect = displays[index]
        width, height = fit(rect[2] - rect[0], rect[3] - rect[1])
        bgra = _grab(rect, width, height)

    if _is_blank(sample_pixels(width, height, bgra)):
        raise ScreenError(
            "your screen came back blank, sir — which usually means it's "
            "locked, or a security prompt is up, rather than that there's "
            "nothing there")
    png = encode_png(width, height, bgra)
    if len(png) > MAX_SHOT_BYTES:
        raise ScreenError("that picture came out far too large to send, sir")
    return Shot(png=png, width=width, height=height)


# ── the window list ────────────────────────────────────────────────────────

_app_names: dict[str, str] = {}


def _exe_path(pid: int) -> str:
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def _file_description(path: str) -> str:
    """The name an .exe gives itself ("Google Chrome"), or ""."""
    version = _version
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return ""
    value = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation",
                                  ctypes.byref(value), ctypes.byref(length)) \
            or length.value < 4:
        return ""
    lang, codepage = struct.unpack("<HH", ctypes.string_at(value, 4))
    key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
    if not version.VerQueryValueW(data, key, ctypes.byref(value), ctypes.byref(length)) \
            or not length.value:
        return ""
    return ctypes.wstring_at(value, length.value).rstrip("\x00").strip()


def app_name(exe_path: str) -> str:
    """What to call the app that owns a window: its own description, else
    the .exe's name. Cached per path, since a desk is mostly the same apps."""
    if not exe_path:
        return ""
    if exe_path not in _app_names:
        try:
            name = _file_description(exe_path)
        except OSError:
            name = ""
        _app_names[exe_path] = name or os.path.splitext(os.path.basename(exe_path))[0]
    return _app_names[exe_path]


def _text(getter, hwnd, size: int) -> str:
    buf = ctypes.create_unicode_buffer(size)
    getter(hwnd, buf, size)
    return buf.value


def _is_users_window(hwnd) -> bool:
    """A top-level window the user would call open: visible, not owned by
    another window, not a tool palette, not cloaked (UWP apps keep invisible
    windows around), and not the desktop or the taskbar."""
    if not _user32.IsWindowVisible(hwnd) or _user32.GetWindow(hwnd, _GW_OWNER):
        return False
    if _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
        return False
    cloaked = ctypes.c_int(0)
    _dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked),
                                  ctypes.sizeof(cloaked))
    if cloaked.value:
        return False
    return _text(_user32.GetClassNameW, hwnd, 256) not in _SHELL_CLASSES


def list_windows() -> list[Window]:
    """screen.list_windows on Windows. Blocking: call it in a thread.

    EnumWindows walks top-level windows front to back, so the list comes out
    in the order they are stacked. Read-only: nothing is focused or moved.
    """
    front = _user32.GetForegroundWindow()
    front_pid = wintypes.DWORD(0)
    if front:
        _user32.GetWindowThreadProcessId(front, ctypes.byref(front_pid))
    windows: list[Window] = []

    def visit(hwnd, _data):
        if len(windows) >= MAX_WINDOWS:
            return False                  # stop enumerating
        if not _is_users_window(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        title = _text(_user32.GetWindowTextW, hwnd, length + 1).strip() if length else ""
        if not title:
            return True
        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = _exe_path(pid.value)
        name = title if os.path.basename(path).lower() == _FRAME_HOST else app_name(path)
        # Front means the front APP, as on macOS: every window it owns.
        windows.append(Window(app=name or "Unknown app", title=title,
                              frontmost=bool(front) and pid.value == front_pid.value))
        return True

    callback = _WNDENUMPROC(visit)        # kept alive for the call's duration
    _user32.EnumWindows(callback, 0)
    return windows
