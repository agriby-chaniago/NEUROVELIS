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

resolve_python_bin() {
  local candidates=()
  local arg_path="${1:-}"

  # Priority:
  # 1) First arg to script
  # 2) VENV_PYTHON env
  # 3) PYTHON_BIN env
  # 4) Active VIRTUAL_ENV (if preserved with sudo -E)
  # 5) Common local venv paths
  if [[ -n "$arg_path" ]]; then
    if [[ "$arg_path" == */bin/activate && -f "$arg_path" ]]; then
      candidates+=("${arg_path%/activate}/python")
    else
      candidates+=("$arg_path")
    fi
  fi
  if [[ -n "${VENV_PYTHON:-}" ]]; then
    candidates+=("$VENV_PYTHON")
  fi
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("$PYTHON_BIN")
  fi
  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    candidates+=("$VIRTUAL_ENV/bin/python")
  fi

  candidates+=(
    "$PROJECT_DIR/.venv/bin/python"
    "$RUN_HOME/neurosense-env/bin/python"
    "$RUN_HOME/.venv/bin/python"
  )

  local py
  for py in "${candidates[@]}"; do
    if [[ -x "$py" ]]; then
      echo "$py"
      return 0
    fi
  done

  return 1
}

PYTHON_BIN="$(resolve_python_bin "${1:-}" || true)"
VENV_ACTIVATE=""
MAIN_PY="$PROJECT_DIR/main.py"
KIOSK_SCRIPT="$PROJECT_DIR/scripts/open_model_kiosk.sh"
SERVICE_FILE="/etc/systemd/system/neurosense.service"
BOOT_CONFIG_FILE=""
BOOT_CONFIG_UPDATED=0
AUTOSTART_FILE="/etc/xdg/lxsession/LXDE-pi/autostart"
USER_AUTOSTART_FILE="$RUN_HOME/.config/lxsession/LXDE-pi/autostart"
DESKTOP_AUTOSTART_DIR="$RUN_HOME/.config/autostart"
DESKTOP_AUTOSTART_FILE="$DESKTOP_AUTOSTART_DIR/neurosense-kiosk.desktop"
KIOSK_LINE="@$KIOSK_SCRIPT http://127.0.0.1:5000/model http://127.0.0.1:5000/health"

ensure_buzzer_boot_default_low() {
  local pin="${1:-5}"
  local line="gpio=${pin}=op,dl"
  local cfg

  for cfg in /boot/firmware/config.txt /boot/config.txt; do
    if [[ -f "$cfg" ]]; then
      BOOT_CONFIG_FILE="$cfg"
      break
    fi
  done

  if [[ -z "$BOOT_CONFIG_FILE" ]]; then
    echo "Warning: Raspberry Pi boot config file not found; skipping GPIO boot default." >&2
    return 1
  fi

  if ! grep -Fqx "$line" "$BOOT_CONFIG_FILE"; then
    {
      echo ""
      echo "# NEUROSENSE: keep buzzer pin low during boot"
      echo "$line"
    } >> "$BOOT_CONFIG_FILE"
    BOOT_CONFIG_UPDATED=1
  fi

  return 0
}

if [[ -z "${PYTHON_BIN:-}" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python virtualenv executable not found." >&2
  echo "Provide venv path explicitly, for example:" >&2
  echo "  sudo bash scripts/install_raspi_autoboot.sh /home/$RUN_USER/neurosense-env/bin/activate" >&2
  echo "Or via env var:" >&2
  echo "  sudo VENV_PYTHON=/home/$RUN_USER/neurosense-env/bin/python bash scripts/install_raspi_autoboot.sh" >&2
  exit 1
fi

VENV_ACTIVATE="$(dirname "$PYTHON_BIN")/activate"
if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "Virtualenv activate script not found: $VENV_ACTIVATE" >&2
  exit 1
fi

if [[ ! -f "$MAIN_PY" ]]; then
  echo "main.py not found in project: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -x "$KIOSK_SCRIPT" ]]; then
  chmod +x "$KIOSK_SCRIPT"
fi

BUZZER_GPIO_PIN="$($PYTHON_BIN - <<'PY'
import config
print(getattr(config, "BUZZER_GPIO_PIN", 5))
PY
)"

if [[ -z "$BUZZER_GPIO_PIN" ]]; then
  BUZZER_GPIO_PIN="5"
fi

ensure_buzzer_boot_default_low "$BUZZER_GPIO_PIN" || true

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=NEUROSENSE Sensor Data Collection & Dashboard
After=local-fs.target systemd-udev-settle.service
Wants=systemd-udev-settle.service

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
SupplementaryGroups=video render
PermissionsStartOnly=true
WorkingDirectory=$PROJECT_DIR
# Force buzzer pin LOW as early as possible to avoid unwanted tone at boot.
ExecStartPre=-/usr/bin/raspi-gpio set 5 op dl
ExecStart=/bin/bash -lc 'source "$VENV_ACTIVATE" && exec python "$MAIN_PY"'
Restart=on-failure
RestartSec=5s
# Keep buzzer pin LOW when service stops/shuts down.
ExecStopPost=-/usr/bin/raspi-gpio set 5 op dl
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

mkdir -p "$DESKTOP_AUTOSTART_DIR"
cat > "$DESKTOP_AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=NEUROSENSE Kiosk
Comment=Open NEUROSENSE /model dashboard in kiosk mode
Exec=$KIOSK_SCRIPT http://127.0.0.1:5000/model http://127.0.0.1:5000/health
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

chown -R "$RUN_USER:$RUN_USER" "$RUN_HOME/.config"

echo ""
echo "NEUROSENSE auto-boot setup complete."
echo "- systemd service  : $SERVICE_FILE"
echo "- venv activate    : $VENV_ACTIVATE"
if [[ -n "$BOOT_CONFIG_FILE" ]]; then
  echo "- boot gpio default: $BOOT_CONFIG_FILE (gpio=$BUZZER_GPIO_PIN=op,dl)"
  if [[ "$BOOT_CONFIG_UPDATED" -eq 1 ]]; then
    echo "  note: boot config updated, reboot is required to apply firmware-level GPIO default"
  fi
fi
echo "- kiosk autostart  : $TARGET_AUTOSTART"
echo "- desktop autostart: $DESKTOP_AUTOSTART_FILE"
echo ""
echo "Check service status with:"
echo "  sudo systemctl status neurosense.service"
echo ""
echo "Reboot to test full boot flow:"
echo "  sudo reboot"
