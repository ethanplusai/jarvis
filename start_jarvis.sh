#!/usr/bin/env bash
# Idempotently start the JARVIS backend + frontend and open Chrome.
# Safe to run repeatedly: it only starts what isn't already listening.
set -u

JARVIS_DIR="/Users/oguz/jarvis"
BACKEND_PORT=8340
FRONTEND_PORT=5173
URL="http://localhost:${FRONTEND_PORT}/"
LOG_DIR="${JARVIS_DIR}/.run"
mkdir -p "$LOG_DIR"

WHISPER_PORT=8765

port_up() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# Whisper STT service (separate Python 3.12 venv — faster-whisper has no 3.14 wheels)
if port_up "$WHISPER_PORT"; then
  echo "[jarvis] whisper already running on :$WHISPER_PORT"
elif [ -x "$JARVIS_DIR/whisper-venv/bin/python" ]; then
  echo "[jarvis] starting whisper on :$WHISPER_PORT"
  ( cd "$JARVIS_DIR" && nohup ./whisper-venv/bin/python whisper_service.py \
      >"$LOG_DIR/whisper.log" 2>&1 & )
else
  echo "[jarvis] whisper-venv missing; STT auto-detect disabled" >&2
fi

# Backend
if port_up "$BACKEND_PORT"; then
  echo "[jarvis] backend already running on :$BACKEND_PORT"
else
  echo "[jarvis] starting backend on :$BACKEND_PORT"
  ( cd "$JARVIS_DIR" && nohup ./venv/bin/python server.py \
      >"$LOG_DIR/backend.log" 2>&1 & )
fi

# Frontend
if port_up "$FRONTEND_PORT"; then
  echo "[jarvis] frontend already running on :$FRONTEND_PORT"
else
  echo "[jarvis] starting frontend on :$FRONTEND_PORT"
  ( cd "$JARVIS_DIR/frontend" && nohup npm run dev \
      >"$LOG_DIR/frontend.log" 2>&1 & )
fi

# Wait (bounded) for the frontend to accept connections, then open Chrome once.
for _ in $(seq 1 30); do
  port_up "$FRONTEND_PORT" && break
  sleep 0.5
done

if port_up "$FRONTEND_PORT"; then
  open -a "Google Chrome" "$URL"
  echo "[jarvis] opened $URL in Chrome"
else
  echo "[jarvis] frontend did not come up in time; not opening browser" >&2
fi
