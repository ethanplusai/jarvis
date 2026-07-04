#!/usr/bin/env bash
# JARVIS macOS/Linux launcher — prepares .env, installs deps, and starts backend/frontend.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="$(command -v python3 || command -v python || true)"
if [[ -z "$PYTHON" ]]; then
  echo "Error: Python 3 is required but was not found. Install it from https://www.python.org/downloads/ and re-run ./start.sh" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Error: npm is required but was not found. Install Node.js from https://nodejs.org and re-run ./start.sh" >&2
  exit 1
fi

if [[ ! -f "$ROOT/.env" && -f "$ROOT/.env.example" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example. Add API keys in onboarding or edit .env manually."
fi

if [[ -x "$ROOT/scripts/install_desktop_shortcut.sh" ]]; then
  "$ROOT/scripts/install_desktop_shortcut.sh" || true
fi

"$PYTHON" -m pip install -r "$ROOT/requirements.txt"
(
  cd "$ROOT/frontend"
  npm install
)

"$PYTHON" "$ROOT/server.py" &
BACKEND_PID=$!
(
  cd "$ROOT/frontend"
  npm run dev
) &
FRONTEND_PID=$!

cat <<MSG
JARVIS is starting.
Open Chrome at http://localhost:5180 (or the Vite URL printed above).
Backend PID: $BACKEND_PID
Frontend PID: $FRONTEND_PID
Press Ctrl+C here to stop both.
MSG

trap 'kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true' INT TERM EXIT

# Best-effort: wait for the backend, then open the HUD in the default browser.
if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 20); do
    if curl -fs http://localhost:8340/api/health >/dev/null 2>&1; then
      if command -v open >/dev/null 2>&1; then
        open http://localhost:5180 || true
      elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open http://localhost:5180 || true
      fi
      break
    fi
    sleep 1
  done
fi

wait
