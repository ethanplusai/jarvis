"""
Speaker verification — compare an incoming utterance against the enrolled
canonical embedding. In-memory 30s cache per ws_id so we don't pay the
embedding cost on every transcript from the same connection.
"""

import logging
import time
from dataclasses import dataclass

import numpy as np

from .embedding import compute_embedding
from .storage import get_canonical_embedding

log = logging.getLogger("jarvis.voice_id.verify")

# Cosine similarity threshold. Resemblyzer embeddings are unit-norm, so
# cosine similarity between same-speaker utterances typically lands in
# 0.75–0.95 range; cross-speaker is 0.0–0.5. 0.75 is a sensible default
# that can be tuned once we have real enrollment data.
VERIFY_THRESHOLD = 0.75

# How long a positive verification stays cached for a given WebSocket
# connection. 30s is short enough that a stranger picking up mid-session
# is still caught; long enough to avoid per-utterance cost for the owner.
CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class VerifyResult:
    recognized: bool
    similarity: float
    profile_id: int | None
    from_cache: bool


_cache: dict[str, tuple[float, int]] = {}  # ws_id -> (verified_at, profile_id)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity for unit-norm embeddings (dot product suffices,
    but we normalize anyway in case the mean embedding drifted)."""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def verify_cached_or_new(ws_id: str, audio_bytes: bytes) -> VerifyResult:
    """Verify a speaker against the enrolled canonical embedding.

    Fast path: if ws_id has a cache entry <30s old, return it immediately.
    Slow path: compute embedding, compare, cache if positive.
    """
    now = time.time()
    cached = _cache.get(ws_id)
    if cached is not None:
        verified_at, profile_id = cached
        if now - verified_at < CACHE_TTL_SECONDS:
            return VerifyResult(
                recognized=True,
                similarity=1.0,  # unknown at cache-hit time; we already said yes
                profile_id=profile_id,
                from_cache=True,
            )
        # Stale — fall through to re-verify
        _cache.pop(ws_id, None)

    canonical = get_canonical_embedding()
    if canonical is None:
        # No profile enrolled — caller should gate on is_enrolled() first,
        # but this is a defensive fallback.
        return VerifyResult(recognized=False, similarity=0.0, profile_id=None, from_cache=False)

    profile_id, canonical_emb = canonical
    incoming_emb = compute_embedding(audio_bytes)
    similarity = _cosine_similarity(canonical_emb, incoming_emb)
    recognized = similarity >= VERIFY_THRESHOLD

    if recognized:
        _cache[ws_id] = (now, profile_id)
        log.debug(f"Verified ws={ws_id} similarity={similarity:.3f}")
    else:
        log.info(f"Rejected ws={ws_id} similarity={similarity:.3f} (threshold={VERIFY_THRESHOLD})")

    return VerifyResult(
        recognized=recognized,
        similarity=similarity,
        profile_id=profile_id if recognized else None,
        from_cache=False,
    )


def clear_cache(ws_id: str | None = None) -> None:
    """Drop cached verification for one ws_id, or all if None.

    Called when a WebSocket disconnects or when a profile is re-enrolled.
    """
    if ws_id is None:
        _cache.clear()
    else:
        _cache.pop(ws_id, None)
