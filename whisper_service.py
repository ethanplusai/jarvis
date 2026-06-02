#!/usr/bin/env python3
"""Whisper transcription microservice for JARVIS.

Runs under the dedicated Python 3.12 venv (whisper-venv), because faster-whisper
and its deps (av, ctranslate2, onnxruntime) have no Python 3.14 wheels. The main
JARVIS backend (3.14) calls this over localhost HTTP, one request per utterance.

Protocol:
  GET  /health                      -> {"status":"ok","model":...}
  POST /transcribe  (raw body)      -> {"text","language","probability"}
      Body: an encoded audio blob from the browser's MediaRecorder (Opus/WebM).
      faster-whisper decodes it via ffmpeg/av. Auto-detects the language; if it
      lands outside the allowed set it is clamped to English so JARVIS never
      replies in a language it has no voice for.
"""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
PORT = int(os.getenv("WHISPER_PORT", "8765"))
ALLOWED = {l.strip() for l in os.getenv("WHISPER_LANGS", "en,fr,tr").split(",") if l.strip()}

print(f"[whisper] loading model '{MODEL_SIZE}' …", flush=True)
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
_lock = threading.Lock()  # faster-whisper isn't meant for concurrent calls
print(f"[whisper] ready on :{PORT}  langs={sorted(ALLOWED)}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "model": MODEL_SIZE})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/transcribe":
            self._json(404, {"error": "not found"})
            return
        # Optional ?lang=tr forces the language instead of auto-detecting.
        forced = parse_qs(parsed.query).get("lang", [None])[0]
        forced = forced if forced in ALLOWED else None
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b""
        if len(raw) < 64:
            self._json(400, {"error": "empty audio"})
            return
        # Write the encoded blob to a temp file; faster-whisper decodes it via av.
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(raw)
            path = f.name
        if os.getenv("WHISPER_DEBUG_DUMP"):
            with open("/tmp/whisper_last.webm", "wb") as d:
                d.write(raw)
            print(f"[whisper] dump: {len(raw)} bytes -> /tmp/whisper_last.webm", flush=True)
        try:
            # Decode to 16 kHz mono float, then peak-normalize — the browser mic
            # level is unpredictable, so we level it here for reliable recognition.
            audio = decode_audio(path, sampling_rate=16000)
            peak = float(np.abs(audio).max()) if audio.size else 0.0
            if peak > 1e-4:
                audio = audio * (0.95 / peak)
            if os.getenv("WHISPER_DEBUG_DUMP"):
                print(f"[whisper] decoded {audio.size/16000:.2f}s peak={peak:.4f}", flush=True)
            with _lock:
                segments, info = model.transcribe(
                    audio, language=forced, beam_size=5,
                    vad_filter=True,
                    no_speech_threshold=0.6,
                    condition_on_previous_text=False,
                )
                text = " ".join(s.text.strip() for s in segments).strip()
            lang = forced or (info.language if info.language in ALLOWED else "en")
            self._json(200, {
                "text": text,
                "language": lang,
                "probability": round(float(info.language_probability), 3),
                "detected": info.language,
            })
        except Exception as exc:  # never crash the loop on a bad clip
            self._json(500, {"error": str(exc)})
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def log_message(self, *args):  # silence default request logging
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
