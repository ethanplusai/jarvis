#!/bin/bash
export PATH="/opt/homebrew/bin:$PATH"
exec > >(tee -a "$(dirname "$0")/data/jarvis/jarvis.log") 2>&1
cd "$(dirname "$0")" && source .venv/bin/activate

# Auto-restart: the server runs in the foreground; when it stops (a crash), the
# clap listener and Vite are stopped too and everything starts again. A crash
# soon after starting waits longer each time (10 s up to 2 min) so a broken
# setup does not spin; a run that lasted 5+ minutes resets the wait.
# Quitting Jarvis.app sends TERM to this whole process group, which ends it all.
delay=10
while true; do
  started=$(date +%s)
  pkill -f "python clap_listener.py"; python clap_listener.py &
  clap=$!
  (cd frontend && npm run dev) &
  vite=$!
  python server.py --host 127.0.0.1
  kill $clap $vite 2>/dev/null
  pkill -f "jarvis/frontend/node_modules/.bin/vite"
  wait
  [ $(( $(date +%s) - started )) -ge 300 ] && delay=10
  echo "$(date '+%F %T') [start-jarvis] JARVIS stopped; restarting in ${delay}s"
  sleep $delay
  [ $delay -lt 120 ] && delay=$(( delay * 2 ))
done
