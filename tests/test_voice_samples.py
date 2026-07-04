"""Voice sample capture API tests."""

import base64
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

import server  # noqa: E402


def test_voice_sample_round_trip(tmp_path, monkeypatch):
    sample_dir = tmp_path / "voice_samples"
    monkeypatch.setattr(server, "VOICE_SAMPLE_DIR", sample_dir)
    monkeypatch.setattr(server, "VOICE_SAMPLE_META", sample_dir / "samples.json")

    client = TestClient(server.app)
    payload = {
        "filename": "../My Jarvis Sample.webm",
        "mime_type": "audio/webm",
        "duration_seconds": 12.4,
        "data_base64": base64.b64encode(b"fake-webm-audio").decode(),
    }

    saved = client.post("/api/voice-samples", json=payload)
    assert saved.status_code == 200
    body = saved.json()
    assert body["success"] is True
    assert body["sample"]["name"] == "My-Jarvis-Sample.webm"
    assert (sample_dir / "My-Jarvis-Sample.webm").read_bytes() == b"fake-webm-audio"

    listed = client.get("/api/voice-samples")
    assert listed.status_code == 200
    samples = listed.json()["samples"]
    assert len(samples) == 1
    assert samples[0]["download_url"] == "/api/voice-samples/My-Jarvis-Sample.webm"

    downloaded = client.get(samples[0]["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"fake-webm-audio"


def test_voice_sample_rejects_invalid_audio(tmp_path, monkeypatch):
    sample_dir = tmp_path / "voice_samples"
    monkeypatch.setattr(server, "VOICE_SAMPLE_DIR", sample_dir)
    monkeypatch.setattr(server, "VOICE_SAMPLE_META", sample_dir / "samples.json")

    client = TestClient(server.app)
    response = client.post("/api/voice-samples", json={
        "filename": "sample.exe",
        "mime_type": "application/octet-stream",
        "duration_seconds": 1,
        "data_base64": base64.b64encode(b"x").decode(),
    })
    assert response.status_code == 400
    assert response.json()["success"] is False
