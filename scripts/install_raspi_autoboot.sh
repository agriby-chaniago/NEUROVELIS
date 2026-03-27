#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo:" >&2
  echo "  sudo bash scripts/install_raspi_autoboot.sh" >&2
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

if [[ -z "$RUN_HOME" ]]; then
  echo "Unable to resolve home directory for user: $RUN_USER" >&2
  exit 1
fi

PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
MAIN_PY="$PROJECT_DIR/main.py"
KIOSK_SCRIPT="$PROJECT_DIR/scripts/open_model_kiosk.sh"
SERVICE_FILE="/etc/systemd/system/neurosense.service"
AUTOSTART_FILE="/etc/xdg/lxsession/LXDE-pi/autostart"
USER_AUTOSTART_FILE="$RUN_HOME/.config/lxsession/LXDE-pi/autostart"
KIOSK_LINE="@$KIOSK_SCRIPT http://127.0.0.1:5000/model http://127.0.0.1:5000/health"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python venv executable not found: $PYTHON_BIN" >&2
  echo "Create venv first, for example:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f "$MAIN_PY" ]]; then
  echo "main.py not found in project: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -x "$KIOSK_SCRIPT" ]]; then
  chmod +x "$KIOSK_SCRIPT"
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=NEUROSENSE Sensor Data Collection & Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PYTHON_BIN $MAIN_PY
Restart=on-failure
RestartSec=5s
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Jakarta
StandardOutput=journal
StandardError=journal
SyslogIdentifier=neurosense

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now neurosense.service

if [[ -f "$AUTOSTART_FILE" ]]; then
  TARGET_AUTOSTART="$AUTOSTART_FILE"
else
  mkdir -p "$(dirname "$USER_AUTOSTART_FILE")"
  touch "$USER_AUTOSTART_FILE"
  chown -R "$RUN_USER:$RUN_USER" "$RUN_HOME/.config"
  TARGET_AUTOSTART="$USER_AUTOSTART_FILE"
fi

if ! grep -Fqx "$KIOSK_LINE" "$TARGET_AUTOSTART"; then
  echo "$KIOSK_LINE" >> "$TARGET_AUTOSTART"
fi

echo ""
echo "NEUROSENSE auto-boot setup complete."
echo "- systemd service  : $SERVICE_FILE"
echo "- kiosk autostart  : $TARGET_AUTOSTART"
echo ""
echo "Check service status with:"
echo "  sudo systemctl status neurosense.service"
echo ""
echo "Reboot to test full boot flow:"
echo "  sudo reboot"
