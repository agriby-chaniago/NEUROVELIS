#!/usr/bin/env bash
set -euo pipefail

MODEL_URL="${1:-http://127.0.0.1:5000/model}"
HEALTH_URL="${2:-http://127.0.0.1:5000/health}"
MAX_WAIT_SEC="${MAX_WAIT_SEC:-90}"

choose_browser() {
  if command -v chromium-browser >/dev/null 2>&1; then
    command -v chromium-browser
    return
  fi
  if command -v chromium >/dev/null 2>&1; then
    command -v chromium
    return
  fi
  return 1
}

wait_for_backend() {
  local i
  for ((i = 1; i <= MAX_WAIT_SEC; i++)); do
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
        return 0
      fi
    elif command -v wget >/dev/null 2>&1; then
      if wget -qO- "$HEALTH_URL" >/dev/null 2>&1; then
        return 0
      fi
    else
      # If neither curl nor wget is present, do a fixed delay and continue.
      sleep 8
      return 0
    fi
    sleep 1
  done
  return 0
}

BROWSER_BIN="$(choose_browser || true)"
if [[ -z "$BROWSER_BIN" ]]; then
  echo "Chromium browser not found. Install with: sudo apt install -y chromium-browser" >&2
  exit 1
fi

wait_for_backend

exec "$BROWSER_BIN" \
  --kiosk \
  --incognito \
  --no-first-run \
  --disable-sync \
  --noerrdialogs \
  --disable-infobars \
  --password-store=basic \
  --disable-features=PasswordManagerOnboarding \
  "$MODEL_URL"
