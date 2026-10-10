"""What counts as a LINE in `.env`, and who gets to decide.

The writer blocked three characters — `"\\n"`, `"\\r"`, `"\\0"`. Every reader
of the file splits it with `str.splitlines()`, which splits on ten. The gap
was a one-request exploit, confirmed against a live server:

    POST /api/settings/preferences
    {"user_name": "Tony\\x0bJARVIS_CLAUDE_PATH=/tmp/evil", "honorific": "sir"}
    -> 200
    _read_env() -> {..., 'JARVIS_CLAUDE_PATH': '/tmp/evil'}

`JARVIS_CLAUDE_PATH` is the binary the brain is spawned from, and
`POST /api/restart` is one call away.

The class of input here is not "the newline" and it is not "these eleven
characters" either — a list typed by hand is exactly what failed the first
time. It is **every character the reader treats as a line boundary**, so
this file DERIVES the set by asking `str.splitlines()` over the whole
Unicode range, and derives the writer's rule from the reader's parser rather
than from a second list that has to be kept in step by hand.

Sweeping all of Unicode costs about a tenth of a second, which is the price
of never having to trust that somebody remembered `\\x85`.
"""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

DASHBOARD_ORIGIN = "http://localhost:5173"

# The reader's own answer to "what ends a line", asked of every codepoint
# there is. Nothing in this file is hand-listed.
SEPARATORS = sorted(chr(c) for c in range(0x110000)
                    if ("a" + chr(c) + "b").splitlines() != ["a" + chr(c) + "b"])

PAYLOAD = "JARVIS_CLAUDE_PATH=/tmp/evil"


def _ids(sep: str) -> str:
    return hex(ord(sep))


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_PORT", "8340")
    import data_paths
    importlib.reload(data_paths)
    import run_store
    importlib.reload(run_store)
    import server
    importlib.reload(server)
    run_store.init_db()
    with TestClient(server.app) as c:
        yield c, server


def test_the_separator_set_is_derived_and_not_typed():
    """A guard on the guard: if this ever comes back empty, every
    parametrised test below silently tests nothing."""
    assert len(SEPARATORS) >= 10, SEPARATORS
    # The three the old blocklist knew about are in here, along with the
    # seven it did not. Named only to prove the derivation found them.
    for known in "\n\r\v\f\x1c\x1d\x1e\x85  ":
        assert known in SEPARATORS, hex(ord(known))
    assert "\0" not in SEPARATORS, "splitlines does not split on NUL"


@pytest.mark.parametrize("sep", SEPARATORS, ids=_ids)
def test_the_reader_really_does_split_on_it(env, sep):
    """The other half of the pair: prove the READER splits here, by writing
    the file by hand and asking the reader what it sees. Without this, a
    writer test proves only that the writer is fussy."""
    _c, server = env
    path = server._env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"USER_NAME=Tony{sep}{PAYLOAD}\n", encoding="utf-8")
    _lines, parsed = server._read_env()
    assert parsed.get("JARVIS_CLAUDE_PATH") == "/tmp/evil", (hex(ord(sep)),
                                                             parsed)


@pytest.mark.parametrize("sep", SEPARATORS, ids=_ids)
def test_the_writer_refuses_every_separator_the_reader_splits_on(env, sep):
    _c, server = env
    with pytest.raises(ValueError):
        server._write_env_key("USER_NAME", f"Tony{sep}{PAYLOAD}")


@pytest.mark.parametrize("sep", SEPARATORS, ids=_ids)
def test_no_separator_smuggles_a_setting_through_the_keys_endpoint(env, sep):
    c, server = env
    r = c.post("/api/settings/keys",
               json={"key_name": "USER_NAME",
                     "key_value": f"Tony{sep}{PAYLOAD}"},
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 400, (hex(ord(sep)), r.text)
    _lines, parsed = server._read_env()
    assert "JARVIS_CLAUDE_PATH" not in parsed, (hex(ord(sep)), parsed)


@pytest.mark.parametrize("sep", SEPARATORS, ids=_ids)
def test_no_separator_smuggles_a_setting_through_the_preferences_endpoint(
        env, sep):
    c, server = env
    r = c.post("/api/settings/preferences",
               json={"user_name": f"Tony{sep}{PAYLOAD}", "honorific": "sir"},
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 400, (hex(ord(sep)), r.text)
    _lines, parsed = server._read_env()
    assert "JARVIS_CLAUDE_PATH" not in parsed, (hex(ord(sep)), parsed)


@pytest.mark.parametrize("sep", SEPARATORS, ids=_ids)
def test_the_honorific_goes_through_the_same_gate(env, sep):
    """Two fields, one rule. The second one was validated by the same
    hand-written list, so it had the same hole."""
    c, server = env
    r = c.post("/api/settings/preferences",
               json={"user_name": "Tony", "honorific": f"sir{sep}{PAYLOAD}"},
               headers={"Origin": DASHBOARD_ORIGIN})
    assert r.status_code == 400, (hex(ord(sep)), r.text)
    assert "JARVIS_CLAUDE_PATH" not in server._read_env()[1]


def test_the_crlf_pair_is_refused(env):
    _c, server = env
    with pytest.raises(ValueError):
        server._write_env_key("USER_NAME", f"Tony\r\n{PAYLOAD}")


def test_a_null_byte_is_still_refused(env):
    """`splitlines()` does not split on NUL, so the derivation above will
    never find it — but it truncates the string for anything that hands the
    path to a C API, so it keeps its own rule and its own reason."""
    _c, server = env
    with pytest.raises(ValueError):
        server._write_env_key("USER_NAME", "Tony\0evil")


def test_the_writer_refuses_everything_the_reader_splits_on(env):
    """The property, over every codepoint there is: nothing the reader would
    split on can be written.

    This is the test that would have caught the original bug, and it is the
    one that will catch the next Unicode release adding a separator. Asked
    of the predicate rather than of the filesystem, so it can afford to be
    exhaustive.

    It used to be stated as an "if and only if", and it is not one any more:
    the writer now ALSO refuses the rest of C0/C1 and DEL, which the reader
    would read back happily. An ESC is not a separator and it is not a
    setting either — `USER_NAME` is spliced into the brain's system prompt
    (`brain.launch_prompt`) and rendered by whatever reads the log. The
    exact extra rule is asserted by `test_a_control_character_is_refused` in
    tests/test_bounds.py; here the direction that matters is that no
    separator ever gets through.
    """
    _c, server = env
    wrong = []
    for cp in range(0x110000):
        ch = chr(cp)
        value = "a" + ch + "b"
        splits = value.splitlines() != [value]
        refused = server._env_value_problem("USER_NAME", value) is not None
        if (splits or ch == "\0") and not refused:
            wrong.append((hex(cp), splits, refused))
        if len(wrong) > 20:
            break
    assert not wrong, wrong


def test_every_ordinary_printable_character_still_saves(env):
    """The other direction, kept honest: the extra rule above must not have
    quietly become "refuse everything interesting". Every printable
    codepoint that the reader would read back is still writable."""
    _c, server = env
    import unicodedata
    wrong = []
    for cp in range(0x110000):
        ch = chr(cp)
        if unicodedata.category(ch)[0] == "C":     # control/format/surrogate
            continue
        value = "a" + ch + "b"
        if value.splitlines() != [value]:
            continue
        if server._env_value_problem("USER_NAME", value) is not None:
            wrong.append(hex(cp))
        if len(wrong) > 20:
            break
    assert not wrong, wrong


def test_ordinary_values_still_save(env):
    """A gate that refuses everything is not a gate, it is an outage."""
    _c, server = env
    for value in ("Tony Stark", "sir", "O'Brien", "Tony-Stark_1",
                  "Éthan Røgers", "🫡", "sk-live-abc123.def_456",
                  "a=b=c", "#notacomment", ""):
        server._write_env_key("USER_NAME", value)
        assert server._read_env()[1].get("USER_NAME", "") == value, value


def test_a_value_the_reader_would_change_is_refused(env):
    """Deliberate, and the reason is honesty rather than injection: the
    reader strips whitespace and one layer of quotes, so ` Tony ` would
    come back as `Tony` and `'Tony'` as `Tony`. Saying "saved" and
    storing something else is the same class of lie as reporting a stalled
    run as a success."""
    _c, server = env
    for value in (" Tony ", "'Tony'", '"Tony"', "Tony\t"):
        with pytest.raises(ValueError):
            server._write_env_key("USER_NAME", value)
