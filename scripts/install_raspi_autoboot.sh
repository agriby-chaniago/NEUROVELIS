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
  local legacy_env_dir_name="neuro""sense-env"

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
    "$RUN_HOME/neurovelis-env/bin/python"
    "$RUN_HOME/$legacy_env_dir_name/bin/python"
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
SERVICE_FILE="/etc/systemd/system/neurovelis.service"
BOOT_CONFIG_FILE=""
BOOT_CONFIG_UPDATED=0
SHUTDOWN_HOOK_FILE="/usr/lib/systemd/system-shutdown/neurovelis-buzzer-low"
SHUTDOWN_HOOK_INSTALLED=0
LEGACY_OLD_BRAND_HEAD="neuro"
LEGACY_OLD_BRAND_TAIL="sense"
LEGACY_OLD_BRAND="${LEGACY_OLD_BRAND_HEAD}${LEGACY_OLD_BRAND_TAIL}"
LEGACY_SERVICE_NAME="${LEGACY_OLD_BRAND}.service"
LEGACY_SERVICE_FILE="/etc/systemd/system/${LEGACY_OLD_BRAND}.service"
LEGACY_SHUTDOWN_HOOK_FILE="/usr/lib/systemd/system-shutdown/${LEGACY_OLD_BRAND}-buzzer-low"
AUTOSTART_FILE="/etc/xdg/lxsession/LXDE-pi/autostart"
USER_AUTOSTART_FILE="$RUN_HOME/.config/lxsession/LXDE-pi/autostart"
DESKTOP_AUTOSTART_DIR="$RUN_HOME/.config/autostart"
DESKTOP_AUTOSTART_FILE="$DESKTOP_AUTOSTART_DIR/neurovelis-kiosk.desktop"
LEGACY_DESKTOP_AUTOSTART_FILE="$DESKTOP_AUTOSTART_DIR/${LEGACY_OLD_BRAND}-kiosk.desktop"
KIOSK_LINE="@$KIOSK_SCRIPT http://127.0.0.1:5000/model http://127.0.0.1:5000/health"

ensure_buzzer_boot_default_low() {
  local pin="${1:-5}"
  local line_pd="gpio=${pin}=ip,pd"
  local line_low="gpio=${pin}=op,dl"
  local cfg
  local need_pd=0
  local need_low=0

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

  if ! grep -Fqx "$line_pd" "$BOOT_CONFIG_FILE"; then
    need_pd=1
  fi
  if ! grep -Fqx "$line_low" "$BOOT_CONFIG_FILE"; then
    need_low=1
  fi

  if [[ "$need_pd" -eq 1 || "$need_low" -eq 1 ]]; then
    {
      echo ""
      echo "# NEUROVELIS: keep buzzer pin low during boot"
      if [[ "$need_pd" -eq 1 ]]; then
        echo "$line_pd"
      fi
      if [[ "$need_low" -eq 1 ]]; then
        echo "$line_low"
      fi
    } >> "$BOOT_CONFIG_FILE"
    BOOT_CONFIG_UPDATED=1
  fi

  return 0
}

install_shutdown_buzzer_hook() {
  local pin="$1"

  mkdir -p "$(dirname "$SHUTDOWN_HOOK_FILE")"
  cat > "$SHUTDOWN_HOOK_FILE" <<EOF
#!/bin/sh
# NEUROVELIS: force buzzer GPIO LOW at very late shutdown/reboot stage.
PIN="$pin"
if [ -x /usr/bin/raspi-gpio ]; then
  # Repeat sequence to reduce audible chirp during rail transition.
  /usr/bin/raspi-gpio set "\$PIN" op dl >/dev/null 2>&1 || true
  /usr/bin/raspi-gpio set "\$PIN" ip pd >/dev/null 2>&1 || true
  /usr/bin/raspi-gpio set "\$PIN" op dl >/dev/null 2>&1 || true
fi
exit 0
EOF
  chmod 0755 "$SHUTDOWN_HOOK_FILE"
  SHUTDOWN_HOOK_INSTALLED=1
}

resolve_buzzer_gpio_pin() {
  local py_bin="$1"
  local pin=""

  pin="$(
    cd "$PROJECT_DIR" &&
    PYTHONPATH="$PROJECT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$py_bin" - <<'PY'
try:
    import config
    print(getattr(config, "BUZZER_GPIO_PIN", 5))
except Exception:
    print(5)
PY
  )" || true

  echo "$pin"
}

cleanup_legacy_brand_artifacts() {
  # Stop and disable the legacy unit if it still exists from previous installs.
  systemctl disable --now "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true

  if [[ -f "$LEGACY_SERVICE_FILE" ]]; then
    rm -f "$LEGACY_SERVICE_FILE"
  fi
  if [[ -f "$LEGACY_SHUTDOWN_HOOK_FILE" ]]; then
    rm -f "$LEGACY_SHUTDOWN_HOOK_FILE"
  fi
  if [[ -f "$LEGACY_DESKTOP_AUTOSTART_FILE" ]]; then
    rm -f "$LEGACY_DESKTOP_AUTOSTART_FILE"
  fi
}

if [[ -z "${PYTHON_BIN:-}" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python virtualenv executable not found." >&2
  echo "Detected candidates checked: $PROJECT_DIR/.venv, $RUN_HOME/neurovelis-env, $RUN_HOME/.venv" >&2
  echo "Provide your existing venv path explicitly, for example:" >&2
  echo "  sudo bash scripts/install_raspi_autoboot.sh /home/$RUN_USER/neurovelis-env/bin/activate" >&2
  echo "Or via env var:" >&2
  echo "  sudo VENV_PYTHON=/home/$RUN_USER/neurovelis-env/bin/python bash scripts/install_raspi_autoboot.sh" >&2
  echo "To find available Python venv executables, run:" >&2
  echo "  ls -l $RUN_HOME/*env/bin/python $RUN_HOME/.venv/bin/python $PROJECT_DIR/.venv/bin/python 2>/dev/null" >&2
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

BUZZER_GPIO_PIN="$(resolve_buzzer_gpio_pin "$PYTHON_BIN")"
if [[ ! "$BUZZER_GPIO_PIN" =~ ^[0-9]+$ ]]; then
  BUZZER_GPIO_PIN="5"
fi

ensure_buzzer_boot_default_low "$BUZZER_GPIO_PIN" || true
install_shutdown_buzzer_hook "$BUZZER_GPIO_PIN"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=NEUROVELIS Sensor Data Collection & Dashboard
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
ExecStartPre=-/bin/sh -lc '/usr/bin/raspi-gpio set $BUZZER_GPIO_PIN ip pd >/dev/null 2>&1 || true; /usr/bin/raspi-gpio set $BUZZER_GPIO_PIN op dl >/dev/null 2>&1 || true'
ExecStart=/bin/bash -lc 'source "$VENV_ACTIVATE" && exec python "$MAIN_PY"'
Restart=on-failure
RestartSec=5s
# Keep buzzer pin LOW when service stops/shuts down.
ExecStopPost=-/bin/sh -lc '/usr/bin/raspi-gpio set $BUZZER_GPIO_PIN op dl >/dev/null 2>&1 || true; /usr/bin/raspi-gpio set $BUZZER_GPIO_PIN ip pd >/dev/null 2>&1 || true'
Environment=PYTHONUNBUFFERED=1
Environment=TZ=Asia/Jakarta
StandardOutput=journal
StandardError=journal
SyslogIdentifier=neurovelis

[Install]
WantedBy=multi-user.target
EOF

cleanup_legacy_brand_artifacts
systemctl daemon-reload
systemctl enable --now neurovelis.service

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
Name=NEUROVELIS Kiosk
Comment=Open NEUROVELIS /model dashboard in kiosk mode
Exec=$KIOSK_SCRIPT http://127.0.0.1:5000/model http://127.0.0.1:5000/health
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

chown -R "$RUN_USER:$RUN_USER" "$RUN_HOME/.config"

echo ""
echo "NEUROVELIS auto-boot setup complete."
echo "- systemd service  : $SERVICE_FILE"
echo "- venv activate    : $VENV_ACTIVATE"
if [[ -n "$BOOT_CONFIG_FILE" ]]; then
  echo "- boot gpio default: $BOOT_CONFIG_FILE (gpio=$BUZZER_GPIO_PIN=ip,pd + gpio=$BUZZER_GPIO_PIN=op,dl)"
  if [[ "$BOOT_CONFIG_UPDATED" -eq 1 ]]; then
    echo "  note: boot config updated, reboot is required to apply firmware-level GPIO default"
  fi
fi
if [[ "$SHUTDOWN_HOOK_INSTALLED" -eq 1 ]]; then
  echo "- shutdown hook    : $SHUTDOWN_HOOK_FILE"
fi
echo "- kiosk autostart  : $TARGET_AUTOSTART"
echo "- desktop autostart: $DESKTOP_AUTOSTART_FILE"
echo ""
echo "Check service status with:"
echo "  sudo systemctl status neurovelis.service"
echo ""
echo "Reboot to test full boot flow:"
echo "  sudo reboot"
