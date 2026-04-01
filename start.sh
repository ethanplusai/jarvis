#!/bin/bash
# ════════════════════════════════════════════════════════════════
# JARVIS — Start Script
#
# Runs the macOS bridge natively + starts the Docker container.
# Access JARVIS at: http://localhost:5180
# ════════════════════════════════════════════════════════════════

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  J.A.R.V.I.S. Starting Up..."
echo ""

# ── 1. Validate .env ──────────────────────────────────────────────
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo "  [!] Created .env from .env.example — add your API keys first!"
    echo "      Edit .env and run ./start.sh again."
    exit 1
  else
    echo "  [!] .env file missing. Copy .env.example and fill in your keys."
    exit 1
  fi
fi

# Check for required keys
if grep -q "your-anthropic-api-key-here" .env 2>/dev/null; then
  echo "  [!] ANTHROPIC_API_KEY not set. Edit .env before starting."
  exit 1
fi

# ── 2. Stop existing macOS bridge if running ─────────────────────
BRIDGE_PID_FILE="$SCRIPT_DIR/.bridge.pid"
if [ -f "$BRIDGE_PID_FILE" ]; then
  OLD_PID=$(cat "$BRIDGE_PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
    echo "  [bridge] Stopped old bridge (PID $OLD_PID)"
  fi
  rm -f "$BRIDGE_PID_FILE"
fi

# ── 3. Start macOS bridge natively ──────────────────────────────
# The bridge provides Calendar, Mail, Notes, Terminal, Chrome via HTTP
# so the Docker container can use them via host.docker.internal:8341
echo "  [bridge] Starting macOS bridge on port 8341..."
python3 macos_bridge.py > /tmp/jarvis-bridge.log 2>&1 &
BRIDGE_PID=$!
echo $BRIDGE_PID > "$BRIDGE_PID_FILE"
echo "  [bridge] Running (PID $BRIDGE_PID) — logs: /tmp/jarvis-bridge.log"

# Wait briefly for bridge to come up
sleep 2

# ── 4. Build and start Docker container ──────────────────────────
echo "  [docker] Building image..."
docker compose build --quiet

echo "  [docker] Starting container (restart: unless-stopped)..."
docker compose up -d

echo ""
echo "  ✓ JARVIS is running at: http://localhost:5180"
echo "  ✓ macOS bridge:         http://localhost:8341/health"
echo ""
echo "  Commands:"
echo "    docker compose logs -f     # live server logs"
echo "    cat /tmp/jarvis-bridge.log # bridge logs"
echo "    ./stop.sh                  # stop everything"
echo ""
