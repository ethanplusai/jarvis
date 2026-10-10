# Contributing to JARVIS

Thanks for your interest in contributing! Here's how to get involved.

## Getting Started

1. Fork the repo
2. Clone your fork
3. Follow the setup instructions in the README
4. Make your changes
5. Run `python -m pip install -r requirements-dev.txt -c constraints.txt`,
   `python -m playwright install chromium`, and `pytest`. In `frontend`, run
   `npm ci`, `npm test`, and `npm run build`.
6. Submit a PR

## What We're Looking For

- **Bug fixes** — if something's broken, fix it
- **New integrations** — Spotify, Slack, Notion, etc.
- **Desktop adapters** — macOS and Windows have separate adapters; Linux desktop support is still unavailable
- **Better error handling** — things fail silently in places
- **Voice improvements** — alternative TTS providers, better speech recognition
- **New actions** — extend what JARVIS can do

## Code Style

Yes, `server.py` is a 5,600-line monolith. It works. If you want to refactor parts into modules, that's welcome — just make sure nothing breaks.

- Keep voice responses short (1-2 sentences max)
- Don't add dependencies unless necessary
- Keep automated tests isolated from real Claude, microphones and desktop actions.
  Test subprocess ownership using harmless Python child processes. Deliberate
  manual voice/desktop checks supplement the offline suite.
- Keep the personality consistent — British butler, dry wit, economy of language

## What NOT to Do

- Don't add telemetry or analytics
- Don't send data to external services beyond the existing API calls (Fish Audio)
- Don't add features that modify or delete user data in the services somebody has connected
- Don't break the existing voice loop

## Reporting Issues

Open an issue with:
- What you expected to happen
- What actually happened
- Your OS and Python version
- Any error messages from the terminal

## Questions?

Open an issue or start a discussion. Keep it simple.
# Dependency and business integration changes

Use [the explicit dependency upgrade workflow](docs/dependency-upgrades.md).
New provider mutations must pass through the immutable Business approval ledger;
never add an MCP tool that approves or executes its own proposal. Test unknown
outcomes, concurrent approval, account/destination changes, and restore replay
prevention using mocked HTTP transports. Do not exercise paid accounts in tests.
See [architecture boundaries](docs/architecture-boundaries.md) for module contracts.
