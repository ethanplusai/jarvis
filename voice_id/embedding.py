"""
Resemblyzer wrapper — computes 256-dim speaker embeddings.

The VoiceEncoder model is loaded once, lazily, on first call. ~14MB
download on first import; ~20ms per embedding on CPU after that.
"""

import logging
import threading

import numpy as np

from .wav import decode

log = logging.getLogger("jarvis.voice_id.embedding")

EMBEDDING_DIM = 256

# Lazy-loaded singleton so importing this module is cheap — the model
# only loads when we actually compute an embedding.
_encoder = None
_encoder_lock = threading.Lock()


def _get_encoder():
    """Load VoiceEncoder lazily. Thread-safe."""
    global _encoder
    if _encoder is not None:
        return _encoder
    with _encoder_lock:
        if _encoder is None:
            from resemblyzer import VoiceEncoder

            log.info("Loading resemblyzer VoiceEncoder (first use)")
            _encoder = VoiceEncoder(verbose=False)
    return _encoder


def compute_embedding(audio_bytes: bytes) -> np.ndarray:
    """Decode audio + produce a 256-dim float32 embedding.

    Raises wav.AudioTooShortError if the clip is too short to embed.
    """
    audio = decode(audio_bytes)
    encoder = _get_encoder()
    embedding = encoder.embed_utterance(audio)
    return embedding.astype(np.float32, copy=False)
