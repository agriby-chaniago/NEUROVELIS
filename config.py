"""
NEUROVELIS – Central Configuration
All hardware addresses, GPIO pins, sampling settings, and paths live here.
Change sensor wiring? Update this file only.
"""

# ─── I2C Bus ───────────────────────────────────────────────────────────────
I2C_BUS = 1   # /dev/i2c-1 (Raspberry Pi standard bus)

# ─── BME280 (temperature + pressure + humidity) ──────────────────────────
# Wiring:
#   VCC → 3.3V (Pin 1)    GND → GND (Pin 6)
#   SDA → GPIO2 (Pin 3)   SCL → GPIO3 (Pin 5)
#   SDO → GND             → I2C address 0x76  (SDO → 3.3V = 0x77)
#   CSB → 3.3V            → selects I2C mode (not SPI)
# Verify: i2cdetect -y 1  → should show 0x76
BMP280_I2C_ADDRESS = 0x76

# ─── MAX30102 ─────────────────────────────────────────────────────────────
# Wiring: SDA→Pin3(GPIO2), SCL→Pin5(GPIO3), VIN→3.3V(Pin1), GND→Pin9
# INT pin NOT used (polling mode — avoids Pi 5 GPIO interrupt issues)
# Address: 0x57 (fixed by Maxim datasheet)
MAX30102_I2C_ADDRESS = 0x57
# REG_SPO2_CONFIG=0x27: bits[4:2]=001 → SR=100 Hz. SMP_AVE=4 → FIFO=25 Hz.
# Empirically confirmed: 100 samples ≈ 4s → 25 Hz effective output rate.
# SMP_AVE=4 → effective rate = 100/4 = 25 Hz
# 300 samples @ 25 Hz = 12s history → 3+ cardiac cycles minimum pada 60 BPM
MAX30102_SAMPLE_BUFFER = 300
MAX30102_SAMPLING_RATE_HZ = 100  # REG_SPO2_CONFIG=0x27 bits[4:2]=001 → SR=100 Hz
# First HR/SpO2 result appears after MIN_SAMPLES are collected.
# 50 samples @ 25 Hz = 2s — enough for 2+ cardiac cycles at 80 BPM.
# After that, results update every _STEP_SIZE samples (~0.4s at step=10).
# On fresh finger placement, fast-fill kicks in to reach this within one step.
MAX30102_MIN_SAMPLES = 50

# LED pulse amplitude (PA) registers for MAX30102.
# Formula: current_mA ~= value * 0.2. Valid range 0x00..0x7F.
# Start from moderate current; runtime controller will raise/lower as needed.
MAX30102_LED_RED_PA = 0x30   # 9.6 mA
MAX30102_LED_IR_PA = 0x38    # 11.2 mA
MAX30102_LED_PA_MIN = 0x10   # 3.2 mA floor
MAX30102_LED_PA_MAX = 0x7F   # 25.4 mA hardware max

# Weak-signal policy for auto gain.
# If IR mean stays below threshold for several consecutive buffers,
# increase LED PA by MAX30102_LED_GAIN_STEP up to MAX30102_LED_PA_MAX.
MAX30102_MIN_IR_SIGNAL = 5000
MAX30102_AUTO_LED_GAIN = True
MAX30102_LED_GAIN_STEP = 0x08
MAX30102_WEAK_SIGNAL_STREAK_FOR_GAIN = 3

# Strong-signal / saturation policy for auto attenuation.
# Prevents ADC clipping (18-bit max = 262143) that can break SpO2 quality.
MAX30102_MAX_IR_SIGNAL = 220000
MAX30102_SATURATION_CLIP_LEVEL = 261000
MAX30102_SATURATION_CLIP_RATIO = 0.20
MAX30102_AUTO_LED_ATTENUATE = True
MAX30102_LED_ATTENUATE_STEP = 0x08
MAX30102_STRONG_SIGNAL_STREAK_FOR_ATTENUATE = 2

# ─── Grove GSR Sensor ─────────────────────────────────────────────────────
# Wiring: Plug Grove cable into A0 port on Grove Base HAT
# Grove Base HAT ADC (STM32 v1.1) I2C address: 0x04
# If your HAT uses MM32 (v1.0), change to 0x08
# Verify with: i2cdetect -y 1
GROVE_HAT_ADC_ADDRESS = 0x04
GSR_GROVE_CHANNEL = 0    # A0 port = channel 0
GSR_ADC_BITS = 12        # Grove Base HAT returns 12-bit values (0–4095)

# ─── ADS1115 (Dual 16-bit ADC via I2C) ───────────────────────────────────
# Wiring (same for both units):
#   VDD → 3.3V (Pin 1)    GND → GND (Pin 6)
#   SDA → GPIO2 (Pin 3)   SCL → GPIO3 (Pin 5)
#   ALRT pin → not connected
#   Unit 1: ADDR → GND  → address 0x48
#   Unit 2: ADDR → VDD  → address 0x49
# Verify: i2cdetect -y 1  → should show 0x48 and 0x49
ADS1115_1_ADDRESS = 0x48
ADS1115_2_ADDRESS = 0x49

# Channels to read: list of (i2c_address, channel_0_to_3, label)
# label = CSV column name. Add/remove entries to configure active channels.
# Connect analog sensors (EMG, flex, NTC, etc.) to AINx → 3.3V max input!
ADS1115_CHANNELS = [
    (0x48, 0, "ads1_ch0_V"),   # ADS1 AIN0 → connect analog sensor here
    (0x48, 1, "ads1_ch1_V"),   # ADS1 AIN1 → spare
    (0x49, 0, "ads2_ch0_V"),   # ADS2 AIN0 → connect analog sensor here
    (0x49, 1, "ads2_ch1_V"),   # ADS2 AIN1 → spare
]

# ─── Sensor Manager ───────────────────────────────────────────────────────
# How often (seconds) each sensor thread reads a new value
BMP280_INTERVAL_S   = 2.0
MAX30102_INTERVAL_S = 0.0   # read_sequential() already blocks for the buffer duration
GSR_INTERVAL_S      = 0.5
ADS1115_INTERVAL_S  = 0.5

# ─── Data Logging ──────────────────────────────────────────────────────────
import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# CSV columns — order matters for readability
# Bump DATA_SCHEMA_VERSION when this list changes (e.g. "1.1.0").
# humidity_percent: populated by BME280; None/empty for BMP280.
DATA_SCHEMA_VERSION = "1.1.0"

CSV_FIELDNAMES = [
    "timestamp_utc",
    "schema_version",
    # ── MAX30102 ──
    "heart_rate_bpm",
    "spo2_percent",
    "hr_valid",
    "spo2_valid",
    # ── BMP280 / BME280 ──
    # humidity_percent: float for BME280, None (empty in CSV) for BMP280
    "temperature_celsius",
    "humidity_percent",
    "pressure_hpa",
    # ── Grove GSR ──
    "gsr_raw_adc",
    "gsr_resistance_ohm",
    "gsr_conductance_us",   # microSiemens (EDA signal)
    # ── ADS1115 (extend by adding channel labels from ADS1115_CHANNELS) ──
    "ads1_ch0_V",
    "ads1_ch1_V",
    "ads2_ch0_V",
    "ads2_ch1_V",
    # ── Model inference (4-class realtime) ──
    "model_label_top1",
    "model_confidence_top1",
    "model_probs_normal",
    "model_probs_anxiety",
    "model_probs_stress",
    "model_probs_depression",
    "model_alert_active",
    "model_alert_reasons",
    "model_latency_ms",
    "model_timestamp_utc",
    # ── Alerts ──
    "alert_active",         # True jika ada kondisi bahaya saat pembacaan ini
    "alert_reasons",        # deskripsi kondisi bahaya, dipisah koma
    "sensor_error",         # nama sensor yang disconnect (None jika semua OK)
]

# ─── Dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = 5000
DASHBOARD_SSE_INTERVAL_S = 0.5   # Push to browser every 500 ms

# ─── Grove Buzzer v1.3 ────────────────────────────────────────────────────
# Wiring: Colok ke port D5 pada Grove Base HAT
# D5 port pada Grove Base HAT = GPIO 5 Raspberry Pi
# Active buzzer: HIGH = berbunyi, LOW = diam
BUZZER_GPIO_PIN   = 5      # GPIO 5 = D5 port pada Grove Base HAT
BUZZER_GPIO_CHIP  = 4      # Raspberry Pi 5 menggunakan gpiochip4 (bukan gpiochip0!)
                           # Pi 4 / Pi 3 gunakan chip 0
BUZZER_ENABLED    = True

# Durasi bunyi untuk tiap kondisi alert (detik)
BUZZER_SHORT_BEEP_S  = 0.1   # 1 beep pendek  → warning ringan
BUZZER_LONG_BEEP_S   = 0.5   # 1 beep panjang → bahaya
BUZZER_COOLDOWN_S    = 5.0   # Jeda minimum antara alert (hindari bunyi terus-menerus)

# Test beep saat startup (2 beep pendek untuk konfirmasi buzzer bekerja)
# Nonaktifkan jika buzzer sudah terbukti berfungsi dan bunyi startup mengganggu
BUZZER_STARTUP_BEEP  = False

# Policy buzzer: default hanya bunyi di kondisi yang benar-benar kritis.
# Alert tetap tercatat di dashboard/CSV, tetapi bunyi bisa dipilih per kondisi.
BUZZER_BEEP_ON_SPO2 = True
BUZZER_BEEP_ON_HR_HIGH = False
BUZZER_BEEP_ON_HR_LOW = False
BUZZER_BEEP_ON_GSR = False
BUZZER_BEEP_ON_SENSOR_ERROR = True

# Kondisi harus terjadi berurutan beberapa kali sebelum buzzer berbunyi.
# Ini menekan false-alarm akibat noise pembacaan sesaat.
BUZZER_MIN_CONSECUTIVE_HITS = 3

# Khusus SpO2: pakai debounce lebih ketat dari default karena paling sensitif
# terhadap noise optik / gerakan jari.
BUZZER_MIN_CONSECUTIVE_HITS_SPO2 = 4

# SpO2 beep hanya diizinkan jika HR valid dan masih masuk rentang fisiologis.
# Mencegah beep dari nilai SpO2 stale/noisy saat finger contact buruk.
BUZZER_REQUIRE_HR_FOR_SPO2_BEEP = True
BUZZER_SPO2_HR_MIN_BPM = 45
BUZZER_SPO2_HR_MAX_BPM = 170

# SENSOR_ERROR beep default: bunyi sekali saat error muncul/berubah,
# bukan berulang selama error state masih sama.
BUZZER_SENSOR_ERROR_BEEP_ON_CHANGE_ONLY = True

# ─── Alert Thresholds ─────────────────────────────────────────────────────
# Buzzer akan berbunyi jika nilai sensor melewati batas ini.
# Set ke None untuk menonaktifkan threshold tertentu.

# Heart Rate (BPM)
ALERT_HR_HIGH    = 120   # > 120 BPM → tachycardia warning
# 40 BPM = lag_max boundary — hampir semua baca ≤40 BPM adalah artifact lag_max,
# bukan bradycardia nyata. Turunkan threshold agar tidak false-alert.
ALERT_HR_LOW     = 40    # < 40 BPM  → bradycardia berat

# SpO2 (%)
# Sensor MAX30102 memberikan pembacaan ~4-6% lebih rendah dari nilai sebenarnya
# (offset kalibrasi). Gunakan 85% sebagai threshold bahaya nyata, bukan 90%.
ALERT_SPO2_LOW   = 85    # < 85% → hipoksemia berbahaya (kalibrasi offset ~5%)

# GSR Conductance (µS) — nilai tinggi = stres/arousal tinggi
ALERT_GSR_HIGH_US = 20.0  # > 20 µS → level stres tinggi

# ─── Arducam / Raspberry Pi Camera ──────────────────────────────────────────
# Wiring:
#   CSI ribbon cable (Arducam CSI) → uses picamera2 automatically   (recommended)
#   USB Arducam → falls back to OpenCV VideoCapture (CAMERA_DEVICE_INDEX)
#
# To verify CSI: rpicam-hello --list-cameras
# To verify USB: ls /dev/video*
CAMERA_ENABLED       = True
# CSI port selection (Raspberry Pi 5 dual-CSI).
# Set 0 untuk CSI0 (default), 1 untuk CSI1.
CAMERA_LIBCAMERA_INDEX = 0
CAMERA_WIDTH         = 1920  # OV64A40 high-speed mode: 1920x1080 target 60fps
CAMERA_HEIGHT        = 1080
CAMERA_FRAMERATE     = 60    # Requires dtoverlay ov64a40 link-frequency=456000000
CAMERA_JPEG_QUALITY  = 75
CAMERA_ROTATION      = 90     # clockwise degrees: 0 / 90 / 180 / 270
CAMERA_DEVICE_INDEX  = 0      # OpenCV fallback: index for /dev/video0 = 0

# Resolusi output stream. Default disamakan dengan mode sensor 1080p.
# Jika latensi/network berat di deployment, turunkan ke 1280x720 atau 640x360.
CAMERA_STREAM_WIDTH  = 1920
CAMERA_STREAM_HEIGHT = 1080

# Sharpness: 1.0 = camera default, 2.0 = sharper (software sharpening via ISP)
CAMERA_SHARPNESS     = 2.0

# Brightness: 0.0 = default, range -1.0 (gelap) hingga 1.0 (terang)
CAMERA_BRIGHTNESS    = 0.2

# Autofocus: True untuk Arducam 64MP AF (OV64A40) — modul ini punya AF motorised.
# False untuk fixed-focus (adjust lensa ring secara manual).
CAMERA_AUTOFOCUS     = True

# Jika driver hanya support mode Auto (tanpa Continuous), worker akan kirim
# trigger fokus berkala agar fokus tetap adaptif terhadap perubahan jarak.
# Set 0 untuk menonaktifkan retrigger berkala.
CAMERA_AF_REFOCUS_INTERVAL_S = 2.0

# Fallback manual untuk modul yang expose LensPosition tapi tidak expose AfMode.
# None = biarkan kamera/driver mengatur. Isi float (mis. 2.0) untuk lock fokus.
CAMERA_LENS_POSITION = None

# Mirror horizontal untuk preview/dashboard (selfie view).
CAMERA_MIRROR_HORIZONTAL = True

# Python executable used by camera_worker.py subprocess.
# Default None = auto-select interpreter that can import picamera2+cv2
# (tries current runtime first, then /usr/bin/python3 on Raspberry Pi).
# Set explicit absolute path only if worker must use a specific runtime.
CAMERA_WORKER_PYTHON = "/usr/bin/python3"
# Max wait time (seconds) for worker pipe activity before camera worker is
# considered stalled and restarted.
CAMERA_WORKER_FRAME_TIMEOUT_S = 12.0

# R/B channel swap.
# OV64A40 (Arducam 64MP) via PiSP backend mengirim data BGR meskipun format
# yang diminta RGB888 — ini bug/quirk driver libcamera + PiSP.
# → CAMERA_SWAP_RB = True wajib untuk kamera ini.
CAMERA_SWAP_RB       = True

# Dataset / fixed-exposure mode
# Set CAMERA_FIXED_EXPOSURE_US > 0 to disable auto-exposure (prevents luminance
# flicker between frames, critical for micro-expression temporal features).
# Example: 16000 ≈ 1/62.5 s shutter at 60 fps.  0 = leave AE enabled (default).
CAMERA_FIXED_EXPOSURE_US = 16000   # microseconds; 0 = AE enabled. 16000 ≈ 1/62.5s @ 60fps
CAMERA_ANALOGUE_GAIN     = 2.0     # analogue gain when fixed-exposure is active
CAMERA_NOISE_REDUCTION_MODE = 2    # 0=off, 1=fast, 2=HighQuality (ISP NR)

# ─── Experiment Settings ───────────────────────────────────────────────────
# All experiment data is saved under DATA_DIR/sessions/{session_id}/
EXPERIMENT_CONDITIONS        = ["normal", "anxiety", "stress", "depression"]
EXPERIMENT_SESSION_DURATION_S = 60          # default recording duration per session
EXPERIMENT_RESPONDENTS_FILE  = os.path.join(DATA_DIR, "respondents.json")
EXPERIMENT_SESSIONS_DIR      = os.path.join(DATA_DIR, "sessions")

# ─── Active Sensors ────────────────────────────────────────────────────────
# To disable a sensor, set its entry to False.
# sensor_manager reads this dict — adding a new sensor only needs a new key here
# and a corresponding reader class.
ACTIVE_SENSORS = {
    "bmp280":   True,
    "max30102": True,
    "gsr":      True,
    "ads1115":  False,  # Set True setelah ADS1115 tersambung ke Pi
}

# ─── Model Inference (Multimodal 4-class) ───────────────────────────────
# Backend type:
#   rule_based  -> deterministic baseline for MVP (no external model file)
#   sklearn_pickle -> scikit-learn model + scaler artifacts
#   pytorch/onnx/tensorflow -> reserved for future model integration
MODEL_INFERENCE_ENABLED = True
MODEL_INFERENCE_BACKEND = "sklearn_pickle"
# Enforce trained-model inference only (disable rule_based fallback path).
MODEL_REQUIRE_TRAINED_BACKEND = True
MODEL_INFERENCE_INTERVAL_S = 1.0
# Visual feature extraction/mesh refresh cadence (independent from model inference).
# Lower value = smoother face mesh overlay but more CPU usage.
MODEL_VISUAL_UPDATE_INTERVAL_S = 0.10
MODEL_INFERENCE_TIMEOUT_MS = 800
MODEL_INFERENCE_MODEL_PATH = os.path.join(
    os.path.dirname(__file__), "model_inference", "artifacts", "model.pkl"
)
# New RandomForest artifact is used directly without external scaler.
MODEL_INFERENCE_SCALER_PATH = None
MODEL_INFERENCE_FEATURES_PATH = os.path.join(
    os.path.dirname(__file__), "model_inference", "artifacts", "features.pkl"
)
MODEL_INFERENCE_MIN_POINTS = 8
# Feature extraction window for model input (seconds).
MODEL_FEATURE_WINDOW_S = 30.0
# Fallback sampling rate used by EDA decomposition when runtime estimation is unstable.
MODEL_SENSOR_SAMPLING_RATE_HZ = 10.0
# Keep runtime feature extraction aligned with training notebook default (10 Hz).
MODEL_USE_FIXED_SENSOR_SAMPLING_RATE = True
# Minimum effective coverage of the configured feature window required for inference.
MODEL_MIN_WINDOW_COVERAGE_RATIO = 0.80
# Training notebook filtered spo2_valid rows before sensor feature extraction.
# Keep this False by default in deployment to avoid over-dropping realtime windows.
MODEL_REQUIRE_SPO2_VALID_FOR_FEATURE_WINDOW = False
# If sensor is untouched, wait this grace period (1-3s recommended) before pausing inference.
MODEL_SENSOR_TOUCH_GRACE_S = 2.0
# Debounce touch transitions to avoid warmup popups from transient sensor spikes.
# Touch starts only after N consecutive valid-touch snapshots.
MODEL_SENSOR_TOUCH_ON_HITS = 2
# Touch ends only after N consecutive non-touch snapshots.
MODEL_SENSOR_TOUCH_OFF_HITS = 2
# Warmup hold after sensor touch/resume to stabilize class decision.
MODEL_CLASS_WARMUP_S = 4.0
MODEL_CLASS_WARMUP_MIN_S = 3.0
MODEL_CLASS_WARMUP_MAX_S = 5.0

# Final class order for the dashboard and API responses.
MODEL_CLASSES = ["normal", "anxiety", "stress", "depression"]

# If camera frame or essential sensor data is missing, force Unknown/No Signal.
MODEL_UNKNOWN_ON_MISSING_DATA = True

# Alert when top-1 class is not normal and confidence exceeds this threshold.
# Tuned for moderate model accuracy (~55%) so alerts stay useful but not too sparse.
MODEL_ALERT_CONFIDENCE_THRESHOLD = 0.68

# EMA smoothing for class probabilities (0<alpha<=1). Lower = smoother.
MODEL_SMOOTHING_ALPHA = 0.30

# Keep normal class in backend probability normalization for calibration,
# while UI can still focus on non-normal risk classes.
MODEL_EXCLUDE_NORMAL_CLASS = False

# Expose independent per-class chance from raw model logits (not normalized
# across classes), so each class can be interpreted on its own 0-100 scale.
MODEL_OUTPUT_INDEPENDENT_CHANCE = True

# Temperature and bias for logit-to-chance conversion.
# chance = sigmoid((logit - bias) / temperature)
MODEL_CHANCE_LOGIT_TEMPERATURE = 1.9
MODEL_CHANCE_LOGIT_BIAS = 0.25

# Post-calibration compression for independent chance values.
# final = clamp(0.5 + (raw - 0.5) * shrinkage, min, max)
MODEL_CHANCE_SHRINKAGE = 0.60
MODEL_CHANCE_MIN = 0.05
MODEL_CHANCE_MAX = 0.88

# Stable QR emission from /model stream.
# Rule: runtime must stay RUNNING for this duration with the same label.
MODEL_STABLE_RUN_SECONDS = 30.0

# Optional advanced gate: require low variance while the stable window accumulates.
MODEL_STABLE_VARIANCE_ENABLED = False
MODEL_STABLE_CONFIDENCE_VAR_MAX = 0.0025
MODEL_STABLE_LABEL_PROB_VAR_MAX = 0.0025

# Route psychiatrist forwarding target per label.
MODEL_QR_TARGET_FIELD_MAP = {
    "anxiety": "anxiety",
    "stress": "stress",
    "depression": "depression",
}

# Custom scheme consumed by scanner apps (Flutter / React Native).
MODEL_QR_FORWARD_URI_TEMPLATE = "neurosense://forward?field={field}"

# ─── Visual Feature Extraction (MediaPipe Face Mesh) ────────────────────
# Enables full realtime facial dynamics extraction for model features
# (EAR, MAR, blink rate, facial motion).
MODEL_FACE_MESH_ENABLED = True
MODEL_FACE_LANDMARKER_MODEL_PATH = os.path.join(
    os.path.dirname(__file__), "model_inference", "artifacts", "face_landmarker.task"
)
MODEL_FACE_MESH_MIN_DETECTION_CONFIDENCE = 0.5
MODEL_FACE_MESH_MIN_TRACKING_CONFIDENCE = 0.5
MODEL_BLINK_EAR_THRESHOLD = 0.21

# EDA phasic event threshold (microSiemens difference per sample) used for
# SCR feature extraction: scr_count, scr_frequency, scr_amp_*.
MODEL_EDA_SCR_DIFF_THRESHOLD_US = 0.03

# Mesh overlay stream interval for /model/mesh_stream (seconds).
# Keep this separate from dashboard SSE interval to avoid camera lag.
MODEL_MESH_STREAM_INTERVAL_S = 0.10

# ─── Scan State Machine ───────────────────────────────────────────────────────
SCAN_DETECTING_DURATION         = 8      # s face must be present before warmup
SCAN_MODEL_STALE_TIMEOUT_S      = 2.0    # s since last inference before face treated as absent
SCAN_WARMUP_DURATION            = 5      # s warmup countdown
SCAN_STABILIZING_DURATION       = 20     # s stabilizing window
SCAN_DATA_COLLECTION_DURATION   = 20     # s collect + average inference + sensor data
SCAN_COOLDOWN_DURATION          = 5      # s wait for user to leave before IDLE
QR_DISPLAY_DURATION             = 120    # s QR code visible
FACE_STABLE_THRESHOLD           = 0.02   # bbox center movement / face width → stable if below
FACE_CONSISTENCY_THRESHOLD      = 0.08   # bbox drift from locked position during STABILIZING
CONFIDENCE_THRESHOLD            = 0.70   # kept for reference; no longer used as freeze gate
SCAN_MIN_SAMPLES                = 10     # min valid samples before freeze allowed
HR_VALID_MIN                    = 30     # BPM — reject absurd sensor readings
HR_VALID_MAX                    = 200
SPO2_VALID_MIN                  = 70.0   # % — reject noise
SPO2_VALID_MAX                  = 100.0
MAX_STATE_DURATION_MULTIPLIER   = 2.5    # watchdog: auto-reset if elapsed > duration × this
