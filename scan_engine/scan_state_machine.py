"""
scan_engine/scan_state_machine.py

Face-triggered scan state machine. Runs as a background daemon thread.
Polls ModelInferenceService (read-only) for face + inference data.
Writes frozen scan results to data/scans/ via ResultFreezer.

State flow:
  IDLE → DETECTING → WARMUP → STABILIZING → DATA_COLLECTION → RESULT_READY → QR_DISPLAY → COOLDOWN → IDLE
"""

import logging
import math
import statistics
import time
import threading
from collections import Counter
from datetime import datetime, timezone

import config
from scan_engine.result_freezer import ResultFreezer


def _is_valid_reading(value, lo=None, hi=None) -> bool:
    """Return True if value is a finite number within optional [lo, hi] range."""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(f) or math.isinf(f):
        return False
    if lo is not None and f < lo:
        return False
    if hi is not None and f > hi:
        return False
    return True

_logger = logging.getLogger(__name__)

_HINTS = {
    "IDLE":            "",
    "DETECTING":       "Pastikan wajah dan sensor terdeteksi secara bersamaan",
    "WARMUP":          "Persiapan... Jangan lepas sensor",
    "STABILIZING":     "Jangan bergerak — wajah dan sensor harus tetap terdeteksi",
    "DATA_COLLECTION": "Stabil, mohon tunggu... Jangan lepas sensor",
    "RESULT_READY":    "Memproses hasil...",
    "QR_DISPLAY":      "Scan QR dengan HP",
    "COOLDOWN":        "Mundur dari kamera untuk scan berikutnya",
}

_DURATIONS = {
    "DETECTING":       "SCAN_DETECTING_DURATION",
    "WARMUP":          "SCAN_WARMUP_DURATION",
    "STABILIZING":     "SCAN_STABILIZING_DURATION",
    "DATA_COLLECTION": "SCAN_DATA_COLLECTION_DURATION",
    "QR_DISPLAY":      "QR_DISPLAY_DURATION",
    "COOLDOWN":        "SCAN_COOLDOWN_DURATION",
}


class ScanStateMachine:
    def __init__(self, model_inference_service, sensor_manager=None):
        self._mis            = model_inference_service
        self._sensor_manager = sensor_manager
        self._freezer        = ResultFreezer()
        self._lock           = threading.Lock()

        # state
        self._state        = "IDLE"
        self._state_entered = time.monotonic()

        # face tracking
        self._prev_bbox          = None   # FIX 1: bbox-based stability
        self._locked_bbox        = None   # locked at STABILIZING entry
        self._face_absent_since  = None
        self._inconsistent_count = 0      # FIX 12: jitter counter
        self._face_present_streak = 0     # consecutive True-face ticks in STABILIZING

        # sensor tracking
        self._sensor_wait         = False  # True when in DETECTING waiting for sensor touch
        self._sensor_was_ok       = False  # True once sensor was present in DETECTING (detect drop)
        self._sensor_absent_since = None   # grace period for STABILIZING/DATA_COLLECTION
        # Prevents 8s countdown from restarting on repeated sensor toggles within one DETECTING cycle.
        # Cleared by _transition() whenever DETECTING is exited.
        self._detecting_clock_locked = False

        # DATA_COLLECTION
        self._data_samples:    list = []
        self._locked_face_bbox      = None  # identity reference locked at DATA_COLLECTION entry

        # frozen result — local copy used by get_state (FIX 3)
        self._frozen_result = None

        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScanStateMachine")

    # ── public API ────────────────────────────────────────────────────────────

    def start(self):
        self._freezer.cleanup_old_scans()   # FIX 5: cleanup on startup
        self._thread.start()

    def get_state(self) -> dict:
        with self._lock:                    # FIX 3: lock for all shared reads
            state       = self._state
            entered     = self._state_entered
            frozen      = self._frozen_result    # local copy — safe to read outside lock
            sensor_wait = self._sensor_wait

        elapsed   = time.monotonic() - entered
        cfg_key   = _DURATIONS.get(state)
        duration  = getattr(config, cfg_key, 0) if cfg_key else 0
        remaining = max(0.0, duration - elapsed) if duration else 0.0

        return {
            "scan_state":              state,
            "scan_elapsed_s":          round(elapsed, 1),
            "scan_remaining_s":        round(remaining, 1),
            "scan_hint_message":       (
                "Sensor tidak terdeteksi — letakkan jari pada sensor"
                if sensor_wait
                else _HINTS.get(state, "")
            ),
            "scan_waiting_for_sensor": sensor_wait,
            "scan_id":                 frozen["scan_id"] if frozen else None,
            "scan_qr_url":             frozen.get("qr_url") if frozen else None,
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _transition(self, new_state: str):
        if self._state == "DETECTING":
            self._detecting_clock_locked = False
        self._state        = new_state
        self._state_entered = time.monotonic()

    def _elapsed(self) -> float:
        return time.monotonic() - self._state_entered

    def _reset_to_idle(self):
        """Full clean reset — no stale data leaks into next scan."""
        self._transition("IDLE")
        self._frozen_result      = None
        self._prev_bbox          = None
        self._locked_bbox        = None
        self._inconsistent_count  = 0
        self._face_absent_since   = None
        self._face_present_streak = 0
        self._data_samples        = []
        self._locked_face_bbox    = None
        self._sensor_wait         = False
        self._sensor_was_ok       = False
        self._sensor_absent_since = None

    @staticmethod
    def _bbox_from_landmarks(landmarks):
        """Return (cx, cy, w) from normalized landmark list, or None."""
        if not landmarks:
            return None
        points = []
        for lm in landmarks:
            try:
                x = float(lm[0])
                y = float(lm[1])
            except (IndexError, TypeError, ValueError):
                continue
            if not (math.isfinite(x) and math.isfinite(y)):
                continue
            points.append((x, y))
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        return ((xmin + xmax) / 2, (ymin + ymax) / 2, xmax - xmin)

    def _face_stable(self, bbox) -> bool:
        """FIX 1+11: stability via bbox center delta, first frame seeds prev and returns False."""
        if bbox is None:
            return False
        if self._prev_bbox is None:
            self._prev_bbox = bbox          # FIX 11: seed prev, NOT stable yet
            return False
        cx0, cy0, w0 = self._prev_bbox
        cx1, cy1, _  = bbox
        if w0 == 0:
            return True
        movement = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
        return movement < config.FACE_STABLE_THRESHOLD

    def _face_consistent(self, bbox) -> bool:
        """Bbox must not drift from locked position (guard during STABILIZING)."""
        if bbox is None:
            return False
        if self._locked_bbox is None:
            return True
        cx0, cy0, w0 = self._locked_bbox
        cx1, cy1, _  = bbox
        if w0 == 0:
            return True
        drift = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
        return drift < config.FACE_CONSISTENCY_THRESHOLD

    @staticmethod
    def _avg_clean(values: list) -> float:
        """Mean with None/NaN/Inf filter + IQR outlier trim."""
        clean = []
        for v in values:
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isnan(f) or math.isinf(f):
                continue
            clean.append(f)
        if not clean:
            return 0.0
        if len(clean) < 4:
            return sum(clean) / len(clean)
        clean.sort()
        q1, _, q3 = statistics.quantiles(clean, n=4)
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        trimmed = [v for v in clean if lo <= v <= hi] or clean
        return sum(trimmed) / len(trimmed)

    @staticmethod
    def _validate_sample(s: dict) -> bool:
        """Reject samples with physiologically impossible sensor values."""
        hr = s.get("hr")
        if hr is not None:
            try:
                if not (config.HR_VALID_MIN <= float(hr) <= config.HR_VALID_MAX):
                    return False
            except (TypeError, ValueError):
                return False
        spo2 = s.get("spo2")
        if spo2 is not None:
            try:
                if not (config.SPO2_VALID_MIN <= float(spo2) <= config.SPO2_VALID_MAX):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _sensor_ready(self, sensor_snapshot: dict) -> bool:
        """True if both HR and GSR sensors are providing valid readings."""
        if self._sensor_manager is None:
            return True
        if sensor_snapshot.get("sensor_stale", True):
            return False
        hr  = sensor_snapshot.get("heart_rate_bpm")
        gsr = sensor_snapshot.get("gsr_conductance_us")
        return (
            _is_valid_reading(hr,  lo=config.HR_VALID_MIN, hi=config.HR_VALID_MAX)
            and _is_valid_reading(gsr, lo=0.0)
        )

    # ── main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                _logger.exception("ScanSM: uncaught error in _tick()")
            time.sleep(0.05)

    def _tick(self):
        latest    = self._mis.get_latest()
        mesh      = self._mis.get_mesh_latest()

        # Face freshness gate — model_face_detected has no staleness protection
        _ts_str = latest.get("model_timestamp_utc")
        _face_fresh = False
        if _ts_str:
            try:
                _ts = datetime.fromisoformat(_ts_str)
                if _ts.tzinfo is None:
                    _ts = _ts.replace(tzinfo=timezone.utc)
                _face_fresh = (
                    (datetime.now(timezone.utc) - _ts).total_seconds()
                    < config.SCAN_MODEL_STALE_TIMEOUT_S
                )
            except (ValueError, TypeError, OverflowError):
                pass  # malformed timestamp → treat as stale

        face       = latest.get("model_face_detected", False) and _face_fresh
        try:
            face_count = int(latest.get("model_face_count", 0)) if _face_fresh else 0
        except (TypeError, ValueError):
            face_count = 0
        landmarks = (mesh or {}).get("model_face_landmarks", [])

        confidence = float(latest.get("model_confidence_top1") or 0.0)
        label      = latest.get("model_label_top1") or ""

        sensor_snapshot = {}
        if self._sensor_manager is not None:
            try:
                sensor_snapshot = self._sensor_manager.get_latest()
            except Exception:
                pass

        bbox = self._bbox_from_landmarks(landmarks)

        with self._lock:
            state = self._state

            # Watchdog: auto-reset if any timed state exceeds max allowed duration
            cfg_key = _DURATIONS.get(state)
            if cfg_key:
                max_dur = getattr(config, cfg_key, 0) * getattr(config, "MAX_STATE_DURATION_MULTIPLIER", 2.5)
                if max_dur > 0 and self._elapsed() > max_dur:
                    _logger.error("ScanSM: watchdog timeout in %s (%.1fs > %.1fs)", state, self._elapsed(), max_dur)
                    self._reset_to_idle()
                    return

            if state == "IDLE":
                if face:
                    self._transition("DETECTING")
                    self._face_absent_since = None
                    self._prev_bbox = bbox

            elif state == "DETECTING":
                if not face:
                    self._reset_to_idle()
                    return

                sensor_ok = self._sensor_ready(sensor_snapshot)
                if sensor_ok:
                    if not self._sensor_was_ok and not self._detecting_clock_locked:
                        # Sensor first becomes valid this cycle — start 8s countdown from
                        # when BOTH face AND sensor are simultaneously active.
                        self._state_entered = time.monotonic()
                        self._detecting_clock_locked = True
                    self._sensor_was_ok = True
                    self._sensor_wait   = False
                else:
                    if self._sensor_was_ok:
                        # sensor was present, now dropped → reset
                        _logger.warning("ScanSM: sensor dropped in DETECTING, reset")
                        self._reset_to_idle()
                        return
                    else:
                        # sensor never attached → hold in DETECTING, update hint
                        self._sensor_wait = True

                self._prev_bbox = bbox
                if self._elapsed() >= config.SCAN_DETECTING_DURATION and sensor_ok:
                    self._sensor_wait = False
                    self._transition("WARMUP")

            elif state == "WARMUP":
                if not face:
                    self._reset_to_idle()
                    return
                elif not self._sensor_ready(sensor_snapshot):
                    _logger.warning("ScanSM: sensor dropped in WARMUP, reset")
                    self._reset_to_idle()
                    return
                else:
                    self._prev_bbox = bbox
                    if self._elapsed() >= config.SCAN_WARMUP_DURATION:
                        self._locked_bbox         = bbox
                        self._inconsistent_count  = 0
                        self._face_present_streak = 0
                        self._face_absent_since   = None
                        self._sensor_absent_since = None
                        self._transition("STABILIZING")

            elif state == "STABILIZING":
                if face_count > 1:
                    _logger.warning("ScanSM: %d faces in STABILIZING, reset", face_count)
                    self._reset_to_idle()
                    return
                if not face:
                    self._face_present_streak = 0
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since > 5.0:
                        self._reset_to_idle()
                        return
                else:
                    self._face_present_streak += 1
                    if self._face_present_streak >= 10:  # ~500ms consecutive before clearing
                        self._face_absent_since = None

                    # sensor absent gate (5s grace)
                    if not self._sensor_ready(sensor_snapshot):
                        if self._sensor_absent_since is None:
                            self._sensor_absent_since = time.monotonic()
                        elif time.monotonic() - self._sensor_absent_since > 5.0:
                            _logger.warning("ScanSM: sensor absent in STABILIZING, reset")
                            self._reset_to_idle()
                            return
                    else:
                        self._sensor_absent_since = None

                    consistent = self._face_consistent(bbox)
                    self._prev_bbox = bbox

                    if not consistent:
                        self._inconsistent_count += 1
                        if self._inconsistent_count > 6:
                            self._reset_to_idle()
                            return
                    else:
                        self._inconsistent_count = 0
                        if self._elapsed() >= config.SCAN_STABILIZING_DURATION:
                            self._data_samples        = []
                            self._locked_face_bbox    = bbox
                            self._face_absent_since   = None
                            self._sensor_absent_since = None
                            self._face_present_streak = 0
                            self._inconsistent_count  = 0
                            _logger.info("ScanSM: STABILIZING → DATA_COLLECTION")
                            self._transition("DATA_COLLECTION")

            elif state == "DATA_COLLECTION":
                # multi-face guard
                if face_count > 1:
                    _logger.warning("ScanSM: %d faces in DATA_COLLECTION, reset", face_count)
                    self._reset_to_idle()
                    return

                # face lost guard
                if not face:
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since > 5.0:
                        _logger.warning("ScanSM: face lost during DATA_COLLECTION, reset")
                        self._reset_to_idle()
                    return

                self._face_absent_since = None

                # sensor absent gate (5s grace)
                if not self._sensor_ready(sensor_snapshot):
                    if self._sensor_absent_since is None:
                        self._sensor_absent_since = time.monotonic()
                    elif time.monotonic() - self._sensor_absent_since > 5.0:
                        _logger.warning("ScanSM: sensor absent in DATA_COLLECTION, reset")
                        self._reset_to_idle()
                        return
                else:
                    self._sensor_absent_since = None

                # identity continuity guard
                if bbox and self._locked_face_bbox:
                    cx0, cy0, w0 = self._locked_face_bbox
                    cx1, cy1, _  = bbox
                    if w0 > 0:
                        drift = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
                        if drift > config.FACE_CONSISTENCY_THRESHOLD * 2:
                            _logger.warning("ScanSM: face identity drift in DATA_COLLECTION, reset")
                            self._reset_to_idle()
                            return

                # collect sample
                sample = {
                    "stress":      latest.get("model_probs_stress"),
                    "anxiety":     latest.get("model_probs_anxiety"),
                    "depression":  latest.get("model_probs_depression"),
                    "label":       label,
                    "confidence":  confidence,
                    "hr":          sensor_snapshot.get("heart_rate_bpm"),
                    "spo2":        sensor_snapshot.get("spo2_percent"),
                    "gsr":         sensor_snapshot.get("gsr_conductance_us"),
                    "temperature": sensor_snapshot.get("temperature_celsius"),
                    "pressure":    sensor_snapshot.get("pressure_hpa"),
                }
                if self._validate_sample(sample):
                    self._data_samples.append(sample)

                if self._elapsed() >= config.SCAN_DATA_COLLECTION_DURATION:
                    n_valid = len(self._data_samples)
                    _logger.info("ScanSM: DATA_COLLECTION done — %d valid samples", n_valid)

                    if n_valid < config.SCAN_MIN_SAMPLES:
                        _logger.warning("ScanSM: too few valid samples (%d), reset", n_valid)
                        self._reset_to_idle()
                        return

                    valid_labels = [s["label"] for s in self._data_samples if s.get("label")]
                    if valid_labels:
                        avg_label = Counter(valid_labels).most_common(1)[0][0]
                    else:
                        _logger.warning("ScanSM: no valid labels in samples, defaulting to 'normal'")
                        avg_label = "normal"

                    label_to_key = {"stress": "stress", "anxiety": "anxiety", "depression": "depression"}
                    prob_key = label_to_key.get(avg_label)
                    avg_conf = round(
                        self._avg_clean([s[prob_key] for s in self._data_samples])
                        if prob_key
                        else self._avg_clean([s["confidence"] for s in self._data_samples]),
                        3,
                    )

                    def _r(key):
                        return round(self._avg_clean([s[key] for s in self._data_samples]), 3)

                    merged = dict(latest)
                    merged["model_probs_stress"]     = _r("stress")
                    merged["model_probs_anxiety"]    = _r("anxiety")
                    merged["model_probs_depression"] = _r("depression")
                    merged["heart_rate_bpm"]         = _r("hr")
                    merged["spo2_percent"]           = _r("spo2")
                    merged["gsr_conductance_us"]     = _r("gsr")
                    merged["temperature_celsius"]    = _r("temperature")
                    merged["pressure_hpa"]           = _r("pressure")
                    merged["_sample_count"]          = n_valid

                    try:
                        frozen = self._freezer.freeze(merged, avg_label, avg_conf)
                        self._frozen_result = frozen
                        _logger.info(
                            "ScanSM: freeze OK → scan_id=%s label=%s conf=%.3f samples=%d",
                            frozen.get("scan_id"), avg_label, avg_conf, n_valid,
                        )
                    except Exception as exc:
                        _logger.error("ScanSM: freeze failed: %s", exc)
                        self._reset_to_idle()
                        return
                    finally:
                        self._data_samples = []

                    self._transition("RESULT_READY")

            elif state == "RESULT_READY":
                self._transition("QR_DISPLAY")

            elif state == "QR_DISPLAY":
                if self._elapsed() >= config.QR_DISPLAY_DURATION:
                    self._face_absent_since = None
                    self._transition("COOLDOWN")

            elif state == "COOLDOWN":
                if not face:
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since >= config.SCAN_COOLDOWN_DURATION:
                        self._reset_to_idle()       # FIX 13/18
                else:
                    self._face_absent_since = None
