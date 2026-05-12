#!/usr/bin/env bash
# install_ngrok_raspi.sh — one-shot ngrok setup for Raspberry Pi (ARM64)
# Run as root or with sudo. Installs binary, configures systemd, patches neurovelis.service.
set -euo pipefail

NGROK_BIN="/usr/local/bin/ngrok"
NGROK_URL="https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz"
SERVICE_FILE="/etc/systemd/system/ngrok.service"
NEUROVELIS_SERVICE="/etc/systemd/system/neurovelis.service"
NGROK_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"
NGROK_HOME=$(getent passwd "$NGROK_USER" | cut -d: -f6)
NGROK_CONFIG="$NGROK_HOME/.config/ngrok/ngrok.yml"

# ── 1. Install binary ─────────────────────────────────────────────────────────

if [[ -x "$NGROK_BIN" ]]; then
    echo "[info] ngrok already installed at $NGROK_BIN"
    "$NGROK_BIN" version
else
    echo "[info] Downloading ngrok ARM64..."
    TMP=$(mktemp -d)
    curl -fsSL "$NGROK_URL" -o "$TMP/ngrok.tgz"
    tar -xzf "$TMP/ngrok.tgz" -C "$TMP"
    install -m 755 "$TMP/ngrok" "$NGROK_BIN"
    rm -rf "$TMP"
    echo "[ok] ngrok installed → $NGROK_BIN"
    "$NGROK_BIN" version
fi

# ── 2. Authtoken ──────────────────────────────────────────────────────────────

echo ""
echo "Get your authtoken at: https://dashboard.ngrok.com/get-started/your-authtoken"
read -rp "Enter ngrok authtoken: " NGROK_TOKEN

if [[ -z "$NGROK_TOKEN" ]]; then
    echo "ERR: authtoken cannot be empty"
    exit 1
fi

# Register token as the service user (config lives in ~/.config/ngrok/)
sudo -u "$NGROK_USER" "$NGROK_BIN" config add-authtoken "$NGROK_TOKEN"

# Validate config
if ! sudo -u "$NGROK_USER" "$NGROK_BIN" config check > /dev/null 2>&1; then
    echo "ERR: ngrok config invalid — check authtoken (ERR_NGROK_4018)"
    exit 1
fi
echo "[ok] authtoken valid"

# ── 3. Write ngrok.service ────────────────────────────────────────────────────

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=ngrok tunnel for NEUROSENSE
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$NGROK_USER
Environment=HOME=$NGROK_HOME
ExecStart=/usr/local/bin/ngrok http 5000 \
    --config=$NGROK_HOME/.config/ngrok/ngrok.yml \
    --web-addr=127.0.0.1:4040 \
    --log=stdout
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ngrok

[Install]
WantedBy=multi-user.target
EOF

echo "[ok] ngrok.service written → $SERVICE_FILE"

# ── 4. Enable ngrok service ───────────────────────────────────────────────────

systemctl daemon-reload
systemctl enable ngrok
echo "[ok] ngrok.service enabled (will start on next boot)"

# ── 5. Patch neurovelis.service ───────────────────────────────────────────────

if [[ -f "$NEUROVELIS_SERVICE" ]]; then
    # Add ngrok.service to After= and Wants= if not already present
    if ! grep -q "ngrok.service" "$NEUROVELIS_SERVICE"; then
        sed -i \
            -e 's/^After=\(.*\)$/After=\1 ngrok.service/' \
            -e 's/^Wants=\(.*\)$/Wants=\1 ngrok.service/' \
            "$NEUROVELIS_SERVICE"
        systemctl daemon-reload
        echo "[ok] neurovelis.service patched — Wants ngrok.service"
    else
        echo "[skip] neurovelis.service already references ngrok.service"
    fi
else
    echo "[warn] $NEUROVELIS_SERVICE not found — run install_raspi_autoboot.sh first, then re-run this script"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "=== Setup complete ==="
echo "Start now:   systemctl start ngrok"
echo "Check:       systemctl status ngrok"
echo "Logs:        journalctl -u ngrok -f"
echo "Verify API:  curl http://127.0.0.1:4040/api/tunnels"
