"""
Audio decode for speaker ID.

Accepts two wire formats:
1. WAV bytes (RIFF header) — what tests upload via curl, what soundfile reads natively.
2. Raw 16-bit PCM mono @ 16kHz (no header) — what the frontend AudioWorklet produces.

Returns float32 numpy array sampled at 16kHz — the format resemblyzer's
VoiceEncoder.embed_utterance expects.
"""

import io

import numpy as np
import soundfile as sf

TARGET_SAMPLE_RATE = 16_000
MIN_DURATION_SECONDS = 1.0  # resemblyzer needs ~1.6s, but we accept >=1.0s


class AudioTooShortError(ValueError):
    """Raised when an audio clip is shorter than MIN_DURATION_SECONDS."""


def decode(audio_bytes: bytes) -> np.ndarray:
    """Decode audio bytes to 16kHz mono float32.

    Detects WAV via RIFF magic; otherwise treats bytes as raw 16-bit PCM
    mono at 16kHz. Returns a 1-D float32 array in [-1.0, 1.0].

    Raises AudioTooShortError if the clip is under MIN_DURATION_SECONDS.
    """
    if len(audio_bytes) < 4:
        raise AudioTooShortError(f"Audio too small: {len(audio_bytes)} bytes")

    audio = _decode_wav(audio_bytes) if audio_bytes[:4] == b"RIFF" else _decode_raw_pcm16(audio_bytes)

    duration = len(audio) / TARGET_SAMPLE_RATE
    if duration < MIN_DURATION_SECONDS:
        raise AudioTooShortError(f"Audio {duration:.2f}s — need at least {MIN_DURATION_SECONDS}s")

    return audio


def _decode_wav(audio_bytes: bytes) -> np.ndarray:
    """WAV → mono float32 at 16kHz. Resamples if the source rate differs."""
    data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=False)
    # Downmix to mono if needed
    if data.ndim == 2:
        data = data.mean(axis=1)
    if sr != TARGET_SAMPLE_RATE:
        data = _resample(data, sr, TARGET_SAMPLE_RATE)
    return data.astype(np.float32, copy=False)


def _decode_raw_pcm16(audio_bytes: bytes) -> np.ndarray:
    """Raw signed 16-bit PCM mono at 16kHz → float32 in [-1, 1]."""
    if len(audio_bytes) % 2 != 0:
        raise ValueError(f"Raw PCM16 must have even byte count, got {len(audio_bytes)}")
    samples = np.frombuffer(audio_bytes, dtype=np.int16)
    return (samples.astype(np.float32) / 32768.0).copy()


def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample using librosa (high quality, slow-ish but enrollment is rare)."""
    import librosa

    return librosa.resample(audio, orig_sr=src_rate, target_sr=dst_rate)
