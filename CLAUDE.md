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

## Architecture
- **Backend**: FastAPI + Python — `server.py` (~620 lines, app wiring + WebSocket voice handler) plus four packages
- **Frontend**: Vite + TypeScript + Three.js (audio-reactive orb)
- **Communication**: WebSocket (JSON messages + binary audio)
- **AI**: Claude Haiku for fast responses, Claude Opus for research
- **TTS**: Fish Audio with JARVIS voice model
- **System**: AppleScript for Calendar, Mail, Notes, Terminal integration

## Key Files / Packages
- `server.py` — FastAPI app, lifespan, WebSocket voice handler, app wiring
- `voice/` — everything the voice handler delegates to: chat/work/planning mode helpers, `[ACTION:*]` dispatch, fast keyword detection, background lookups, claude -p dispatch, TTS
- `api/` — REST router factories (`core`, `settings`, `control`) mounted on the FastAPI app
- `macos/` — AppleScript access: `calendar_access`, `mail_access`, `notes_access`, `screen`, `actions`
- `feedback/` — task-outcome loops: `SuccessTracker`, `ABTester`, `UsageLearner`
- `llm.py` — Anthropic call + system prompt assembly
- `planner.py` — clarifying-question flow for complex tasks
- `task_manager.py` — background `claude -p` subprocess manager
- `memory.py` — SQLite memory system with FTS5 full-text search
- `mc_client.py` / `mc_inbox.py` — Mission Control REST client + inbox watcher
- `work_mode.py` — persistent Claude Code tmux sessions
- `browser.py` — Playwright web automation
- `frontend/src/orb.ts` — Three.js particle orb visualization
- `frontend/src/voice.ts` — Web Speech API + audio playback
- `frontend/src/main.ts` — Frontend state machine

## Environment Variables
- `ANTHROPIC_API_KEY` (required) — Claude API access
- `FISH_API_KEY` (required) — Fish Audio TTS
- `FISH_VOICE_ID` (optional) — Voice model ID
- `USER_NAME` (optional) — Your name for JARVIS to use
- `CALENDAR_ACCOUNTS` (optional) — Comma-separated calendar emails

## Conventions
- JARVIS personality: British butler, dry wit, economy of language
- Max 1-2 sentences per voice response
- Action tags: [ACTION:BUILD], [ACTION:BROWSE], [ACTION:RESEARCH], etc.
- AppleScript for all macOS integrations (no OAuth needed)
- Read-only for Mail (safety by design)
- SQLite for all local data storage
