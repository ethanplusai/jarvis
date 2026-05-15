# JARVIS

**Just A Rather Very Intelligent System.**

A voice-first AI assistant that runs on your Mac. Talk to it, and it talks back -- with a British accent, dry wit, and an audio-reactive particle orb straight out of the MCU.

JARVIS connects to your Apple Calendar, Mail, and Notes. It can browse the web, spawn Claude Code sessions to build entire projects, and plan your day -- all through natural voice conversation.

> "Will do, sir."

<!-- TODO: Add demo GIF or screenshot here -->
<!-- ![JARVIS Demo](docs/demo.gif) -->

---

## What It Does

- **Voice conversation** -- speak naturally, get spoken responses with a JARVIS voice
- **Builds software** -- say "build me a landing page" and watch Claude Code do the work
- **Reads your calendar** -- "What's on my schedule today?"
- **Reads your email** -- "Any unread messages?" (read-only, by design)
- **Browses the web** -- "Search for the best restaurants in Austin"
- **Manages tasks** -- "Remind me to call the client tomorrow"
- **Takes notes** -- "Save that as a note"
- **Remembers things** -- "I prefer React over Vue" (it remembers next time)
- **Plans your day** -- combines calendar, tasks, and priorities into a plan
- **Sees your screen** -- knows what apps are open for context-aware responses
- **Audio-reactive orb** -- a Three.js particle visualization that pulses with JARVIS's voice

## Requirements

- **macOS** (uses AppleScript for Calendar, Mail, Notes integration)
- **Python 3.11+**
- **Node.js 18+**
- **Google Chrome** (required for Web Speech API)
- **Anthropic API key** -- powers the AI brain ([get one here](https://console.anthropic.com/))
- **Fish Audio API key** -- powers the voice ([get one here](https://fish.audio/))
- **Claude Code CLI** -- for spawning dev tasks ([install here](https://docs.anthropic.com/en/docs/claude-code))

## Quick Start (with Claude Code)

The fastest way to get running:

```bash
git clone https://github.com/yourusername/jarvis.git
cd jarvis
claude
```

Claude Code will read the project's `CLAUDE.md` and walk you through setup step by step -- API keys, dependencies, SSL certs, everything.

## Manual Setup

```bash
# 1. Clone the repo
git clone https://github.com/yourusername/jarvis.git
cd jarvis

# 2. Set up environment
cp .env.example .env
# Edit .env with your API keys (see below)

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install frontend dependencies
cd frontend && npm install && cd ..

# 5. Generate SSL certificates (needed for secure WebSocket)
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'

# 6. Start the backend (Terminal 1)
python server.py

# 7. Start the frontend (Terminal 2)
cd frontend && npm run dev

# 8. Open Chrome
open http://localhost:5173
```

Click the page once to enable audio, then speak. JARVIS will respond.

## Configuration

Edit your `.env` file:

```env
# Required
ANTHROPIC_API_KEY=your-anthropic-api-key-here
FISH_API_KEY=your-fish-audio-api-key-here

# Optional -- your name (JARVIS will address you personally)
USER_NAME=Tony

# Optional -- specific calendar accounts (comma-separated)
# Leave empty to auto-discover all calendars
CALENDAR_ACCOUNTS=you@gmail.com,work@company.com
```

## Local Access & Security

JARVIS can open terminals, start Claude Code sessions, restart itself, and update API keys, so the server is intentionally local-first. By default `python server.py` listens on `127.0.0.1` and the control API/WebSocket reject non-local clients.

If you deliberately expose JARVIS beyond your machine, set a long random token and send it as `Authorization: Bearer <token>` or `X-JARVIS-API-Token`. Browser WebSocket clients can pass the same token as `/ws/voice?token=<token>`:

```env
JARVIS_API_TOKEN=generate-a-long-random-token
```

Localhost browser origins are allowed automatically for development. To allow additional trusted cross-origin frontends, set:

```env
JARVIS_CORS_ORIGINS=https://trusted.example.com
```

## Architecture

```
Microphone -> Web Speech API -> WebSocket -> FastAPI -> Claude (Haiku) -> Fish Audio TTS -> WebSocket -> Speaker
                                                |
                                                v
                                        Claude Code Tasks
                                        (spawns real dev work)
                                                |
                                                v
                                        AppleScript Bridge
                                        (Calendar, Mail, Notes, Terminal)
```

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Python (`server.py`, ~2300 lines) |
| Frontend | Vite + TypeScript + Three.js |
| Communication | WebSocket (JSON messages + binary audio) |
| AI (fast) | Claude Haiku -- low-latency voice responses |
| AI (deep) | Claude Opus -- research and complex tasks |
| TTS | Fish Audio with JARVIS voice model |
| System | AppleScript for all macOS integrations |

## How the Voice Loop Works

1. You speak into your microphone
2. Chrome's Web Speech API transcribes your speech in real-time
3. The transcript is sent to the server via WebSocket
4. JARVIS detects intent -- conversation, action, or build request
5. For actions: spawns a Claude Code subprocess or runs AppleScript
6. Generates a response via Claude Haiku (optimized for speed)
7. Fish Audio converts the response to speech with the JARVIS voice
8. Audio streams back to the browser via WebSocket
9. The Three.js orb deforms and pulses in response to the audio
10. Background tasks notify you proactively when they complete

## Key Files

| File | Purpose |
|------|---------|
| `server.py` | Main server -- WebSocket handler, LLM, action system |
| `frontend/src/orb.ts` | Three.js particle orb visualization |
| `frontend/src/voice.ts` | Web Speech API + audio playback |
| `frontend/src/main.ts` | Frontend state machine |
| `memory.py` | SQLite memory system with FTS5 full-text search |
| `calendar_access.py` | Apple Calendar integration via AppleScript |
| `mail_access.py` | Apple Mail integration (read-only) |
| `notes_access.py` | Apple Notes integration |
| `actions.py` | System actions (Terminal, Chrome, Claude Code) |
| `browser.py` | Playwright web automation |
| `work_mode.py` | Persistent Claude Code sessions |
| `planner.py` | Multi-step task planning with smart questions |

## Features in Detail

### Action System
JARVIS uses action tags to trigger real system actions:
- `[ACTION:BUILD]` -- spawns Claude Code to build a project
- `[ACTION:BROWSE]` -- opens Chrome to a URL or search query
- `[ACTION:RESEARCH]` -- deep research with Claude Opus, outputs an HTML report
- `[ACTION:PROMPT_PROJECT]` -- connects to an existing project via Claude Code
- `[ACTION:ADD_TASK]` -- creates a tracked task with priority and due date
- `[ACTION:REMEMBER]` -- stores a fact for future context

### Memory System
JARVIS remembers things you tell it using SQLite with FTS5 full-text search. Preferences, decisions, and facts persist across sessions.

### Calendar & Mail
All macOS integrations use AppleScript -- no OAuth flows, no token management. Just native system access. Mail is intentionally read-only for safety.

## Contributing

Contributions are welcome. Some areas that could use work:

- **Linux/Windows support** -- replace AppleScript with cross-platform alternatives
- **Alternative TTS engines** -- add ElevenLabs, OpenAI TTS, or local models
- **Alternative LLMs** -- add OpenAI, Gemini, or local model support
- **Mobile client** -- a companion app for voice interaction on the go
- **Plugin system** -- make it easy to add new actions and integrations

Please open an issue before submitting large PRs so we can discuss the approach.

## License

Free for personal, non-commercial use. Commercial use requires a license — visit [ethanplus.ai](https://ethanplus.ai) for inquiries. See [LICENSE](LICENSE) for details.

## Credits

Built by [Ethan](https://ethanplus.ai).

Powered by [Anthropic Claude](https://anthropic.com) and [Fish Audio](https://fish.audio).

Inspired by the AI that started it all -- Tony Stark's JARVIS.

> **Disclaimer:** This is an independent fan project and is not affiliated with, endorsed by, or connected to Marvel Entertainment, The Walt Disney Company, or any related entities. The JARVIS name and character are property of Marvel Entertainment.
