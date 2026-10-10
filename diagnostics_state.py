"""Runtime observations shared by startup, speech, and the diagnostics router."""
import time

checks = []
checked_at = None
tts_status = "unchecked"
tts_checked_at = None


def record_checks(values):
    global checks, checked_at
    checks = list(values)
    checked_at = time.time()


def record_tts(ok):
    global tts_status, tts_checked_at
    tts_status = "ready" if ok else "unavailable"
    tts_checked_at = time.time()
