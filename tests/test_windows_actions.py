"""The Windows halves of actions.py, driven on any platform.

Nothing here starts a process: `subprocess.Popen` is replaced by a recorder,
so what is checked is exactly what WOULD have been launched.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import actions


@pytest.fixture
def launched(monkeypatch):
    calls = []

    class _Popen:
        def __init__(self, argv, **kwargs):
            calls.append((argv, kwargs))

    monkeypatch.setattr(actions.sys, "platform", "win32")
    monkeypatch.setattr(actions.subprocess, "Popen", _Popen)
    monkeypatch.setattr(actions.subprocess, "CREATE_NEW_CONSOLE", 0x10,
                        raising=False)
    return calls


@pytest.mark.asyncio
async def test_the_directory_is_the_consoles_cwd_never_part_of_a_command(
        launched, tmp_path):
    """A path with spaces, `&` and a quote reaches the console untouched,
    because it is never put on a command line at all."""
    project = tmp_path / "it's a&b project"
    project.mkdir()
    result = await actions.open_terminal_at(str(project), "npm run dev")
    assert result["success"]
    (argv, kwargs), = launched
    assert argv == ["cmd.exe", "/k", "npm run dev"]
    assert kwargs["cwd"] == str(project)
    assert str(project) not in " ".join(argv)


@pytest.mark.asyncio
async def test_no_command_opens_a_bare_console(launched, tmp_path):
    assert (await actions.open_terminal_at(str(tmp_path)))["success"]
    (argv, kwargs), = launched
    assert argv == ["cmd.exe"] and kwargs["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_a_missing_directory_starts_nothing(launched, tmp_path):
    result = await actions.open_terminal_at(str(tmp_path / "gone"), "npm start")
    assert not result["success"] and launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "--remote-debugging-port=9222",            # a browser flag, not an address
    r"C:\Windows\System32\calc.exe",           # a program, not an address
    "javascript:alert(1)",
    "",
])
async def test_only_a_real_url_reaches_the_browser(launched, url):
    result = await actions.open_browser(url)
    assert not result["success"] and launched == []


@pytest.mark.asyncio
async def test_a_url_is_the_browsers_only_argument(launched, monkeypatch):
    monkeypatch.setattr(actions, "_windows_browser",
                        lambda name: ("Chrome", r"C:\chrome.exe"))
    result = await actions.open_browser("https://example.com/?q=a&b")
    assert result["success"]
    (argv, _kwargs), = launched
    assert argv == [r"C:\chrome.exe", "https://example.com/?q=a&b"]


@pytest.mark.asyncio
async def test_without_vs_code_a_file_is_only_ever_displayed(
        launched, monkeypatch, tmp_path):
    """Never the default app: on Windows that RUNS an .exe, .bat or .js."""
    monkeypatch.setattr(actions, "_vscode_command", lambda path: None)
    script = tmp_path / "setup.bat"
    script.write_text("calc")
    result = await actions.open_in_editor(str(script))
    assert result["success"] and result["editor"] == "Notepad"
    (argv, _kwargs), = launched
    assert argv == ["notepad.exe", str(script)]
