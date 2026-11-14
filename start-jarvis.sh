#!/bin/bash
export PATH="/opt/homebrew/bin:$PATH"
exec > >(tee -a "$(dirname "$0")/data/jarvis/jarvis.log") 2>&1
cd "$(dirname "$0")" && source .venv/bin/activate
pkill -f "python clap_listener.py"; python clap_listener.py &
python server.py --host 127.0.0.1 & (cd frontend && npm run dev) & wait
