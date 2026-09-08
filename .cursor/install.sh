#!/usr/bin/env bash
#
# Idempotent install script for the JARVIS / Friday voice assistant.
# Prepares the Python backend, the Vite/Three.js frontend, Playwright's
# Chromium (used by the web-browsing / research actions), and local TLS
# certificates for the HTTPS backend + secure WebSocket.
#
# NOTE: The macOS AppleScript integrations (Calendar, Mail, Notes, Terminal)
# only run on macOS. On a Linux Cloud Agent the backend and frontend run and
# the full voice-loop transport works; those macOS features degrade gracefully.
set -euo pipefail

# Always operate from the repository root (parent of the .cursor directory).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# System package: the default image's python3 lacks the venv/ensurepip module.
# ---------------------------------------------------------------------------
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv
fi

# ---------------------------------------------------------------------------
# Python backend dependencies (isolated in a virtualenv; .venv is gitignored).
# pytest / pytest-asyncio are required to run the repo's test suite.
# ---------------------------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt pytest pytest-asyncio

# ---------------------------------------------------------------------------
# Playwright Chromium (+ system deps) for browser.py web browsing / research.
# ---------------------------------------------------------------------------
playwright install --with-deps chromium

# ---------------------------------------------------------------------------
# Frontend dependencies.
# ---------------------------------------------------------------------------
( cd frontend && npm ci )

# ---------------------------------------------------------------------------
# Local TLS certificates for the HTTPS backend and secure WebSocket.
# Generated once; both files are gitignored.
# ---------------------------------------------------------------------------
if [ ! -f cert.pem ] || [ ! -f key.pem ]; then
  openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
    -days 365 -nodes -subj '/CN=localhost'
fi

echo "JARVIS environment ready."
