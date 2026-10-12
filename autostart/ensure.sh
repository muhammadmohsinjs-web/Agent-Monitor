#!/bin/sh
# Start the agent monitor if it isn't already running. Safe to call from any hook, any number of times.
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${CCVIZ_PORT:-8765}"
if curl -fsS -m 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then exit 0; fi
nohup python3 "$DIR/server.py" --port "$PORT" >"${TMPDIR:-/tmp}/cc-viz.log" 2>&1 &
exit 0
