#!/usr/bin/env python3
"""
clap_listener.py — wake JARVIS with two claps.

Listens to the Mac's microphone for two sharp claps in quick succession, then:
  * if the JARVIS server isn't running, starts it (./start-jarvis.sh);
  * if no JARVIS tab is open, opens it in Chrome (it greets you on connect);
  * otherwise brings Chrome forward and asks the server to greet you with the
    weather (POST /api/wake, authorised with JARVIS's own tool token).

Tuning (in .env or the environment):
  CLAP_SENSITIVITY  how far above room noise a clap must be (default 7; lower = more sensitive)
  CLAP_MIN_LEVEL    absolute peak floor, 0..1 (default 0.25)
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd

HERE = Path(__file__).resolve().parent
SERVER = "https://127.0.0.1:8340"
FRONTEND = "http://localhost:5173"
RATE, BLOCK = 44100, 512                 # ~11.6 ms blocks
SENSITIVITY = float(os.getenv("CLAP_SENSITIVITY", "8"))
MIN_LEVEL = float(os.getenv("CLAP_MIN_LEVEL", "0.3"))
GAP_MIN, GAP_MAX = 0.2, 0.8              # seconds between the two claps (under 0.2 s is one bump plus its echo)
MAX_CLAP_LEN = 0.15                      # a clap is a short spike, not a sustained sound
COOLDOWN = 20.0

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE         # localhost, JARVIS's own self-signed cert


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), "[clap]", msg, flush=True)


def _token() -> str:
    for p in (HERE / "data" / "jarvis" / "tool-token",):
        try:
            return p.read_text().strip()
        except OSError:
            pass
    return ""


def _server_up() -> bool:
    try:
        urllib.request.urlopen(SERVER + "/api/health", context=_ctx, timeout=2)
        return True
    except urllib.error.HTTPError:
        return True                      # answered, just not 200
    except Exception:
        return False


def wake() -> None:
    if not _server_up():
        log("JARVIS isn't running — starting it")
        subprocess.Popen([str(HERE / "start-jarvis.sh")], cwd=HERE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    req = urllib.request.Request(SERVER + "/api/wake", method="POST", data=b"{}",
                                 headers={"Authorization": f"Bearer {_token()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ctx, timeout=5) as r:
            res = json.load(r)
    except Exception as e:
        log(f"wake request failed: {e}")
        res = {"voice_client": False}
    if res.get("ignored"):
        log(f"ignored ({res.get('ignored')})")
    elif res.get("voice_client"):
        subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'],
                       capture_output=True)
        log("woke JARVIS")
    else:
        log("no JARVIS tab open — opening it")
        subprocess.run([str(HERE / "open-jarvis-tab.sh")])


def main() -> None:
    noise = deque([0.01] * 200, maxlen=200)      # ~2.3 s of background peaks
    state = {"in_spike": False, "spike_start": 0.0, "last_clap": 0.0, "cooldown_until": 0.0, "peak": 0.0}

    def on_audio(indata, frames, t, status):
        now = time.monotonic()
        peak = float(np.abs(indata[:, 0]).max())
        floor = float(np.median(noise))
        loud = peak > MIN_LEVEL and peak > floor * SENSITIVITY
        if loud and not state["in_spike"]:
            state["in_spike"], state["spike_start"], state["peak"] = True, now, peak
        elif loud:
            state["peak"] = max(state["peak"], peak)
        elif not loud and state["in_spike"]:
            state["in_spike"] = False
            if now - state["spike_start"] > MAX_CLAP_LEN:
                log(f"too long for a clap: {1000*(now - state['spike_start']):.0f} ms, peak {state['peak']:.2f}")
            elif now >= state["cooldown_until"]:
                gap = now - state["last_clap"]
                if gap < GAP_MIN:          # the room's echo of the same clap
                    return
                log(f"clap: peak {state['peak']:.2f}, room {floor:.3f}, {1000*(now - state['spike_start']):.0f} ms, gap {gap:.2f}s")
                if GAP_MIN <= gap <= GAP_MAX:
                    state["last_clap"], state["cooldown_until"] = 0.0, now + COOLDOWN
                    threading.Thread(target=wake, daemon=True).start()
                else:
                    state["last_clap"] = now
        if not loud:
            noise.append(max(peak, 0.002))

    log(f"listening for two claps (sensitivity {SENSITIVITY}, floor {MIN_LEVEL})")
    with sd.InputStream(channels=1, samplerate=RATE, blocksize=BLOCK, callback=on_audio):
        while True:
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
