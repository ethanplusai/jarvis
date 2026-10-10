"""Non-streaming TTS transport, separate from turn and usage orchestration."""
import logging
import httpx

log = logging.getLogger("jarvis.tts")


async def synthesize(text, *, key, voice, model, endpoint):
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(endpoint, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json", "model": model},
                json={"text": text, "reference_id": voice, "format": "mp3"})
        if response.status_code == 200:
            return response.content
        log.error("TTS returned HTTP %s", response.status_code)
    except Exception:
        # Transport exceptions may carry request URLs/headers; don't log secrets.
        log.error("TTS transport failed")
    return None
