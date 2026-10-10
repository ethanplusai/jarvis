"""Opening the result: a browser, or a terminal.

`actions.py` has been able to do this since the first version — it was never
wired to the tool-based brain, so JARVIS could build a site and then not show
it to anybody. The user's words: "he will need to be able to read output from
sessions to be able to open up results in browser."

The dangerous half is `file://`. A target is text an LLM wrote, possibly
echoing something a spawned run said, so an absolute path is never opened on
trust — it is resolved and proved to sit inside a directory JARVIS already
knows as a project.

NOTHING in this file may launch a browser or a terminal: `actions` is mocked
at the module boundary, and the recorder asserts what it was handed.
"""

import importlib

import pytest


class _Actions:
    @staticmethod
    def directory_command(path):
        import shlex
        return f"cd {shlex.quote(path)}"
    """Stands in for actions.py. Records, never launches."""

    def __init__(self, success=True):
        self.browser: list[str] = []
        self.browsers: list[str] = []       # which application each went to
        self.terminal: list[str] = []
        self.success = success

    async def open_browser(self, url, browser="chrome"):
        self.browser.append(url)
        self.browsers.append(browser)
        # Mirrors the real actions.open_browser, which names the application
        # it actually drove.
        app = "Firefox" if browser == "firefox" else "Chrome"
        return {"success": self.success,
                "confirmation": f"Pulled that up in {app}, sir."
                if self.success else f"{app} ran into a problem, sir."}

    async def open_terminal(self, command=""):
        self.terminal.append(command)
        return {"success": self.success,
                "confirmation": "Terminal is open, sir."
                if self.success else "I had trouble opening Terminal, sir."}


@pytest.fixture
def ready(monkeypatch, tmp_path):
    """A server with one project on disk and a recording `actions`."""
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_BRAIN_AUTOSTART", "0")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server as server_module
    importlib.reload(server_module)
    run_store.init_db()

    project = tmp_path / "tony-starks-website"
    project.mkdir()
    (project / "index.html").write_text("<h1>Stark</h1>", encoding="utf-8")
    (project / "styles.css").write_text("body{}", encoding="utf-8")

    fake = _Actions()
    monkeypatch.setattr(server_module, "actions", fake)
    monkeypatch.setattr(server_module, "cached_projects",
                        [{"name": "tony-starks-website", "path": str(project)}])
    return server_module, fake, project


# --- registered in all three places ---------------------------------------

def test_the_three_tool_sets_still_agree(ready):
    import brain
    import jarvis_mcp
    server, _fake, _project = ready
    for name in ("open_in_browser", "open_in_terminal"):
        assert name in server.TOOL_HANDLERS
        assert name in server.ACTING_TOOLS, "it acts; the origin gate applies"
        assert f"mcp__jarvis__{name}" in brain.ALLOWED_TOOLS
    assert {t["name"] for t in jarvis_mcp.TOOL_SPECS} == set(server.TOOL_HANDLERS)


@pytest.mark.asyncio
async def test_a_watcher_turn_cannot_open_anything(ready, monkeypatch):
    """A line in somebody else's transcript must not put a window on the
    user's screen."""
    from fastapi.testclient import TestClient
    server, fake, _project = ready

    class _Brain:
        current_origin = "session_event"

    monkeypatch.setattr(server, "brain_instance", _Brain())
    import data_paths
    token = data_paths.ensure_tool_token()
    with TestClient(server.app) as client:
        r = client.post("/internal/tool",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"tool": "open_in_browser",
                              "arguments": {"target": "https://example.com"}})
    assert r.json()["ok"] is False
    assert fake.browser == []


# --- URLs -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_url_is_opened(ready):
    server, fake, _project = ready
    out = await server.tool_open_in_browser({"target": "https://example.com/x"})
    assert fake.browser == ["https://example.com/x"]
    assert "Chrome" in out


@pytest.mark.parametrize("target", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<script>fetch('/')</script>",
    "ftp://elsewhere/x",
])
@pytest.mark.asyncio
async def test_only_web_addresses_are_opened_as_addresses(ready, target):
    server, fake, _project = ready
    out = await server.tool_open_in_browser({"target": target})
    assert fake.browser == []
    assert "left alone" in out


# --- a file inside a project ----------------------------------------------

@pytest.mark.asyncio
async def test_a_bare_filename_resolves_against_the_named_project(ready):
    server, fake, project = ready
    out = await server.tool_open_in_browser(
        {"target": "index.html", "project": "tony-starks-website"})
    assert fake.browser == [(project / "index.html").as_uri()]
    assert "index.html" in out and "tony-starks-website" in out


@pytest.mark.asyncio
async def test_a_bare_filename_resolves_against_the_run_just_started(ready):
    """"open index.html" straight after "build me a site" must mean the site
    JARVIS just built, and nothing else on the machine."""
    server, fake, project = ready
    run_id = server.run_store.create_run("build", "tony-starks-website",
                                         str(project), "voice")
    server.last_started_run = run_id

    await server.tool_open_in_browser({"target": "index.html"})

    assert fake.browser == [(project / "index.html").as_uri()]


@pytest.mark.asyncio
async def test_a_path_that_names_its_own_project_works(ready):
    server, fake, project = ready
    await server.tool_open_in_browser(
        {"target": "tony-starks-website/styles.css"})
    assert fake.browser == [(project / "styles.css").as_uri()]


@pytest.mark.asyncio
async def test_a_directory_opens_its_index(ready):
    server, fake, project = ready
    await server.tool_open_in_browser({"target": str(project)})
    assert fake.browser == [(project / "index.html").as_uri()]


@pytest.mark.asyncio
async def test_with_no_project_in_sight_it_asks(ready):
    server, fake, _project = ready
    out = await server.tool_open_in_browser({"target": "index.html"})
    assert fake.browser == []
    assert out.rstrip().endswith("?")


# --- refusals: the whole point --------------------------------------------

@pytest.mark.asyncio
async def test_a_file_that_is_not_there_is_refused_not_faked(ready):
    """The same class of bug as calling a stalled run a success: opening
    nothing and saying it worked."""
    server, fake, _project = ready
    out = await server.tool_open_in_browser(
        {"target": "about.html", "project": "tony-starks-website"})
    assert fake.browser == []
    assert "no about.html" in out
    assert "opened nothing" in out


@pytest.mark.asyncio
async def test_an_absolute_path_outside_every_project_is_refused(ready,
                                                                tmp_path):
    server, fake, _project = ready
    outside = tmp_path / "secrets.html"
    outside.write_text("<h1>not yours</h1>", encoding="utf-8")

    out = await server.tool_open_in_browser({"target": str(outside)})

    assert fake.browser == [], "nothing outside a project may be opened"
    assert "isn't inside a project I know" in out


@pytest.mark.asyncio
async def test_a_traversal_out_of_a_project_is_refused(ready, tmp_path):
    server, fake, _project = ready
    (tmp_path / "elsewhere.html").write_text("x", encoding="utf-8")

    out = await server.tool_open_in_browser(
        {"target": "../elsewhere.html", "project": "tony-starks-website"})

    assert fake.browser == []
    assert "isn't inside a project I know" in out


@pytest.mark.asyncio
async def test_a_symlink_pointing_out_of_the_project_is_refused(ready,
                                                                tmp_path):
    """Containment is proved by resolving both sides — a string check alone
    has never been enough."""
    server, fake, project = ready
    secret = tmp_path / "secret.html"
    secret.write_text("x", encoding="utf-8")
    (project / "innocent.html").symlink_to(secret)

    out = await server.tool_open_in_browser(
        {"target": "innocent.html", "project": "tony-starks-website"})

    assert fake.browser == []
    assert "isn't inside a project I know" in out


@pytest.mark.asyncio
async def test_nothing_to_open_asks(ready):
    server, fake, _project = ready
    out = await server.tool_open_in_browser({"target": "  "})
    assert fake.browser == []
    assert out.rstrip().endswith("?")


@pytest.mark.asyncio
async def test_a_browser_that_will_not_start_is_reported(ready, monkeypatch):
    server, _fake, project = ready
    monkeypatch.setattr(server, "actions", _Actions(success=False))
    out = await server.tool_open_in_browser(
        {"target": "index.html", "project": "tony-starks-website"})
    assert "problem" in out.lower() or "wouldn't open" in out.lower()


# --- the terminal ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_terminal_opens_in_the_project(ready):
    server, fake, project = ready
    out = await server.tool_open_in_terminal({"project": "tony-starks-website"})
    import shlex
    assert fake.terminal == [f"cd {shlex.quote(str(project))}"]
    assert "tony-starks-website" in out


@pytest.mark.asyncio
async def test_an_unknown_project_opens_no_terminal(ready):
    server, fake, _project = ready
    out = await server.tool_open_in_terminal({"project": "nowhere"})
    assert fake.terminal == []
    assert "don't see that project" in out


@pytest.mark.asyncio
async def test_no_project_named_asks(ready):
    server, fake, _project = ready
    out = await server.tool_open_in_terminal({})
    assert fake.terminal == []
    assert out.rstrip().endswith("?")


# --- which browser --------------------------------------------------------
#
# The user: "can users set their default browser ... can we actually get mic
# working in Firefox cuz as of right now it forces us to use Google Chrome",
# and "can you open that for me in Firefox".


@pytest.mark.asyncio
async def test_chrome_is_the_default_when_nothing_is_configured(ready, monkeypatch):
    server, fake, _project = ready
    monkeypatch.delenv("JARVIS_DEFAULT_BROWSER", raising=False)
    await server.tool_open_in_browser({"target": "https://example.com"})
    assert fake.browsers == ["chrome"]


@pytest.mark.asyncio
async def test_the_configured_default_is_honoured(ready, monkeypatch):
    """The repo's existing convention — a JARVIS_* environment variable — and
    read at call time, so setting it does not need a restart."""
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "firefox")
    await server.tool_open_in_browser({"target": "https://example.com"})
    assert fake.browsers == ["firefox"]


@pytest.mark.asyncio
async def test_a_browser_named_for_one_call_beats_the_default(ready, monkeypatch):
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "chrome")
    await server.tool_open_in_browser({"target": "https://example.com",
                                       "browser": "Firefox"})
    assert fake.browsers == ["firefox"]


@pytest.mark.asyncio
async def test_a_file_from_a_project_goes_to_the_chosen_browser_too(ready,
                                                                    monkeypatch):
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "firefox")
    out = await server.tool_open_in_browser({"target": "index.html",
                                             "project": "tony-starks-website"})
    assert fake.browsers == ["firefox"]
    assert "index.html" in out


@pytest.mark.asyncio
async def test_a_browser_he_cannot_drive_is_refused_not_swapped(ready):
    """Saying "opened that in Safari, sir" while Chrome comes up is the same
    class of lie as reporting a stalled run as a success."""
    server, fake, _project = ready
    out = await server.tool_open_in_browser({"target": "https://example.com",
                                             "browser": "Safari"})
    assert fake.browser == [], "it opened something anyway"
    assert "Chrome or Firefox" in out


@pytest.mark.asyncio
async def test_a_nonsense_default_falls_back_rather_than_breaking(ready,
                                                                  monkeypatch):
    """A typo in .env must not stop pages opening — but it is logged."""
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "netscape")
    await server.tool_open_in_browser({"target": "https://example.com"})
    assert fake.browsers == ["chrome"]


# --- the microphone is a constraint, not a preference ---------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://localhost:5173/",
                                 "http://127.0.0.1:5173",
                                 "http://localhost:8340/",
                                 "https://localhost:5173/index.html"])
async def test_his_own_interface_is_not_opened_where_the_mic_is_dead(ready,
                                                                     monkeypatch,
                                                                     url):
    """`frontend/src/voice.ts` is built on webkitSpeechRecognition, which
    Firefox does not implement. Opening JARVIS there gives the user a page
    whose microphone can never work, and he would conclude JARVIS is broken.
    Say why; do not do it silently."""
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "firefox")
    out = await server.tool_open_in_browser({"target": url})
    assert fake.browser == [], "it opened his own UI where the mic is dead"
    assert "Chrome" in out and "microphone" in out


@pytest.mark.asyncio
async def test_his_own_interface_opens_perfectly_well_in_chrome(ready,
                                                                monkeypatch):
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "firefox")
    await server.tool_open_in_browser({"target": "http://localhost:5173/",
                                       "browser": "chrome"})
    assert fake.browsers == ["chrome"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://localhost:5173/dashboard",
                                 "https://example.com/",
                                 "http://localhost:3000/",
                                 "http://stark.example:5173/"])
async def test_everything_else_opens_in_firefox_quite_happily(ready, monkeypatch,
                                                              url):
    """The dashboard has no microphone in it, and neither has the rest of the
    web. Only JARVIS's own voice page is refused."""
    server, fake, _project = ready
    monkeypatch.setenv("JARVIS_DEFAULT_BROWSER", "firefox")
    await server.tool_open_in_browser({"target": url})
    assert fake.browsers == ["firefox"], f"{url} was wrongly refused"


def test_the_voice_ui_test_tracks_the_configured_port(ready, monkeypatch):
    server, _fake, _project = ready
    monkeypatch.setenv("JARVIS_PORT", "9999")
    assert server._is_jarvis_voice_ui("http://127.0.0.1:9999/") is True
    assert server._is_jarvis_voice_ui("http://127.0.0.1:8340/") is False


@pytest.mark.parametrize("url", ["not a url", "http://", "https://[::1", ""])
def test_a_malformed_url_is_not_mistaken_for_his_interface(ready, url):
    server, _fake, _project = ready
    assert server._is_jarvis_voice_ui(url) is False
