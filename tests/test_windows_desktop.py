"""Windows adapter boundaries: no real windows, notifications, or input."""
import base64
import json
import pytest
import windows_desktop
import windows_console


@pytest.mark.asyncio
async def test_powershell_keeps_untrusted_values_out_of_script(monkeypatch):
    calls = []
    payload = {"title": "世界 '; Stop-Process -Id 1; #"}
    class Child:
        returncode = 0
        async def communicate(self, data):
            assert json.loads(data) == payload
            return "世界".encode(), b""
    async def spawn(*args, **kwargs):
        calls.append(args)
        return Child()
    monkeypatch.setattr(windows_desktop.asyncio, "create_subprocess_exec", spawn)
    assert await windows_desktop.powershell("$p.title", payload) == "世界"
    source = base64.b64decode(calls[0][-1]).decode("utf-16-le")
    assert payload["title"] not in source
    assert "UTF8Encoding" in source
    assert "-NonInteractive" in calls[0]


@pytest.mark.asyncio
async def test_powershell_does_not_hand_down_a_powershell_7_module_path(monkeypatch):
    """Started from a pwsh terminal, the backend carries 7's PSModulePath;
    5.1 given it loads 7's module manifests, and Get-Acl cannot load at
    all. Everything else is still inherited."""
    seen = []
    class Child:
        returncode = 0
        async def communicate(self, data):
            return b"ok", b""
    async def spawn(*args, **kwargs):
        seen.append(kwargs)
        return Child()
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("JARVIS_PROBE", "kept")
    monkeypatch.setattr(windows_desktop.asyncio, "create_subprocess_exec", spawn)
    await windows_desktop.powershell("$p")
    env = seen[0]["env"]
    assert not [k for k in env if k.upper() == "PSMODULEPATH"]
    assert env["JARVIS_PROBE"] == "kept"


@pytest.mark.asyncio
async def test_browser_rejects_executable_schemes_before_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Must not launch a process")
    monkeypatch.setattr(windows_desktop.subprocess, "Popen", forbidden)
    for url in ("javascript:alert(1)", "cmd:calc", "powershell.exe"):
        with pytest.raises(ValueError):
            await windows_desktop.open_browser(url, "edge")


@pytest.mark.asyncio
async def test_browser_launches_exact_url_argument(monkeypatch):
    calls = []
    monkeypatch.setattr(windows_desktop, "browser_binary", lambda name: "C:/browser.exe")
    monkeypatch.setattr(windows_desktop.subprocess, "Popen", lambda args, **kw: calls.append(args))
    url = "https://example.test/?q=a&x=';calc"
    await windows_desktop.open_browser(url, "edge")
    assert calls == [["C:/browser.exe", url]]


def test_console_refuses_unbounded_input_before_native_calls():
    assert windows_console.operate(123, "yes\n") == {"status": "bad_key"}
    assert windows_console.operate(-1) == {"status": "no_tty"}


def test_windows_directory_command_quotes_literal_paths(monkeypatch):
    import actions
    monkeypatch.setattr(actions, "_windows", True)
    assert actions.directory_command("C:\\Ada's work;$x") == "Set-Location -LiteralPath 'C:\\Ada''s work;$x'"
    assert actions.project_command("C:\\work", "npm run dev") == "Set-Location -LiteralPath 'C:\\work'; if ($?) { npm run dev }"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Winsock transport")
def test_native_inbox_delivers_bytes_to_isolated_socket(tmp_path):
    import ctypes as c
    import os
    import windows_inbox
    ws = c.WinDLL("ws2_32")
    handle = c.c_size_t
    ws.socket.argtypes = [c.c_int, c.c_int, c.c_int]
    ws.socket.restype = handle
    ws.bind.argtypes = [handle, c.c_void_p, c.c_int]
    ws.listen.argtypes = [handle, c.c_int]
    ws.accept.argtypes = [handle, c.c_void_p, c.c_void_p]
    ws.accept.restype = handle
    ws.recv.argtypes = [handle, c.c_void_p, c.c_int, c.c_int]
    ws.closesocket.argtypes = [handle]
    class Address(c.Structure):
        _fields_ = [("family", c.c_ushort), ("path", c.c_char * 108)]
    path = tmp_path / "inbox.sock"
    encoded = os.fsencode(path)
    if len(encoded) >= 108:
        pytest.skip("Temporary socket path exceeds Winsock limit")
    assert ws.WSAStartup(0x202, c.create_string_buffer(512)) == 0
    listener = ws.socket(1, 1, 0)
    client = handle(-1).value
    try:
        assert listener != handle(-1).value
        address = Address(1, encoded)
        assert ws.bind(listener, c.byref(address), c.sizeof(address)) == 0
        assert ws.listen(listener, 1) == 0
        payload = 'hello 世界\n'.encode()
        windows_inbox.send(str(path), payload, timeout=1)
        client = ws.accept(listener, None, None)
        assert client != handle(-1).value
        buffer = c.create_string_buffer(256)
        count = ws.recv(client, buffer, len(buffer), 0)
        assert buffer.raw[:count] == payload
    finally:
        if client != handle(-1).value:
            ws.closesocket(client)
        ws.closesocket(listener)
        ws.WSACleanup()
        path.unlink(missing_ok=True)



@pytest.mark.asyncio
async def test_terminal_runs_with_the_environment_it_is_given(monkeypatch):
    """A terminal JARVIS opens for `claude` is given a scrubbed environment;
    one opened for anything else inherits, as before."""
    calls = []
    monkeypatch.setattr(windows_desktop.subprocess, "Popen",
                        lambda args, **kw: calls.append((args, kw)))
    await windows_desktop.open_terminal("claude", env={"PATH": "p"})
    await windows_desktop.open_terminal("dir")
    assert calls[0][1]["env"] == {"PATH": "p"}
    assert calls[1][1].get("env") is None


@pytest.mark.asyncio
async def test_actions_hands_the_environment_to_the_windows_terminal(monkeypatch):
    import actions
    seen = []

    async def fake(command="", env=None):
        seen.append((command, env))

    monkeypatch.setattr(actions, "_windows", True)
    monkeypatch.setattr(windows_desktop, "open_terminal", fake)
    result = await actions.open_terminal("claude", env={"PATH": "p"})
    assert result["success"] is True
    assert seen == [("claude", {"PATH": "p"})]
