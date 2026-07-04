# JARVIS — Agent Operating Instructions

## Mission
JARVIS is a local, voice-first desktop assistant by AI Evolution Labs. It combines a browser HUD, WebSocket voice loop, configurable LLM/TTS providers, persistent SQLite memory, installable skills, desktop integrations, and Claude Code project dispatch.

Your job as a coding agent is to keep the system simple to run, safe with secrets, and honest about supported connectors.

## Fast start for a fresh clone

### Windows
1. Open PowerShell in the repository root.
2. Run `.\start.ps1`.
3. Let the launcher copy `.env.example` to `.env`, install dependencies, install the desktop shortcut, and start backend/frontend terminals.
4. Open Chrome at `http://localhost:5180` or the Vite URL printed in the terminal.
5. Add API keys in Settings → Required APIs / Model & Voice.

### macOS / Linux
1. Open a terminal in the repository root.
2. Run `./start.sh`.
3. Let the launcher copy `.env.example` to `.env`, install dependencies, install the desktop shortcut, and start backend/frontend processes.
4. Open Chrome at `http://localhost:5180` or the Vite URL printed in the terminal.
5. Add API keys in Settings.

### Manual start
```bash
cp .env.example .env
python -m pip install -r requirements.txt
cd frontend && npm install && cd ..
python server.py
```
Second terminal:
```bash
cd frontend
npm run dev
```

## Supported connectors only
- LLM: `anthropic`, `openai`, `deepseek`, `google`, `ollama`.
- TTS: `fish_audio`, `elevenlabs`.
- Do not re-add Perplexity, Groq, or Hermes unless the project gains real runtime adapters and tests.
- Hermes is intentionally absent because this repository has no Hermes API connector.
- DeepSeek defaults to `deepseek-v4-pro`; keep `deepseek-v4-flash` available.
- OpenAI defaults to `gpt-5.2`; normalize spoken/legacy “GPT 5-5” style aliases rather than sending unsupported model IDs.

## Architecture map
- `server.py` — FastAPI app, WebSocket loop, settings endpoints, prompt assembly, action dispatch.
- `providers/config.py` — single source of truth for LLM/TTS provider metadata.
- `providers/llm.py` — unified completion router for Anthropic, OpenAI-compatible providers, Gemini, and Ollama.
- `providers/tts.py` — Fish Audio and ElevenLabs synthesis router.
- `integrations.py` — onboarding/settings connector catalog.
- `memory.py` — SQLite memories, tasks, notes, FTS search, and memory context injected into prompts.
- `skills.py` — bundled skill catalog, enabled-skill prompt injection, executable artifact handlers.
- `onboarding.py` — first-run profile discovery and skill recommendations.
- `mcp_registry.py` — curated MCP connector catalog, connection state, auth env keys.
- `system_control.py` — cross-platform desktop control (apps, URLs, paths, volume, media, lock, clipboard, screenshots) with pure per-platform command builders and graceful degradation.
- `frontend/src/settings.ts` — settings/onboarding UI and provider status rendering.
- `frontend/src/orb.ts` — Three.js audio-reactive orb.
- `actions.py`, `work_mode.py`, `dispatch_registry.py` — desktop actions and Claude Code dispatch.
- `scripts/generate_icon.py`, `scripts/install_desktop_shortcut.*` — brand icon generation and desktop shortcut installers.

## Memory and skill rules
- Memory is local/private SQLite state in `data/jarvis.db`; never commit `data/` artifacts or secrets.
- Memory should store durable facts only: preferences, decisions, project facts, people, plans, dates, and user goals.
- Enabled skills are prompt instructions, not magic capabilities. If no enabled skill fits, suggest enabling a relevant available skill.
- Executable skills must use `[ACTION:RUN_SKILL] slug ||| {json params}` and save artifacts under `data/artifacts/`.
- Keep skill prompt text compact; the voice response budget is short.

## Runtime personality conventions
- JARVIS voice responses should be one sentence when possible, two maximum.
- Style: elegant British butler, calm, loyal, concise, dry wit.
- Do not use markdown in spoken responses.
- Use action tags only when work should actually be done.

## Security and configuration
- Secrets belong only in `.env`; `.env.example` must contain placeholders only.
- Settings API should allow only whitelisted environment keys (provider keys, personalization, MCP auth tokens, weather).
- Mail access is read-only by design.
- Non-macOS platforms should report unavailable Apple integrations gracefully; generic desktop control must route through `system_control.py`, never raise, and reply with a butler-style "not available on this platform" message when unsupported.

## Checks before handing off
```bash
python -m compileall server.py providers integrations.py memory.py skills.py actions.py system_control.py mcp_registry.py
python tests/test_providers.py
python tests/test_system_control.py
python tests/test_skills_handlers.py
cd frontend && npm run build
```

If dependencies are missing, state exactly which command failed and why.
