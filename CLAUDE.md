# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# JARVIS — Voice AI Assistant

## Overview
JARVIS (Just A Rather Very Intelligent System) is a voice-first AI assistant for macOS. It runs locally on your machine, connecting to your Apple Calendar, Mail, Notes, and can spawn Claude Code sessions for development tasks.

## Quick Start
When a user clones this repo and starts Claude Code, help them:
1. Copy .env.example to .env
2. Get an Anthropic API key from console.anthropic.com
3. Get a Fish Audio API key from fish.audio
4. Install Python dependencies: pip install -r requirements.txt
5. Install frontend dependencies: cd frontend && npm install
6. Generate SSL certs: openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'
7. Run the backend: python server.py
8. Run the frontend: cd frontend && npm run dev
9. Open Chrome to http://localhost:5173
10. Click to enable audio, speak to JARVIS

## Commands
Run the app in two terminals: `python server.py` (backend, secure WebSocket — needs `cert.pem`/`key.pem`) and `cd frontend && npm run dev` (frontend on http://localhost:5173, must be Chrome for the Web Speech API).

Frontend build/typecheck: `cd frontend && npm run build` (runs `tsc` then `vite build`).

Tests live in `tests/` in two styles, and most call the real Anthropic API, so `ANTHROPIC_API_KEY` must be set (tests self-load `.env`):
- pytest suites: `pytest tests/`; single test by name: `pytest tests/test_e2e_pipeline.py -k <name>`
- standalone scripts (have `__main__`): `python3 tests/test_classifier.py`
- `pytest`/`pytest-asyncio` are NOT in `requirements.txt` — install them separately to run the pytest suites.

Live quality monitor (run alongside the server): `python monitor.py` tails server logs and flags low-quality conversations.

## Architecture
- **Backend**: FastAPI + Python (server.py, ~2700 lines)
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI**: Claude Haiku for fast responses, Claude Opus for research
- **TTS**: Fish Audio with JARVIS voice model
- **System**: AppleScript for Calendar, Mail, Notes, Terminal integration

### Request pipeline
`server.py` is an intentional ~2700-line monolith (see CONTRIBUTING.md) and is the orchestrator; the `/ws/voice` handler is the core loop:
1. Frontend captures mic audio (`audio_capture.ts`, MediaRecorder + VAD) and streams each utterance as binary over the WebSocket. The backend transcribes it via a local Whisper service (`whisper_service.py`, runs in `whisper-venv` / Python 3.12 on :8765) which also returns the language. A legacy browser-Web-Speech transcript path still exists as a fallback.
2. `classify_intent()` calls Haiku (`claude-haiku-4-5-20251001`) to pick an intent and emit an `[ACTION:*]` tag.
3. `execute_action` (actions.py) routes the tag to a system integration or a Claude Code spawn.
4. Reply text → Fish Audio TTS → streamed back as binary audio while the orb reacts.

Heavier paths use bigger models: deep research uses Opus (`claude-opus-4-6`) to write an HTML report, open it in the browser, and speak a Haiku summary; rolling session summaries run on Haiku in the background. Adding a capability usually means a new action tag + a classifier prompt update + a handler.

### Two ways to spawn Claude Code
- **Build dispatch** (`actions.py` + `dispatch_registry.py`): one-shot `claude -p` builds; `dispatch_registry` persists what's building / just-finished so JARVIS knows what "it" refers to.
- **Work mode** (`work_mode.py`): persistent sessions tied to a project dir, resumed with `--continue`. `planner.py` runs a conversational plan→clarify→confirm flow before spawning.

### Self-improvement loop
A feedback system tunes the prompts sent to Claude Code (only makes sense read together): `templates.py` (prompt templates by task type) → `ab_testing.py` (assigns template versions) → `qa.py` (spawns `claude -p` to verify output, auto-retries) → `tracking.py` (success rates) → `evolution.py` (analyzes failures, generates improved template versions) → `learning.py` (request patterns / context pre-loading) → `suggestions.py` (one heuristic follow-up per task). `conversation.py` holds multi-turn planning context.

### Storage — two separate SQLite DBs
These are NOT shared; confirm which one a module uses before touching persistence:
- `data/jarvis.db` — `memory.py` (FTS5 full-text memory) and `dispatch_registry.py`
- `jarvis_data.db` (repo root) — `tracking.py`, `learning.py`, `ab_testing.py`, `evolution.py`

## Key Files
- `server.py` — Main server, WebSocket handler, LLM integration, action system
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + audio playback
- `frontend/src/main.ts` — Frontend state machine
- `memory.py` — SQLite memory system with FTS5 search
- `calendar_access.py` — Apple Calendar integration via AppleScript
- `mail_access.py` — Apple Mail integration (READ-ONLY)
- `notes_access.py` — Apple Notes integration
- `actions.py` — System actions (Terminal, Chrome, Claude Code)
- `browser.py` — Playwright web automation
- `work_mode.py` — Persistent Claude Code sessions

## Environment Variables
- `ANTHROPIC_API_KEY` (required) — Claude API access
- `FISH_API_KEY` (required) — Fish Audio TTS
- `FISH_VOICE_ID` (optional) — Voice model ID
- `USER_NAME` (optional) — Your name for JARVIS to use
- `CALENDAR_ACCOUNTS` (optional) — Comma-separated calendar emails (empty = auto-discover all)
- `JARVIS_SKIP_PERMISSIONS` (optional) — Defaults to `true`; the voice loop can't answer interactive `claude` permission prompts (they'd hang the subprocess). Set `false` only when running in a visible Terminal.
- Weather overrides (optional): `WEATHER_LOCATION_LABEL`, `WEATHER_LATITUDE`, `WEATHER_LONGITUDE`, `WEATHER_UNIT` — defaults to public-IP geolocation, Fahrenheit.

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- Action tags: [ACTION:BUILD], [ACTION:BROWSE], [ACTION:RESEARCH], [ACTION:SCREEN], [ACTION:CAMERA], [ACTION:SENTIMENT], etc.
- Market sentiment ([ACTION:SENTIMENT] / `_do_sentiment_lookup`): runs the external kukapay `market-sentiment` skill analyzer as a subprocess and speaks a one-line mood score. The script lives outside the repo at `~/bybit-mcp/.agents/skills/market-sentiment/scripts/sentiment_analyzer.py` and needs `requests`, so it's invoked with `SENTIMENT_PYTHON` (defaults to the bybit-mcp venv). Override both via `SENTIMENT_PYTHON` / `SENTIMENT_SCRIPT` env vars. News-based only — never present as trading advice.
- Multilingual voice (English/French/Turkish): a top-left EN/FR/TR toggle sends `{type:"set_lang"}`; the chosen language is FORCED for Whisper transcription, the LLM reply, and the TTS voice (auto-detect proved unreliable on short utterances). Per-language Fish voices live in `_LANG_VOICE` — French and Turkish use private cloned voices (native speakers), English uses the MCU JARVIS voice. `whisper_service.py` peak-normalizes audio and accepts `?lang=` to force a language. Start it with `WHISPER_MODEL=base` for speed or `small` (default) for accuracy.
- Camera (`camera.py`): on-demand single-frame webcam vision. The frame lives in the browser, so the backend requests it over the WebSocket (`{"type":"capture_camera"}`) and the frontend (`frontend/src/camera.ts`) captures one JPEG, **releases the camera immediately**, and replies (`{"type":"camera_frame"}`). Privacy by design — never a continuous feed, nothing recorded. Distinct from screen vision (`screen.py`), which is captured server-side.
- AppleScript for all macOS integrations (no OAuth needed); all user-controlled strings MUST pass through `applescript_escape()` (actions.py) — injection guard, covered by `tests/test_applescript_escape.py`
- Read-only for Mail (safety by design) — never add write paths to connected services (Mail, Calendar, Notes)
- No telemetry/analytics; no external services beyond Anthropic and Fish Audio
- SQLite for all local data storage
