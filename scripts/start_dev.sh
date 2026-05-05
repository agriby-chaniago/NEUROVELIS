#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
NGROK_PID=""

cleanup() {
    [[ -n "$NGROK_PID" ]] && kill "$NGROK_PID" 2>/dev/null || true
    echo "[dev] ngrok stopped"
}
trap cleanup EXIT INT TERM

# Start ngrok — lock web API to 127.0.0.1:4040 (prevents port collision)
ngrok http 5000 --web-addr=127.0.0.1:4040 --log=stdout > /tmp/ngrok.log 2>&1 &
NGROK_PID=$!
echo "[dev] ngrok started (PID $NGROK_PID)"

sleep 3  # initial stability before polling

# Poll until tunnel API responds (max 15s)
for i in $(seq 1 15); do
    curl -sf http://127.0.0.1:4040/api/tunnels > /dev/null 2>&1 && break
    sleep 1
done

# Print active tunnel URL
echo "[dev] ngrok URL:"
curl -s http://127.0.0.1:4040/api/tunnels | jq -r '.tunnels[0].public_url // "no tunnel yet"'

# Start Flask app
cd "$PROJECT_DIR"
source .venv/bin/activate
python main.py "$@"
