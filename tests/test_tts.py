"""tts.synthesize_chunk on the Mac's built-in `say`.

`say` is faked at the subprocess seam: the fake writes the WAV file `say -o`
would have written, so no audio is ever really synthesised.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class _Proc:
    def __init__(self, rc=0, err=b"", hang=False):
        self.returncode = rc
        self._err = err
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(10)
        return b"", self._err

    def kill(self):
        self.killed = True


def _fake_say(monkeypatch, *, results, audio=b"RIFF....WAVEfmt "):
    """Each call pops the next (rc, err, hang) from `results`; returns the argv log."""
    import tts
    calls = []

    async def exec_(*args, **kwargs):
        calls.append(list(args))
        rc, err, hang = results.pop(0)
        if rc == 0 and not hang:
            out = args[args.index("-o") + 1]
            Path(out).write_bytes(audio)
        return _Proc(rc, err, hang)

    monkeypatch.setattr(tts.asyncio, "create_subprocess_exec", exec_)
    return calls


@pytest.mark.asyncio
async def test_say_writes_a_wav_with_the_configured_voice(monkeypatch):
    import tts
    monkeypatch.setattr(tts, "MAC_VOICE", "Daniel")
    calls = _fake_say(monkeypatch, results=[(0, b"", False)])
    r = await tts.synthesize_chunk("Good evening.")
    assert r is not None and r.audio.startswith(b"RIFF")
    argv = calls[0]
    assert argv[0] == "say"
    assert argv[argv.index("-v") + 1] == "Daniel"
    assert "--file-format=WAVE" in argv
    assert argv[-2:] == ["--", "Good evening."]   # text after --, never parsed as a flag
    assert r.total_sec >= 0


@pytest.mark.asyncio
async def test_a_failing_voice_falls_back_to_the_system_default(monkeypatch):
    import tts
    monkeypatch.setattr(tts, "MAC_VOICE", "NoSuchVoice")
    calls = _fake_say(monkeypatch, results=[(1, b"voice not found", False), (0, b"", False)])
    r = await tts.synthesize_chunk("Hello")
    assert r is not None
    assert "-v" in calls[0] and "-v" not in calls[1]


@pytest.mark.asyncio
async def test_say_failing_outright_returns_none(monkeypatch):
    import tts
    _fake_say(monkeypatch, results=[(1, b"x", False), (1, b"x", False)])
    assert await tts.synthesize_chunk("x") is None


@pytest.mark.asyncio
async def test_a_hung_say_is_killed_and_returns_none(monkeypatch):
    import tts
    monkeypatch.setattr(tts, "MAC_VOICE", "")
    _fake_say(monkeypatch, results=[(0, b"", True)])
    assert await tts.synthesize_chunk("x", timeout=0.05) is None


@pytest.mark.asyncio
async def test_empty_text_short_circuits(monkeypatch):
    import tts
    calls = _fake_say(monkeypatch, results=[])
    assert await tts.synthesize_chunk("   ") is None
    assert calls == []


@pytest.mark.asyncio
async def test_no_temp_file_is_left_behind(monkeypatch, tmp_path):
    import tts
    monkeypatch.setattr(tts.tempfile, "tempdir", str(tmp_path))
    _fake_say(monkeypatch, results=[(0, b"", False)])
    assert await tts.synthesize_chunk("Hello") is not None
    assert list(tmp_path.iterdir()) == []
