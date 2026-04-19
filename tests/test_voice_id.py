"""
Speaker identification — unit tests for the voice_id package.

We use synthetic audio (different signal families act as different
"speakers") rather than real recordings. Real-voice smoke is done
manually via the enrollment UI after PR #2.
"""

import io
import time

import numpy as np
import pytest
import soundfile as sf

# ---------------------------------------------------------------------------
# Isolate each test: point DB_PATH at a tmp file, reset in-memory cache
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_voice_id(tmp_path, monkeypatch):
    """Redirect voice_id.storage.DB_PATH to a per-test tmp file, clear caches,
    and reset server._AUTH_TOKEN (other tests leave it set).
    """
    import server
    from voice_id import storage, verify

    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "voice_profiles.db")
    monkeypatch.setattr(server, "_AUTH_TOKEN", "")
    verify._cache.clear()
    # Force the resemblyzer encoder to persist across tests for speed — it
    # takes ~0.5s to load. Don't reset embedding._encoder.
    yield
    verify._cache.clear()


# ---------------------------------------------------------------------------
# Synthetic "speakers"
# ---------------------------------------------------------------------------


def _synth_wav(kind: str, seed: int) -> bytes:
    """Generate a 2s WAV clip. Each `kind` is a different synthetic 'speaker'."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 2, 32000, dtype=np.float32)
    if kind == "speaker_a":
        # Pink-ish noise centered on 200Hz
        x = 0.3 * rng.standard_normal(32000).astype(np.float32)
        x += 0.2 * np.sin(2 * np.pi * 200 * t)
    elif kind == "speaker_b":
        # Much higher fundamental + different noise
        x = 0.1 * rng.standard_normal(32000).astype(np.float32)
        x += 0.4 * np.sin(2 * np.pi * 440 * t)
        x += 0.2 * np.sin(2 * np.pi * 880 * t)
    else:
        raise ValueError(f"unknown speaker kind: {kind}")
    buf = io.BytesIO()
    sf.write(buf, x, 16000, format="WAV")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Storage + enrollment
# ---------------------------------------------------------------------------


class TestEnrollment:
    def test_enroll_stores_sample(self, isolated_voice_id):
        from voice_id import enroll_sample, get_status, is_enrolled

        assert not is_enrolled()
        assert get_status() == {"enrolled": False, "name": None, "sample_count": 0}

        count = enroll_sample(_synth_wav("speaker_a", 1), "tom")
        assert count == 1
        assert is_enrolled()
        assert get_status() == {"enrolled": True, "name": "tom", "sample_count": 1}

    def test_enroll_multiple_samples_increments_count(self, isolated_voice_id):
        from voice_id import enroll_sample, get_status

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        enroll_sample(_synth_wav("speaker_a", 2), "tom")
        count = enroll_sample(_synth_wav("speaker_a", 3), "tom")
        assert count == 3
        assert get_status()["sample_count"] == 3

    def test_audio_too_short_rejected(self, isolated_voice_id):
        from voice_id import enroll_sample
        from voice_id.wav import AudioTooShortError

        # 0.5 seconds at 16kHz — below the 1s floor
        short = np.zeros(8000, dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, short, 16000, format="WAV")

        with pytest.raises(AudioTooShortError):
            enroll_sample(buf.getvalue(), "tom")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerification:
    def test_same_speaker_passes(self, isolated_voice_id):
        from voice_id import enroll_sample, verify_cached_or_new
        from voice_id.verify import VERIFY_THRESHOLD

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        enroll_sample(_synth_wav("speaker_a", 2), "tom")

        result = verify_cached_or_new("ws-test-1", _synth_wav("speaker_a", 99))
        assert result.recognized, f"same speaker rejected (sim={result.similarity:.3f}, threshold={VERIFY_THRESHOLD})"
        assert result.similarity > VERIFY_THRESHOLD
        assert result.profile_id is not None
        assert not result.from_cache

    def test_different_speaker_rejected(self, isolated_voice_id):
        from voice_id import enroll_sample, verify_cached_or_new
        from voice_id.verify import VERIFY_THRESHOLD

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        enroll_sample(_synth_wav("speaker_a", 2), "tom")

        result = verify_cached_or_new("ws-test-2", _synth_wav("speaker_b", 99))
        assert not result.recognized, f"different speaker passed (sim={result.similarity:.3f})"
        assert result.similarity < VERIFY_THRESHOLD
        assert result.profile_id is None

    def test_no_profile_returns_false_without_error(self, isolated_voice_id):
        from voice_id import verify_cached_or_new

        result = verify_cached_or_new("ws-test-3", _synth_wav("speaker_a", 1))
        assert not result.recognized
        assert result.similarity == 0.0
        assert result.profile_id is None

    def test_cache_hit_within_ttl(self, isolated_voice_id):
        from voice_id import enroll_sample, verify_cached_or_new

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        first = verify_cached_or_new("ws-cache", _synth_wav("speaker_a", 2))
        assert first.recognized and not first.from_cache

        # Second call from the same ws_id — even with audio that'd normally
        # miss, the cache hit short-circuits the check.
        second = verify_cached_or_new("ws-cache", _synth_wav("speaker_b", 99))
        assert second.from_cache
        assert second.recognized

    def test_cache_expires_after_ttl(self, isolated_voice_id, monkeypatch):
        from voice_id import enroll_sample, verify_cached_or_new
        from voice_id import verify as verify_mod

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        verify_cached_or_new("ws-expire", _synth_wav("speaker_a", 2))
        assert "ws-expire" in verify_mod._cache

        # Fast-forward time past the TTL by monkey-patching time.time
        real_now = time.time()
        monkeypatch.setattr(verify_mod.time, "time", lambda: real_now + verify_mod.CACHE_TTL_SECONDS + 1)

        # Next call with an impostor — should NOT short-circuit
        result = verify_cached_or_new("ws-expire", _synth_wav("speaker_b", 99))
        assert not result.from_cache
        assert not result.recognized

    def test_clear_cache_one_ws(self, isolated_voice_id):
        from voice_id import clear_cache, enroll_sample, verify_cached_or_new
        from voice_id import verify as verify_mod

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        verify_cached_or_new("ws-a", _synth_wav("speaker_a", 2))
        verify_cached_or_new("ws-b", _synth_wav("speaker_a", 3))
        assert "ws-a" in verify_mod._cache
        assert "ws-b" in verify_mod._cache

        clear_cache("ws-a")
        assert "ws-a" not in verify_mod._cache
        assert "ws-b" in verify_mod._cache


# ---------------------------------------------------------------------------
# Clear profile
# ---------------------------------------------------------------------------


class TestClearProfile:
    def test_clear_resets_status(self, isolated_voice_id):
        from voice_id import clear_profile, enroll_sample, get_status, is_enrolled

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        assert is_enrolled()

        clear_profile()
        assert not is_enrolled()
        assert get_status() == {"enrolled": False, "name": None, "sample_count": 0}

    def test_clear_also_drops_verification_cache(self, isolated_voice_id):
        from voice_id import clear_cache, clear_profile, enroll_sample, verify_cached_or_new
        from voice_id import verify as verify_mod

        enroll_sample(_synth_wav("speaker_a", 1), "tom")
        verify_cached_or_new("ws-xyz", _synth_wav("speaker_a", 2))
        assert "ws-xyz" in verify_mod._cache

        clear_profile()
        clear_cache()  # API layer will call this after DELETE /enroll
        assert "ws-xyz" not in verify_mod._cache


# ---------------------------------------------------------------------------
# REST API surface (FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestRESTEndpoints:
    def test_status_endpoint_returns_not_enrolled(self, isolated_voice_id):
        from fastapi.testclient import TestClient

        import server

        client = TestClient(server.app)
        r = client.get("/api/voice/status", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert r.json() == {"enrolled": False, "name": None, "sample_count": 0}

    def test_enroll_via_upload(self, isolated_voice_id):
        from fastapi.testclient import TestClient

        import server

        client = TestClient(server.app)
        wav = _synth_wav("speaker_a", 1)
        r = client.post(
            "/api/voice/enroll",
            headers={"Authorization": "Bearer "},
            data={"name": "tom"},
            files={"audio": ("sample.wav", wav, "audio/wav")},
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"success": True, "name": "tom", "sample_count": 1}

        r = client.get("/api/voice/status", headers={"Authorization": "Bearer "})
        assert r.json()["enrolled"] is True

    def test_enroll_rejects_short_audio(self, isolated_voice_id):
        from fastapi.testclient import TestClient

        import server

        client = TestClient(server.app)
        short = np.zeros(4000, dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, short, 16000, format="WAV")
        r = client.post(
            "/api/voice/enroll",
            headers={"Authorization": "Bearer "},
            data={"name": "tom"},
            files={"audio": ("sample.wav", buf.getvalue(), "audio/wav")},
        )
        assert r.status_code == 400
        assert r.json()["success"] is False

    def test_test_endpoint_scores_without_gating(self, isolated_voice_id):
        from fastapi.testclient import TestClient

        import server

        client = TestClient(server.app)
        client.post(
            "/api/voice/enroll",
            headers={"Authorization": "Bearer "},
            data={"name": "tom"},
            files={"audio": ("a.wav", _synth_wav("speaker_a", 1), "audio/wav")},
        )

        r = client.post(
            "/api/voice/test",
            headers={"Authorization": "Bearer "},
            files={"audio": ("b.wav", _synth_wav("speaker_b", 99), "audio/wav")},
        )
        assert r.status_code == 200
        body = r.json()
        assert "similarity" in body
        assert body["recognized"] is False
        assert body["threshold"] == 0.75

    def test_clear_endpoint_resets(self, isolated_voice_id):
        from fastapi.testclient import TestClient

        import server

        client = TestClient(server.app)
        client.post(
            "/api/voice/enroll",
            headers={"Authorization": "Bearer "},
            data={"name": "tom"},
            files={"audio": ("a.wav", _synth_wav("speaker_a", 1), "audio/wav")},
        )
        r = client.delete("/api/voice/enroll", headers={"Authorization": "Bearer "})
        assert r.status_code == 200
        assert r.json() == {"success": True}

        r = client.get("/api/voice/status", headers={"Authorization": "Bearer "})
        assert r.json()["enrolled"] is False
