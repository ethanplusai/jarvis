"""
Voice-ID API — /api/voice/{enroll, status, test}

server.py mounts this via build_voice_router(require_auth). Pattern
matches api/settings.py — one factory function returning an APIRouter
with the auth dependency pre-applied.
"""

import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse

import voice_id
from voice_id.embedding import compute_embedding
from voice_id.storage import get_canonical_embedding
from voice_id.verify import VERIFY_THRESHOLD, _cosine_similarity
from voice_id.wav import AudioTooShortError

log = logging.getLogger("jarvis.api_voice")


def build_voice_router(require_auth: Callable) -> APIRouter:
    """Build the /api/voice/* router with injected auth dependency."""
    router = APIRouter(prefix="/api/voice", dependencies=[Depends(require_auth)])

    @router.post("/enroll")
    async def enroll(name: str = Form(...), audio: UploadFile = File(...)):
        audio_bytes = await audio.read()
        try:
            sample_count = voice_id.enroll_sample(audio_bytes, name.strip())
        except AudioTooShortError as e:
            return JSONResponse(status_code=400, content={"success": False, "error": str(e)})
        return {"success": True, "name": name.strip(), "sample_count": sample_count}

    @router.delete("/enroll")
    async def clear():
        voice_id.clear_profile()
        voice_id.clear_cache()
        return {"success": True}

    @router.get("/status")
    async def status():
        return voice_id.get_status()

    @router.post("/test")
    async def test(audio: UploadFile = File(...)):
        """Score audio against the enrolled profile without gating. For
        tuning the threshold during enrollment testing.
        """
        if not voice_id.is_enrolled():
            return JSONResponse(status_code=400, content={"error": "No profile enrolled"})
        audio_bytes = await audio.read()
        try:
            incoming = compute_embedding(audio_bytes)
        except AudioTooShortError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        canonical = get_canonical_embedding()
        if canonical is None:
            return JSONResponse(status_code=500, content={"error": "Enrolled but no canonical embedding"})
        _, canonical_emb = canonical
        similarity = _cosine_similarity(canonical_emb, incoming)
        return {
            "similarity": round(similarity, 4),
            "recognized": similarity >= VERIFY_THRESHOLD,
            "threshold": VERIFY_THRESHOLD,
        }

    return router
