# Ngrok Setup for Raspberry Pi (NEUROSENSE)

Expose the Flask dashboard (port 5000) via a public URL so phones can scan QR codes from anywhere — not just the local WiFi.

---

## Prerequisites

- Raspberry Pi 5, ARM64, Raspberry Pi OS
- Internet connection
- ngrok free account: https://dashboard.ngrok.com/signup
- Project deployed at `/home/pi/neurovelis/`
- Python venv at `/home/pi/neurovelis/.venv/`

---

## 1. Install ngrok (ARM64)

```bash
# Download and install
curl -fsSL https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz -o /tmp/ngrok.tgz
tar -xzf /tmp/ngrok.tgz -C /tmp
sudo install -m 755 /tmp/ngrok /usr/local/bin/ngrok

# Verify
ngrok version
```

---

## 2. Authtoken Setup

Get your token at: https://dashboard.ngrok.com/get-started/your-authtoken

```bash
ngrok config add-authtoken YOUR_TOKEN_HERE

# Validate (must return no error)
ngrok config check
```

If `config check` fails → token is wrong. Re-run `add-authtoken` with correct token.

---

## 3. Manual Run (Test First)

```bash
ngrok http 5000 --web-addr=127.0.0.1:4040
```

In another terminal, verify tunnel is active:

```bash
curl http://127.0.0.1:4040/api/tunnels | python3 -m json.tool
```

Expected output includes a `public_url` like `https://abc-123.ngrok-free.app`.

---

## 4. Automated Run via systemd

### 4a. Write ngrok.service

```bash
sudo tee /etc/systemd/system/ngrok.service > /dev/null << 'EOF'
[Unit]
Description=ngrok tunnel for NEUROSENSE
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Environment=HOME=/home/pi
ExecStart=/usr/local/bin/ngrok http 5000 \
    --config=/home/pi/.config/ngrok/ngrok.yml \
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
```

### 4b. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable ngrok
sudo systemctl start ngrok
```

### 4c. Patch neurovelis.service (if not already done by installer)

Add `ngrok.service` to `After=` and `Wants=` so neurovelis waits for ngrok:

```bash
sudo sed -i \
    -e 's/^After=\(.*\)$/After=\1 ngrok.service/' \
    -e 's/^Wants=\(.*\)$/Wants=\1 ngrok.service/' \
    /etc/systemd/system/neurovelis.service
sudo systemctl daemon-reload
```

> **Note:** Uses `Wants=` not `Requires=` — neurovelis still starts if ngrok fails, falls back to LAN IP.

### 4d. One-shot automated install

Alternatively, run the included installer (handles all steps above):

```bash
cd /home/pi/neurovelis
sudo bash scripts/install_ngrok_raspi.sh
```

---

## 5. Verify Tunnel

```bash
# Service status
systemctl status ngrok

# Active tunnel URL
curl -s http://127.0.0.1:4040/api/tunnels | jq -r '.tunnels[0].public_url'

# Live logs
journalctl -u ngrok -f
```

---

## 6. How Backend Detects the URL

`scan_engine/result_freezer.py` → `_resolve_base_url()`:

1. Queries `http://127.0.0.1:4040/api/tunnels` (timeout 1s)
2. Retries up to 3× with 0.5s gap on failure (guards startup race condition)
3. Prefers HTTPS tunnel; falls back to first tunnel if no HTTPS
4. Caches result: 10s TTL for ngrok URLs, 30s for LAN IP
5. If ngrok unreachable after 3 attempts → falls back to LAN IP

QR URL format: `{base_url}/report/{scan_id}?token={token}`

---

## 7. QR Mode

### ngrok mode (default when ngrok is running)

- QR encodes: `https://abc-123.ngrok-free.app/report/...`
- Phone can access from **anywhere on the internet**
- No WiFi requirement for phone

### LAN mode (fallback when ngrok is not running)

- QR encodes: `http://192.168.x.x:5000/report/...`
- Phone must be on the **same WiFi network** as Raspberry Pi

### Check which mode is active

```bash
curl -s http://127.0.0.1:4040/api/tunnels | jq -r '.tunnels[0].public_url'
# Returns ngrok URL → ngrok mode
# Returns "null" or connection refused → LAN mode
```

Backend switches automatically within 10 seconds when ngrok starts or stops (cache TTL).

---

## 8. Troubleshooting

### ngrok service not starting

```bash
journalctl -u ngrok -n 50
```

Common causes:
- **ERR_NGROK_4018** — invalid authtoken. Re-run: `ngrok config add-authtoken YOUR_TOKEN`
- **Config not found** — missing `--config` flag or wrong path. Check `/home/pi/.config/ngrok/ngrok.yml` exists
- **Network not ready** — service starts before network. Check `After=network-online.target` is in service file

### Tunnel not detected by backend

```bash
# Check API is reachable
curl http://127.0.0.1:4040/api/tunnels

# If connection refused → ngrok not running or wrong --web-addr
systemctl status ngrok

# If returns empty tunnels array → ngrok starting up, wait 5s and retry
```

### QR not working on phone

| Symptom | Cause | Fix |
|---------|-------|-----|
| Page not loading (ngrok URL) | Phone has no internet | Connect phone to internet |
| Page not loading (LAN IP) | Phone on different network | Connect phone to same WiFi as Raspi |
| QR displays but scan fails | Tunnel URL changed after scan frozen | Trigger new scan |
| 403 / token error | Wrong token in URL | Token is per-scan; don't reuse old QR |

### Check current QR mode

```bash
# If this returns a URL → ngrok mode active
curl -s http://127.0.0.1:4040/api/tunnels | jq -r '.tunnels[0].public_url // "LAN mode"'
```

---

## 9. Notes

| Item | Value |
|------|-------|
| Flask port | `5000` |
| ngrok web API | `127.0.0.1:4040` (locked via `--web-addr`) |
| ngrok config | `/home/pi/.config/ngrok/ngrok.yml` |
| Internet required | Yes, for ngrok mode. LAN mode works offline. |
| QR fallback | Automatic — no manual intervention needed |
| ngrok free plan | 1 tunnel, random subdomain per session |
| URL changes on restart | Yes — new tunnel = new URL. QR frozen at scan time. |
