"""
Speaker identification for JARVIS.

Public API (backend, consumed by api/voice.py and server.py voice_handler):
- enroll_sample(audio_bytes, name) -> int           # returns new sample_count
- verify_cached_or_new(ws_id, audio_bytes) -> VerifyResult
- get_status() -> StatusDict
- is_enrolled() -> bool                              # fast bool for the voice handler gate
- clear_profile() -> None
- clear_cache(ws_id) -> None                         # drop cache on disconnect
"""

from .storage import clear_profile, enroll_sample, get_status, is_enrolled
from .verify import VerifyResult, clear_cache, verify_cached_or_new

__all__ = [
    "VerifyResult",
    "clear_cache",
    "clear_profile",
    "enroll_sample",
    "get_status",
    "is_enrolled",
    "verify_cached_or_new",
]
