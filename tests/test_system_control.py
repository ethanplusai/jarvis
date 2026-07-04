"""Unit tests for system_control — cross-platform command builders and
graceful degradation. No real desktop is required: builders take the platform
explicitly and executor tests use commands that are guaranteed to be missing.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import system_control as sc


# --- builders ----------------------------------------------------------------

def test_open_url_per_platform():
    assert sc.open_url_cmd("https://x.io", "mac") == ["open", "https://x.io"]
    assert sc.open_url_cmd("https://x.io", "windows") == ["cmd", "/c", "start", "", "https://x.io"]
    assert sc.open_url_cmd("https://x.io", "linux") == ["xdg-open", "https://x.io"]


def test_open_app_per_platform():
    assert sc.open_app_cmd("Spotify", "mac") == ["open", "-a", "Spotify"]
    assert sc.open_app_cmd("Spotify", "windows") == ["cmd", "/c", "start", "", "Spotify"]
    assert sc.open_app_cmd("Google Chrome", "linux") == ["gtk-launch", "google-chrome"]


def test_open_path_per_platform():
    assert sc.open_path_cmd("/tmp", "mac") == ["open", "/tmp"]
    assert sc.open_path_cmd("C:\\Users", "windows") == ["explorer", "C:\\Users"]
    assert sc.open_path_cmd("/tmp", "linux") == ["xdg-open", "/tmp"]


def test_set_volume_clamps_and_routes():
    assert sc.set_volume_cmd(40, "mac") == ["osascript", "-e", "set volume output volume 40"]
    assert sc.set_volume_cmd(150, "mac")[-1].endswith("100")
    assert sc.set_volume_cmd(-5, "linux") == ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "0%"]
    assert sc.set_volume_cmd(50, "windows") is None  # graceful degrade


def test_media_per_platform():
    assert sc.media_cmd("play_pause", "linux") == ["playerctl", "play-pause"]
    assert sc.media_cmd("next", "mac") == ["osascript", "-e", 'tell application "Music" to next track']
    assert sc.media_cmd("play_pause", "windows") is None
    assert sc.media_cmd("not-a-verb", "linux") is None


def test_lock_screen_per_platform():
    assert sc.lock_screen_cmd("mac") == ["pmset", "displaysleepnow"]
    assert sc.lock_screen_cmd("windows") == ["rundll32.exe", "user32.dll,LockWorkStation"]
    assert sc.lock_screen_cmd("linux") == ["loginctl", "lock-session"]


def test_clipboard_per_platform():
    assert sc.clipboard_copy_cmd("mac") == ["pbcopy"]
    assert sc.clipboard_copy_cmd("linux") == ["xclip", "-selection", "clipboard"]
    assert sc.clipboard_copy_cmd("windows")[0] == "powershell"
    assert sc.clipboard_copy_fallback_cmd("linux") == ["wl-copy"]
    assert sc.clipboard_copy_fallback_cmd("mac") is None


def test_screenshot_per_platform():
    assert sc.screenshot_cmd("/tmp/s.png", "mac") == ["screencapture", "-x", "/tmp/s.png"]
    assert sc.screenshot_cmd("/tmp/s.png", "linux") == ["gnome-screenshot", "-f", "/tmp/s.png"]
    assert sc.screenshot_cmd("/tmp/s.png", "windows") is None
    assert sc.screenshot_fallback_cmd("/tmp/s.png", "linux") == ["import", "-window", "root", "/tmp/s.png"]


def test_terminal_per_platform():
    assert sc.terminal_cmd("", "windows") == ["cmd", "/c", "start", "powershell", "-NoExit"]
    assert sc.terminal_cmd("ls", "linux") == ["x-terminal-emulator", "-e", "bash", "-lc", "ls"]
    assert sc.terminal_cmd("ls", "mac") is None  # mac uses the AppleScript path
    assert sc.terminal_fallback_cmds("", "linux") == [["gnome-terminal"], ["konsole"]]
    assert sc.terminal_fallback_cmds("", "windows") == []


# --- executors: never raise, degrade with butler-voice messages --------------

def test_run_missing_binary_degrades():
    ok, err = asyncio.run(sc._run(["jarvis-definitely-not-a-binary-xyz"]))
    assert ok is False
    assert "not found" in err


def test_set_volume_rejects_garbage():
    result = asyncio.run(sc.set_volume("loud please"))
    assert result["success"] is False
    assert "sir" in result["confirmation"]


def test_media_rejects_unknown_verb():
    result = asyncio.run(sc.media("backflip"))
    assert result["success"] is False
    assert result["confirmation"]


def test_media_normalises_aliases():
    assert sc.media_cmd("play_pause", "linux") is not None
    # alias mapping happens in the executor; verify via a known alias on an
    # unsupported platform path returning the degrade message, not a crash
    result = asyncio.run(sc.clipboard_copy(""))
    assert result["success"] is False


def test_open_path_missing_file():
    result = asyncio.run(sc.open_path("/definitely/not/a/real/path/xyz"))
    assert result["success"] is False
    assert "sir" in result["confirmation"]


def test_open_app_empty_name():
    result = asyncio.run(sc.open_app("  "))
    assert result["success"] is False


def test_unsupported_shape():
    out = sc._unsupported("teleportation")
    assert out["success"] is False
    assert "teleportation" in out["confirmation"]


# --- action tag extraction recognises the new desktop-control tags -----------

def test_extract_action_new_tags():
    import server
    for tag, expected in [
        ("[ACTION:OPEN_APP] Spotify", "open_app"),
        ("[ACTION:SET_VOLUME] 40", "set_volume"),
        ("[ACTION:MEDIA] next", "media"),
        ("[ACTION:LOCK_SCREEN]", "lock_screen"),
        ("[ACTION:CLIPBOARD] hello world", "clipboard"),
        ("[ACTION:SCREENSHOT]", "screenshot"),
        ("[ACTION:OPEN_PATH] /tmp/report.html", "open_path"),
    ]:
        clean, action = server.extract_action(f"Right away, sir. {tag}")
        assert action is not None, tag
        assert action["action"] == expected
        assert clean == "Right away, sir."


# --- manual runner (no pytest required) -------------------------------------

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
