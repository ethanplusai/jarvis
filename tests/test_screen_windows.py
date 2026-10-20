"""screen.py on Windows: `screen_windows`, the user32/gdi32 half.

Nothing here captures the real screen or reads the real window list unless
marked `browser` (deselected by default, like the macOS half's live
osascript test). The pure parts — the PNG encoder, the display numbering,
the sizing, the blank check — are tested on synthetic pixels, and the GDI
grab is replaced where a capture's handling is what is under test.
"""

import asyncio
import struct
import sys
import zlib

import pytest

if sys.platform != "win32":
    pytest.skip("user32/gdi32 are Windows'", allow_module_level=True)

import screen
import screen_windows as sw


def _bgra(width, height, pixel):
    return bytes(pixel) * (width * height)


def _busy_bgra(width, height):
    return bytes((x * 7 + y * 13) % 256 for y in range(height) for x in range(width)
                 for _ in range(4))


def _decode_png(png):
    """(width, height, rgb rows) from a filter-0 truecolour PNG."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    i, idat, size = 8, b"", None
    while i < len(png):
        length = struct.unpack(">I", png[i:i + 4])[0]
        tag, data = png[i + 4:i + 8], png[i + 8:i + 8 + length]
        crc = struct.unpack(">I", png[i + 8 + length:i + 12 + length])[0]
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF
        if tag == b"IHDR":
            size = struct.unpack(">II", data[:8])
            assert data[8:] == bytes([8, 2, 0, 0, 0])      # 8-bit RGB, no interlace
        elif tag == b"IDAT":
            idat += data
        i += 12 + length
    width, height = size
    raw = zlib.decompress(idat)
    row = width * 3 + 1
    rows = [raw[y * row:(y + 1) * row] for y in range(height)]
    assert all(r[0] == 0 for r in rows)
    return width, height, [r[1:] for r in rows]


# --- the encoder ------------------------------------------------------------

def test_the_png_is_valid_and_puts_red_where_red_was():
    # BGRA in, RGB out: one blue pixel then one red one, on two rows.
    bgra = bytes([255, 0, 0, 255, 0, 0, 255, 255]) * 2
    png = sw.encode_png(2, 2, bgra)
    assert screen._png_size(png) == (2, 2)
    width, height, rows = _decode_png(png)
    assert (width, height) == (2, 2)
    assert rows[0] == bytes([0, 0, 255, 255, 0, 0])


def test_a_full_size_screen_encodes_within_the_tool_channel_bound():
    png = sw.encode_png(1280, 720, _busy_bgra(1280, 720))
    assert screen._png_size(png) == (1280, 720)
    assert len(png) < screen.MAX_SHOT_BYTES


# --- sizing and numbering ---------------------------------------------------

@pytest.mark.parametrize("size, expected", [
    ((1920, 1080), (1280, 720)),
    ((3840, 2160), (1280, 720)),
    ((1080, 1920), (720, 1280)),      # portrait: the long edge is the height
    ((1024, 768), (1024, 768)),       # already small enough: drawn as is
])
def test_the_longest_edge_is_brought_to_the_budget(size, expected):
    assert sw.fit(*size) == expected


def test_the_primary_display_is_one_and_the_rest_go_left_to_right():
    left, right, primary = (-1920, 0, 0, 1080), (2560, 0, 4480, 1080), (0, 0, 2560, 1440)
    assert sw.order_displays([(right, False), (primary, True), (left, False)]) == [
        primary, left, right]


def _one_display(monkeypatch, rect=(0, 0, 1920, 1080)):
    monkeypatch.setattr(sw, "_displays", lambda: [rect])


def test_a_display_that_is_not_there_is_said_not_guessed(monkeypatch):
    _one_display(monkeypatch)
    with pytest.raises(screen.ScreenError, match="only see 1 display,"):
        sw.capture_screen(display=2)


def test_display_one_is_the_default(monkeypatch):
    _one_display(monkeypatch)
    seen = []
    monkeypatch.setattr(sw, "_grab", lambda rect, w, h: seen.append((rect, w, h))
                        or _busy_bgra(w, h))
    shot = sw.capture_screen()
    assert seen == [((0, 0, 1920, 1080), 1280, 720)]
    assert (shot.width, shot.height) == (1280, 720)
    assert screen._png_size(shot.png) == (1280, 720)


# --- the blank frame: a locked desk or a UAC prompt captures as black --------

def test_a_black_frame_is_refused_rather_than_described(monkeypatch):
    _one_display(monkeypatch)
    monkeypatch.setattr(sw, "_grab", lambda rect, w, h: _bgra(w, h, (0, 0, 0, 255)))
    with pytest.raises(screen.ScreenError, match="blank"):
        sw.capture_screen()


def test_one_flat_colour_is_blank_too(monkeypatch):
    _one_display(monkeypatch)
    monkeypatch.setattr(sw, "_grab", lambda rect, w, h: _bgra(w, h, (24, 62, 44, 255)))
    with pytest.raises(screen.ScreenError, match="blank"):
        sw.capture_screen()


def test_the_sample_reads_blue_green_red_in_that_order():
    pixels = sw.sample_pixels(4, 4, _bgra(4, 4, (10, 20, 30, 255)), edge=2)
    assert pixels == [(10, 20, 30)] * 4


# --- names ------------------------------------------------------------------

def test_an_exe_with_no_description_is_called_by_its_file_name(tmp_path):
    exe = tmp_path / "Thing.exe"
    exe.write_bytes(b"not a real program")
    assert sw.app_name(str(exe)) == "Thing"
    assert sw.app_name("") == ""


def test_a_real_exe_is_called_by_the_name_it_gives_itself():
    import os
    notepad = os.path.join(os.environ["SystemRoot"], "System32", "notepad.exe")
    assert sw.app_name(notepad) == "Notepad"


# --- screen.py hands Windows to screen_windows ------------------------------

@pytest.mark.asyncio
async def test_screen_capture_is_routed_here_and_runs_off_the_loop(monkeypatch):
    import threading
    main = threading.get_ident()
    seen = {}

    def fake(display):
        seen["display"], seen["thread"] = display, threading.get_ident()
        return screen.Shot(png=b"png", width=1, height=1)

    monkeypatch.setattr(sw, "capture_screen", fake)
    shot = await asyncio.wait_for(screen.capture_screen(display=2), 5)
    assert shot.png == b"png" and seen["display"] == 2 and seen["thread"] != main


@pytest.mark.asyncio
async def test_the_window_list_is_routed_here(monkeypatch):
    monkeypatch.setattr(sw, "list_windows",
                        lambda: [screen.Window(app="Notepad", title="notes", frontmost=True)])
    windows = await asyncio.wait_for(screen.list_windows(), 5)
    assert [(w.app, w.title, w.frontmost) for w in windows] == [("Notepad", "notes", True)]


# --- the real thing, deliberately ------------------------------------------

@pytest.mark.browser
def test_the_real_screen_comes_back_as_a_png_within_budget():
    try:
        shot = sw.capture_screen()
    except screen.ScreenError as e:
        if "blank" in str(e):
            pytest.skip("the desk is locked or a security prompt is up")
        raise
    assert screen._png_size(shot.png) == (shot.width, shot.height)
    assert max(shot.width, shot.height) <= screen.SHOT_MAX_EDGE


@pytest.mark.browser
def test_the_real_window_list_is_bounded_titled_and_free_of_the_shell():
    windows = sw.list_windows()
    assert len(windows) <= screen.MAX_WINDOWS
    assert all(w.app and w.title for w in windows)
    assert not any(w.title == "Program Manager" for w in windows)
    assert sum(1 for w in windows if w.frontmost) <= len(windows)
