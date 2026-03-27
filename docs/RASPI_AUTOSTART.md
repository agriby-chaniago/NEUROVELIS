# Raspberry Pi 5 Auto-Boot Setup

Dokumen ini membuat Raspberry Pi otomatis:

- menjalankan backend NEUROSENSE saat boot
- membuka dashboard `http://127.0.0.1:5000/model` dalam mode kiosk

## Prasyarat

- Raspberry Pi OS Desktop (agar browser bisa auto-open)
- Project sudah ada di Raspberry Pi (contoh: `/home/pi/NEUROSENSE`)
- Virtual environment sudah dibuat dan dependency sudah terpasang

Contoh install dependency:

```bash
cd /home/pi/NEUROSENSE
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Setup Otomatis (Disarankan)

Dari folder project di Raspberry Pi:

```bash
cd /home/pi/NEUROSENSE
sudo bash scripts/install_raspi_autoboot.sh
```

Jika venv berada di luar folder project, kirim path python venv secara eksplisit:

```bash
cd /home/neurosense/NEUROSENSE
sudo bash scripts/install_raspi_autoboot.sh /home/neurosense/neurosense-env/bin/activate
```

Alternatif lewat environment variable:

```bash
cd /home/neurosense/NEUROSENSE
sudo VENV_PYTHON=/home/neurosense/neurosense-env/bin/python bash scripts/install_raspi_autoboot.sh
```

Script ini otomatis:

- membuat `/etc/systemd/system/neurosense.service`
- `enable` + `start` service
- menambahkan autostart Chromium kiosk untuk `/model`
  pada `~/.config/lxsession/LXDE-pi/autostart` (legacy)
  dan `~/.config/autostart/neurosense-kiosk.desktop` (XDG desktop autostart)

## Cek Status

```bash
sudo systemctl status neurosense.service
journalctl -u neurosense.service -n 100 --no-pager
```

## Uji Boot Penuh

```bash
sudo reboot
```

Saat boot sukses:

- service NEUROSENSE aktif
- browser Chromium terbuka otomatis ke `/model`

## Catatan

- Jika mode kiosk tidak terbuka, pastikan Anda login ke sesi desktop (GUI).
- Jika backend lambat start, script kiosk menunggu endpoint `/health` sebelum membuka `/model`.
- Launcher kiosk sudah menjalankan Chromium dengan `--password-store=basic`
  agar popup unlock keyring tidak muncul.
- Jika tetap diminta password saat startup, biasanya desktop belum auto-login
  atau keyring Chromium masih terkunci dari konfigurasi lama.

### Troubleshooting Saat Reboot Tidak Auto-Buka Browser

1. Aktifkan desktop auto-login:

```bash
sudo raspi-config
```

Pilih: `System Options` → `Boot / Auto Login` → `Desktop Autologin`.

1. Cek file autostart terpasang:

```bash
ls -l ~/.config/autostart/neurosense-kiosk.desktop
```

1. Jika popup keyring minta password terus muncul, atur keyring agar tidak meminta
   password saat boot (misalnya lewat aplikasi Passwords and Keys / Seahorse).

### Troubleshooting Kamera Terdeteksi di OS, Tapi Tidak di App Saat Boot

Gejala umum:

- `rpicam-hello --list-cameras` menampilkan kamera
- dashboard menampilkan kamera tidak tersedia / model sering `NO_CAMERA`
- masalah muncul setelah service systemd aktif

Penyebab paling umum: service berjalan di virtualenv yang tidak memiliki
`picamera2` (paket ini biasanya terpasang di system Python via apt).

Solusi:

1. Set interpreter worker kamera ke system Python di `config.py`:

```python
CAMERA_WORKER_PYTHON = "/usr/bin/python3"
```

1. Restart service:

```bash
sudo systemctl restart neurosense.service
```

1. Verifikasi log:

```bash
journalctl -u neurosense.service -n 120 --no-pager
```

### Jika Muncul `ModuleNotFoundError: libcamera._libcamera`

Gejala khas:

- `rpicam-hello --list-cameras` sudah OK
- tetapi service gagal kamera dengan error Python `libcamera._libcamera`
- traceback menunjuk ke `/usr/local/lib/python3.x/site-packages/libcamera`

Ini biasanya karena sisa package `libcamera` dari instalasi manual di `/usr/local`
menimpa package resmi `python3-libcamera` dari apt.

Perbaikan cepat:

```bash
sudo mkdir -p /root/libcamera-py-backup
sudo mv /usr/local/lib/python3.11/site-packages/libcamera* /root/libcamera-py-backup/ 2>/dev/null || true
sudo mv /usr/local/lib/python3.11/dist-packages/libcamera* /root/libcamera-py-backup/ 2>/dev/null || true
sudo apt install --reinstall python3-libcamera python3-picamera2
sudo ldconfig
sudo systemctl restart neurosense.service
```
