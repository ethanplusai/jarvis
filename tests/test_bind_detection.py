"""The server has to know where it is actually listening — however it started.

`main()` recorded `JARVIS_PORT` / `JARVIS_BIND_HOST` / `JARVIS_SCHEME` into
the environment right before `uvicorn.run`, and three things read them: the
origin allowlist, the `Host` allowlist, and the URL the brain's MCP child
dials. So `python server.py --port 9000` worked and

    uvicorn server:app --port 9000

did not — nothing set those variables, so the allowlist was still built for
8340, and the operator's own browser at `http://localhost:9000` was refused
by JARVIS's own guard. Same for `uvicorn server:app` on uvicorn's default
port 8000, and same for the `Host` header when the operator named a host to
bind to. The `--host 0.0.0.0` warning had the same shape: it was printed
from `__main__`, so the one launch path that most needed it never saw it.

Detection therefore lives where the app is, not where `main()` is. It
cannot ask uvicorn — uvicorn runs the lifespan *before* it binds its
sockets, so there is no socket to interrogate at that point — so it reads
what it was launched with: the environment first (which is what `main()`
sets, and what an operator can set), then the command line, then the
launcher's own documented defaults. Anything it cannot work out is said out
loud rather than guessed at silently.
"""

import importlib
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import web_auth

_BIND_VARS = ("JARVIS_BIND_HOST", "JARVIS_PORT", "JARVIS_SCHEME",
              "JARVIS_ALLOWED_ORIGINS")


@pytest.fixture(autouse=True)
def clean_env():
    """Save and restore by hand rather than with monkeypatch.

    `monkeypatch.delenv(name, raising=False)` on a variable that is not set
    records nothing to undo, so anything the test then *creates* survives
    teardown. These tests exist to make the app write exactly these
    variables, so that leak is not hypothetical: it escaped into the rest of
    the session and moved another module's idea of the API port.
    """
    saved = {name: os.environ.get(name) for name in _BIND_VARS}
    for name in _BIND_VARS:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# -- reading the launch, whichever launch it was --------------------------


def test_main_still_wins_because_it_sets_the_environment():
    """`python server.py` records its parsed arguments. Nothing may override
    that: it is the one launcher that knows for certain."""
    bind = web_auth.detect_bind(
        argv=["server.py", "--host", "127.0.0.1", "--port", "8340"],
        environ={"JARVIS_BIND_HOST": "::1", "JARVIS_PORT": "9999",
                 "JARVIS_SCHEME": "https"})
    assert bind.host == "::1"
    assert bind.port == 9999
    assert bind.scheme == "https"
    assert bind.source == "environment"


def test_the_uvicorn_command_line_is_read():
    bind = web_auth.detect_bind(
        argv=["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"],
        environ={})
    assert (bind.host, bind.port) == ("0.0.0.0", 9000)
    assert bind.source == "command line"


def test_the_equals_form_is_read_too():
    bind = web_auth.detect_bind(
        argv=["uvicorn", "server:app", "--host=0.0.0.0", "--port=9000"],
        environ={})
    assert (bind.host, bind.port) == ("0.0.0.0", 9000)


def test_uvicorn_with_no_flags_gets_uvicorns_own_defaults():
    """`uvicorn server:app` listens on 127.0.0.1:8000, not JARVIS's 8340 —
    which is exactly why the browser was refused."""
    bind = web_auth.detect_bind(argv=["uvicorn", "server:app"], environ={})
    assert (bind.host, bind.port) == ("127.0.0.1", 8000)
    assert bind.source == "uvicorn default"


def test_running_the_script_with_no_flags_gets_jarvis_defaults():
    bind = web_auth.detect_bind(argv=["server.py"], environ={})
    assert (bind.host, bind.port) == ("127.0.0.1", 8340)
    assert bind.source == "default"


def test_module_launcher_and_windows_executable_defaults(monkeypatch):
    for args in (["python", "-m", "uvicorn", "server:app"],
                 [r"C:\venv\Scripts\uvicorn.exe", "server:app"]):
        bind = web_auth.detect_bind(argv=args, environ={})
        assert bind.port == 8000 and bind.source == "uvicorn default"
    monkeypatch.setattr(web_auth.sys, "argv", ["__main__.py", "server:app"])
    monkeypatch.setattr(web_auth.sys, "orig_argv", ["python", "-m", "uvicorn", "server:app"])
    assert web_auth.detect_bind(environ={}).port == 8000


def test_a_partial_command_line_fills_in_the_rest():
    bind = web_auth.detect_bind(
        argv=["uvicorn", "server:app", "--host", "0.0.0.0"], environ={})
    assert (bind.host, bind.port) == ("0.0.0.0", 8000)


def test_ssl_flags_make_the_scheme_https():
    bind = web_auth.detect_bind(
        argv=["uvicorn", "server:app", "--ssl-keyfile", "key.pem",
              "--ssl-certfile", "cert.pem"], environ={})
    assert bind.scheme == "https"


def test_a_port_that_is_not_a_number_does_not_raise():
    bind = web_auth.detect_bind(
        argv=["uvicorn", "server:app", "--port", "not-a-port"], environ={})
    assert bind.port == 8000


@pytest.mark.parametrize("flag", ["--uds", "--fd"])
def test_a_socket_launch_says_it_does_not_know(flag):
    """`--uds /tmp/x.sock` is not an address this can reason about, and
    guessing 127.0.0.1:8000 would silently build the wrong allowlist."""
    bind = web_auth.detect_bind(
        argv=["uvicorn", "server:app", flag, "3"], environ={})
    assert bind.source == "unknown"


# -- what "loopback" means ------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost",
                                  "127.0.0.5"])
def test_these_are_loopback(host):
    assert web_auth.is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10",
                                  "jarvis.example.com", ""])
def test_these_are_not(host):
    assert not web_auth.is_loopback(host)


# -- the warning ----------------------------------------------------------


def test_a_wide_bind_produces_a_warning():
    lines = web_auth.exposure_warning(
        web_auth.Bind("0.0.0.0", 8340, "http", "command line"))
    assert lines
    text = " ".join(lines)
    assert "0.0.0.0" in text
    assert "JARVIS_ALLOWED_ORIGINS" in text, (
        "an operator who means it must be told the one thing they have to "
        "set, or their own browser is refused")


def test_a_loopback_bind_produces_nothing():
    assert web_auth.exposure_warning(
        web_auth.Bind("127.0.0.1", 8340, "http", "default")) == []


def test_an_unknown_bind_says_so_rather_than_claiming_safety():
    lines = web_auth.exposure_warning(
        web_auth.Bind("127.0.0.1", 8000, "http", "unknown"))
    assert lines and "could not" in " ".join(lines).lower()


# -- adopting it ----------------------------------------------------------


def test_adopting_a_bind_teaches_the_origin_allowlist_the_real_port():
    web_auth.adopt_bind(web_auth.Bind("127.0.0.1", 9000, "http",
                                      "command line"))
    assert web_auth.api_port() == 9000
    assert web_auth.origin_allowed("http://localhost:9000")
    assert not web_auth.origin_allowed("http://localhost:8340")


def test_adopting_a_bind_teaches_the_host_allowlist_the_real_name():
    web_auth.adopt_bind(web_auth.Bind("jarvis.example.com", 8340, "http",
                                      "command line"))
    assert web_auth.host_allowed("jarvis.example.com")


def test_adopting_never_overwrites_what_main_already_recorded(monkeypatch):
    monkeypatch.setenv("JARVIS_PORT", "8340")
    monkeypatch.setenv("JARVIS_BIND_HOST", "127.0.0.1")
    web_auth.adopt_bind(web_auth.Bind("0.0.0.0", 9999, "http", "command line"))
    assert os.environ["JARVIS_PORT"] == "8340"
    assert os.environ["JARVIS_BIND_HOST"] == "127.0.0.1"


def test_an_unknown_bind_is_not_adopted():
    """Guessing would build an allowlist for a port nothing is listening on
    and quietly lock the operator out of their own dashboard."""
    web_auth.adopt_bind(web_auth.Bind("127.0.0.1", 8000, "http", "unknown"))
    assert "JARVIS_PORT" not in os.environ
    assert "JARVIS_BIND_HOST" not in os.environ


# -- and the whole thing, through the app's own startup -------------------


@pytest.fixture
def reloaded(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    return server


def _start(server, monkeypatch, argv):
    """Run the lifespan's startup half with a pretend command line."""
    import asyncio

    monkeypatch.setattr(sys, "argv", argv)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(server, "start_brain_and_speech", nothing)
    monkeypatch.setattr(server, "start_session_watcher", nothing)
    monkeypatch.setattr(server, "stop_session_watcher", nothing)
    monkeypatch.setattr(server, "stop_brain_and_speech", nothing)
    monkeypatch.setattr(server, "_run_preflight", nothing)

    async def run():
        ctx = server.lifespan(server.app)
        await ctx.__aenter__()
        await ctx.__aexit__(None, None, None)

    asyncio.run(run())


def test_a_uvicorn_launch_teaches_the_guard_its_real_port(reloaded,
                                                          monkeypatch):
    """The whole point: `uvicorn server:app --port 9000` and the browser at
    `http://localhost:9000` is JARVIS's own page, not a stranger."""
    server = reloaded
    assert not web_auth.origin_allowed("http://localhost:9000")
    _start(server, monkeypatch, ["uvicorn", "server:app", "--port", "9000"])
    assert web_auth.origin_allowed("http://localhost:9000")


def test_a_uvicorn_launch_teaches_the_guard_its_real_host(reloaded,
                                                          monkeypatch):
    server = reloaded
    assert not web_auth.host_allowed("jarvis.example.com")
    _start(server, monkeypatch,
           ["uvicorn", "server:app", "--host", "jarvis.example.com"])
    assert web_auth.host_allowed("jarvis.example.com")


def test_the_wide_bind_warning_is_reached_from_a_uvicorn_launch(reloaded,
                                                                monkeypatch,
                                                                caplog):
    """It used to print only from `__main__`, so the one launch path that
    most needed it never saw it."""
    server = reloaded
    with caplog.at_level(logging.WARNING):
        _start(server, monkeypatch,
               ["uvicorn", "server:app", "--host", "0.0.0.0"])
    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.WARNING)
    assert "0.0.0.0" in warned, caplog.text
    assert "JARVIS_ALLOWED_ORIGINS" in warned, caplog.text


def test_a_loopback_launch_says_nothing_alarming(reloaded, monkeypatch,
                                                 caplog):
    server = reloaded
    with caplog.at_level(logging.WARNING):
        _start(server, monkeypatch, ["uvicorn", "server:app"])
    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.WARNING)
    assert "0.0.0.0" not in warned


def test_the_script_launch_still_warns_too(reloaded, monkeypatch, caplog):
    """`python server.py --host 0.0.0.0` records its arguments in the
    environment before uvicorn starts, so lifespan sees them there. Moving
    the warning must not have lost the launch path that already had it."""
    server = reloaded
    monkeypatch.setenv("JARVIS_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("JARVIS_PORT", "8340")
    with caplog.at_level(logging.WARNING):
        _start(server, monkeypatch, ["server.py", "--host", "0.0.0.0"])
    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.WARNING)
    assert "0.0.0.0" in warned, caplog.text


def test_a_socket_launch_is_told_it_is_on_its_own(reloaded, monkeypatch,
                                                  caplog):
    """`uvicorn server:app --uds ...` is a launch this cannot read. Saying
    so is the whole difference between a documented limitation and a silent
    one."""
    server = reloaded
    with caplog.at_level(logging.WARNING):
        _start(server, monkeypatch,
               ["uvicorn", "server:app", "--uds", "/tmp/jarvis.sock"])
    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.WARNING)
    assert "could not" in warned.lower(), caplog.text
    # And it published nothing, so the existing defaults still stand.
    assert "JARVIS_PORT" not in os.environ
