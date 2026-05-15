"""Security boundary tests for local-only JARVIS control APIs."""

import sys
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).parent.parent))

import server


def _request(host: str, headers: dict[str, str] | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers=headers or {},
    )


def test_loopback_hosts_are_trusted_without_token(monkeypatch):
    monkeypatch.setattr(server, "JARVIS_API_TOKEN", "")

    assert server._http_request_is_trusted(_request("127.0.0.1"))
    assert server._http_request_is_trusted(_request("::1"))
    assert server._http_request_is_trusted(_request("localhost"))


def test_remote_host_requires_valid_token(monkeypatch):
    monkeypatch.setattr(server, "JARVIS_API_TOKEN", "correct-token")

    assert not server._http_request_is_trusted(_request("192.168.1.44"))
    assert not server._http_request_is_trusted(
        _request("192.168.1.44", {"authorization": "Bearer wrong-token"})
    )
    assert server._http_request_is_trusted(
        _request("192.168.1.44", {"authorization": "Bearer correct-token"})
    )
    assert server._http_request_is_trusted(
        _request("192.168.1.44", {"x-jarvis-api-token": "correct-token"})
    )


def test_remote_host_is_rejected_when_token_is_unset(monkeypatch):
    monkeypatch.setattr(server, "JARVIS_API_TOKEN", "")

    assert not server._http_request_is_trusted(
        _request("203.0.113.10", {"authorization": "Bearer anything"})
    )


def test_env_writer_strips_newline_injection(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_example = tmp_path / ".env.example"
    env_file.write_text("USER_NAME=Tony\n")
    env_example.write_text("")

    monkeypatch.setattr(server, "_env_file_path", lambda: env_file)
    monkeypatch.setattr(server, "_env_example_path", lambda: env_example)

    server._write_env_key("USER_NAME", "Alice\nJARVIS_SKIP_PERMISSIONS=true")

    lines = env_file.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("USER_NAME=")
