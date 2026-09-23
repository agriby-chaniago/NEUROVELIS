# NEUROSENSE + Forward Psikiater

Sistem skrining psikofisiologis berbasis Raspberry Pi yang menggabungkan pembacaan
sensor biometrik dengan analisis fitur wajah, lalu menampilkan hasilnya melalui
dashboard web real-time.

Perangkat membaca detak jantung dan SpO2 (MAX30102), konduktansi kulit/GSR (ADS1115),
serta suhu dan tekanan lingkungan (BME280/BMP280). Secara paralel, kamera mengekstraksi
fitur visual seperti *eye aspect ratio*, *mouth aspect ratio*, laju kedipan, dan
pergerakan kepala melalui MediaPipe Face Landmarker. Fitur gabungan dari kedua kanal
tersebut dimasukkan ke model Random Forest yang mengeluarkan distribusi probabilitas
untuk empat kelas: `normal`, `anxiety`, `stress`, dan `depression`. Hasil satu sesi
pemindaian dibekukan menjadi laporan yang dapat dibuka responden melalui QR code dan
diunduh sebagai PDF.

---

## ⚠️ Disclaimer

**Perangkat lunak ini adalah proyek penelitian dan edukasi. Ini bukan alat diagnosis
medis dan tidak boleh digunakan sebagai dasar diagnosis, terapi, atau keputusan klinis
apa pun.**

Keluaran model berupa estimasi statistik dari sinyal fisiologis dan fitur visual, bukan
penilaian psikiatrik. Model dilatih pada dataset terbatas dalam lingkungan penelitian
terkontrol, sehingga akurasinya tidak tervalidasi secara klinis dan tidak dapat
digeneralisasi ke populasi umum. Kondisi kesehatan mental hanya dapat ditegakkan oleh
tenaga profesional berlisensi melalui asesmen langsung.

Siapa pun yang mengalami keluhan kesehatan mental dianjurkan menghubungi psikolog,
psikiater, atau layanan kesehatan terdekat. Penggunaan perangkat lunak ini sepenuhnya
menjadi tanggung jawab pengguna, sesuai penafian jaminan pada berkas [LICENSE](LICENSE).

Pengumpulan data responden pada repositori ini tunduk pada persetujuan etik penelitian
di institusi penyelenggara. Data sesi bersifat lokal, tidak disertakan dalam repositori,
dan tidak dikirim ke layanan pihak ketiga mana pun.

---

## Kepemilikan dan Pengembang

NEUROSENSE + Forward Psikiater dikembangkan sebagai proyek penelitian di
**Universitas Harapan Bangsa**, dan hak atas karya ini berada pada institusi beserta
tim pengembangnya.

Sistem ini dikerjakan oleh tim mahasiswa Universitas Harapan Bangsa secara kolektif,
mencakup perancangan perangkat keras sensor, pipeline inferensi, dan antarmuka web.
Koordinasi pengembangan serta pemeliharaan repositori dilakukan melalui akun
[@agriby-chaniago](https://github.com/agriby-chaniago).

Pertanyaan, laporan bug, dan usulan perbaikan silakan disampaikan melalui
[GitHub Issues](https://github.com/agriby-chaniago/NEUROSENSE/issues) pada repositori ini.

---

## Menjalankan

Target perangkat keras adalah Raspberry Pi dengan sensor I2C terpasang. Dependensi
sistem tercantum pada [packages.txt](packages.txt), dependensi Python pada
[requirements.txt](requirements.txt).

```bash
pip install -r requirements.txt
python main.py              # mode normal
python main.py --debug      # logging verbose + Flask debug
```

Dashboard tersedia di `http://<ip-raspberry-pi>:5000`.

Panduan lanjutan tersedia pada direktori [docs/](docs/): penerapan model
([MODEL_DEPLOYMENT.md](docs/MODEL_DEPLOYMENT.md)), autostart perangkat
([RASPI_AUTOSTART.md](docs/RASPI_AUTOSTART.md)), akses jarak jauh
([NGROK_RASPI_SETUP.md](docs/NGROK_RASPI_SETUP.md)), dan struktur frontend
([FRONTEND_STRUCTURE.md](docs/FRONTEND_STRUCTURE.md)).

---

## Lisensi

Dirilis di bawah [Lisensi MIT](LICENSE). Hak cipta © 2026 Tim NEUROSENSE,
Universitas Harapan Bangsa.

---

## Struktur Proyek

```
NEUROSENSE + Forward Psikiater
├─ dashboard
│  ├─ static
│  │  ├─ css
│  │  │  ├─ core
│  │  │  │  ├─ base.css
│  │  │  │  ├─ components.css
│  │  │  │  └─ theme.css
│  │  │  ├─ layouts
│  │  │  │  └─ fixed_dashboard.css
│  │  │  ├─ pages
│  │  │  │  ├─ dashboard.css
│  │  │  │  ├─ experiment.css
│  │  │  │  ├─ model.css
│  │  │  │  ├─ respondents.css
│  │  │  │  └─ sessions.css
│  │  │  └─ shared
│  │  │     └─ warmup_ui.css
│  │  └─ js
│  │     ├─ pages
│  │     │  ├─ dashboard.js
│  │     │  ├─ experiment.js
│  │     │  ├─ model.js
│  │     │  ├─ respondents.js
│  │     │  └─ sessions.js
│  │     └─ shared
│  │        ├─ chart_utils.js
│  │        ├─ sse_client.js
│  │        └─ warmup_ui.js
│  ├─ templates
│  │  ├─ pages
│  │  │  ├─ dashboard.html
│  │  │  ├─ experiment.html
│  │  │  ├─ model.html
│  │  │  ├─ report.html
│  │  │  ├─ respondents.html
│  │  │  └─ sessions.html
│  │  ├─ base.html
│  │  ├─ experiment.html
│  │  ├─ index.html
│  │  ├─ model.html
│  │  ├─ respondents.html
│  │  └─ sessions.html
│  ├─ __init__.py
│  ├─ app.py
│  ├─ report_content.py
│  └─ report_pdf.py
├─ data
│  ├─ sessions
│  │  └─ .gitkeep
│  └─ .gitkeep
├─ data_logging
│  ├─ __init__.py
│  └─ csv_logger.py
├─ docs
│  ├─ FRONTEND_STRUCTURE.md
│  ├─ MODEL_DEPLOYMENT.md
│  ├─ NGROK_RASPI_SETUP.md
│  └─ RASPI_AUTOSTART.md
├─ experiments
│  ├─ __init__.py
│  ├─ respondent_registry.py
│  └─ session_manager.py
├─ model_inference
│  ├─ artifacts
│  │  ├─ face_landmarker.task
│  │  ├─ features.pkl
│  │  └─ model.pkl
│  ├─ __init__.py
│  ├─ model_adapter.py
│  ├─ model_inference_service.py
│  ├─ smoother.py
│  ├─ stability_tracker.py
│  ├─ visual_feature_extractor.py
│  └─ warmup_gate.py
├─ scan_engine
│  ├─ __init__.py
│  ├─ result_freezer.py
│  └─ scan_state_machine.py
├─ scripts
│  ├─ install_ngrok_raspi.sh
│  ├─ install_raspi_autoboot.sh
│  ├─ open_model_kiosk.sh
│  └─ start_dev.sh
├─ sensors
│  ├─ __init__.py
│  ├─ ads1115_reader.py
│  ├─ bme280_reader.py
│  ├─ bmp280_reader.py
│  ├─ buzzer.py
│  ├─ camera_reader.py
│  ├─ gsr_reader.py
│  ├─ hrcalc.py
│  ├─ max30102.py
│  ├─ max30102_reader.py
│  └─ sensor_manager.py
├─ tests
│  ├─ __init__.py
│  ├─ test_model_inference.py
│  └─ test_sensors.py
├─ .gitignore
├─ LICENSE
├─ README.md
├─ camera_worker.py
├─ config.py
├─ main.py
├─ neurovelis.service
├─ packages.txt
└─ requirements.txt
```
