"""_write_mcp_config must point the brain's MCP child at a URL that actually
reaches the running server — not a hardcoded 127.0.0.1:8340/http guess.

The real server's scheme/host/port are recorded into the environment by
main() (JARVIS_SCHEME / JARVIS_BIND_HOST / JARVIS_PORT) right before
uvicorn.run, because CLAUDE.md's own quick-start has users generate a
self-signed cert (which flips the server to HTTPS) and the project's own
documented test invocation uses --host ::1 --port 8341.
"""

import json

import pytest


@pytest.fixture
def server_module(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import importlib
    import server as server_module
    importlib.reload(server_module)
    return server_module


@pytest.mark.parametrize("scheme,bind_host,port,expected_host", [
    ("http", "0.0.0.0", 8340, "127.0.0.1"),
    ("https", "0.0.0.0", 8340, "127.0.0.1"),
    ("http", "::1", 8341, "[::1]"),
    ("https", "::1", 8341, "[::1]"),
])
def test_mcp_config_url_matches_the_real_bind_params(
        server_module, monkeypatch, tmp_path, scheme, bind_host, port, expected_host):
    monkeypatch.setenv("JARVIS_SCHEME", scheme)
    monkeypatch.setenv("JARVIS_BIND_HOST", bind_host)
    monkeypatch.setenv("JARVIS_PORT", str(port))

    home = tmp_path / "home"
    home.mkdir()
    path = server_module._write_mcp_config(home)
    config = json.loads(path.read_text(encoding="utf-8"))
    url = config["mcpServers"]["jarvis"]["env"]["JARVIS_TOOL_URL"]

    assert url == f"{scheme}://{expected_host}:{port}/internal/tool"


def test_mcp_config_defaults_when_env_is_unset(server_module, monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_SCHEME", raising=False)
    monkeypatch.delenv("JARVIS_BIND_HOST", raising=False)
    monkeypatch.delenv("JARVIS_PORT", raising=False)

    home = tmp_path / "home"
    home.mkdir()
    path = server_module._write_mcp_config(home)
    config = json.loads(path.read_text(encoding="utf-8"))
    url = config["mcpServers"]["jarvis"]["env"]["JARVIS_TOOL_URL"]

    assert url == "http://127.0.0.1:8340/internal/tool"
