#!/bin/zsh
# Start JARVIS (backend + frontend) if it is not already running, and open it
# in Chrome. Safe to run repeatedly: a running instance is left alone.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/logs
started=0
if ! curl -sk -o /dev/null https://localhost:8340/api/health; then
  nohup .venv/bin/python server.py --host 127.0.0.1 > data/logs/server.log 2>&1 &
  started=1
fi
if ! curl -s -o /dev/null http://localhost:5173; then
  (cd frontend && nohup npx vite > ../data/logs/vite.log 2>&1 &)
  started=1
fi
if [ "$started" = 1 ]; then
  for _ in {1..30}; do curl -s -o /dev/null http://localhost:5173 && break; sleep 1; done
  open -a "Google Chrome" http://localhost:5173
fi
