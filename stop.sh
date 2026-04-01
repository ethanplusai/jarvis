#!/bin/bash
# ════════════════════════════════════════════════════════════════
# JARVIS — Stop Script
# ════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  J.A.R.V.I.S. Shutting Down..."
echo ""

# Stop Docker container
echo "  [docker] Stopping container..."
docker compose down 2>/dev/null || true

# Stop macOS bridge
BRIDGE_PID_FILE="$SCRIPT_DIR/.bridge.pid"
if [ -f "$BRIDGE_PID_FILE" ]; then
  BRIDGE_PID=$(cat "$BRIDGE_PID_FILE")
  if kill -0 "$BRIDGE_PID" 2>/dev/null; then
    kill "$BRIDGE_PID"
    echo "  [bridge] Stopped bridge (PID $BRIDGE_PID)"
  fi
  rm -f "$BRIDGE_PID_FILE"
fi

echo "  ✓ JARVIS stopped."
echo ""
