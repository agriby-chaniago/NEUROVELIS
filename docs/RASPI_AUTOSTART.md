# Raspberry Pi 5 Auto-Boot Setup

Dokumen ini membuat Raspberry Pi otomatis:

- menjalankan backend NEUROSENSE saat boot
- membuka dashboard `http://127.0.0.1:5000/model` dalam mode kiosk

## Prasyarat

- Raspberry Pi OS Desktop (agar browser bisa auto-open)
- Project sudah ada di Raspberry Pi (contoh: `/home/pi/NEUROSENSE`)
- Virtual environment `.venv` sudah dibuat dan dependency sudah terpasang

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

Script ini otomatis:

- membuat `/etc/systemd/system/neurosense.service`
- `enable` + `start` service
- menambahkan autostart Chromium kiosk untuk `/model`

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
