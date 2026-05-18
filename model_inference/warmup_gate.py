"""Sensor-touch + face-presence lifecycle manager and class-output warmup gate."""
from __future__ import annotations

import random
from typing import Optional

import config


class _WarmupGate:
    """Tracks finger-on-sensor contact and face presence; gates class output during warmup.

    Responsibilities:
    - Debounce raw sensor-touch signal (on/off hit counters)
    - Schedule warmup window when BOTH touch AND face detected
    - Suppress class output during warmup period
    - Annotate result dict with runtime state / warmup countdown
    - Signal when EMA smoother should be reset (returns True from update())
    """

    def __init__(self) -> None:
        self._class_warmup_until: float = 0.0
        self._warmup_completed: bool = False
        self._sensor_touch_prev: bool = False
        self._sensor_touch_missing_since: Optional[float] = None
        self._sensor_touch_paused: bool = False
        self._sensor_touch_paused_at: Optional[float] = None
        self._sensor_touch_present_hits: int = 0
        self._sensor_touch_absent_hits: int = 0
        self._face_last_seen: bool = False
        # True when sensor IS touched but face not yet in frame.
        # Distinct from _sensor_touch_paused (sensor absent) so the UI shows
        # "WAITING_FACE" rather than "WAITING_SENSOR" when finger is on sensor.
        self._waiting_for_face: bool = False

    @property
    def is_paused(self) -> bool:
        return self._sensor_touch_paused

    @property
    def is_waiting_for_face(self) -> bool:
        return self._waiting_for_face

    @staticmethod
    def _is_sensor_touched(sensor_data: dict) -> bool:
        """Heuristic: finger contact requires valid HR+SpO2 in plausible ranges + non-null GSR."""
        hr_valid = bool(sensor_data.get("hr_valid"))
        spo2_valid = bool(sensor_data.get("spo2_valid"))

        try:
            hr_val: Optional[float] = float(sensor_data.get("heart_rate_bpm"))
        except (TypeError, ValueError):
            hr_val = None
        try:
            spo2_val: Optional[float] = float(sensor_data.get("spo2_percent"))
        except (TypeError, ValueError):
            spo2_val = None
        try:
            gsr_val: Optional[float] = float(sensor_data.get("gsr_conductance_us"))
        except (TypeError, ValueError):
            gsr_val = None

        hr_ok = hr_valid and hr_val is not None and 40.0 <= hr_val <= 160.0
        spo2_ok = spo2_valid and spo2_val is not None and 70.0 <= spo2_val <= 100.0
        gsr_ok = gsr_val is not None and gsr_val >= 0.0
        return hr_ok and spo2_ok and gsr_ok

    def _is_sensor_touched_debounced(self, sensor_data: dict) -> bool:
        """Debounce so a transient spike doesn't flip touch state."""
        raw_touched = self._is_sensor_touched(sensor_data)
        on_hits = max(1, int(getattr(config, "MODEL_SENSOR_TOUCH_ON_HITS", 2)))
        off_hits = max(1, int(getattr(config, "MODEL_SENSOR_TOUCH_OFF_HITS", 2)))

        if raw_touched:
            self._sensor_touch_present_hits += 1
            self._sensor_touch_absent_hits = 0
        else:
            self._sensor_touch_absent_hits += 1
            self._sensor_touch_present_hits = 0

        if self._sensor_touch_prev:
            if raw_touched:
                return True
            return self._sensor_touch_absent_hits < off_hits

        if not raw_touched:
            return False
        return self._sensor_touch_present_hits >= on_hits

    def update(self, sensor_data: dict, now: float, face_detected: bool = True) -> bool:
        """Process one sensor tick. Returns True if EMA smoother should be reset.

        Warmup only starts when BOTH sensor is touched AND face_detected is True.
        If sensor is held but face is absent, warmup is deferred until face appears.
        """
        should_reset_smoother = False
        touched = self._is_sensor_touched_debounced(sensor_data)
        grace_s = max(1.0, min(3.0, float(getattr(config, "MODEL_SENSOR_TOUCH_GRACE_S", 2.0))))
        warmup_default = max(0.0, float(getattr(config, "MODEL_CLASS_WARMUP_S", 4.0)))
        warmup_min = max(0.0, float(getattr(config, "MODEL_CLASS_WARMUP_MIN_S", warmup_default)))
        warmup_max = max(warmup_min, float(getattr(config, "MODEL_CLASS_WARMUP_MAX_S", warmup_default)))

        face_just_appeared = face_detected and not self._face_last_seen
        self._face_last_seen = face_detected

        if touched:
            self._sensor_touch_missing_since = None
            if not face_detected:
                # Sensor held but face not in frame — clear sensor-pause (finger IS on sensor)
                # and set waiting_for_face so UI shows WAITING_FACE, not WAITING_SENSOR.
                self._sensor_touch_paused = False
                self._sensor_touch_paused_at = None
                self._waiting_for_face = True
                self._sensor_touch_prev = True
                return False
            # Face AND sensor both present — clear both hold flags.
            self._waiting_for_face = False
            if self._sensor_touch_paused or not self._sensor_touch_prev or face_just_appeared:
                absent_s = (
                    (now - self._sensor_touch_paused_at)
                    if self._sensor_touch_paused_at is not None
                    else float("inf")
                )
                restart_after = max(
                    0.0,
                    float(getattr(config, "MODEL_WARMUP_RESTART_AFTER_AWAY_S", 30.0)),
                )
                should_restart_warmup = not self._warmup_completed or absent_s >= restart_after
                self._sensor_touch_paused = False
                self._sensor_touch_paused_at = None
                should_reset_smoother = True
                if should_restart_warmup:
                    warmup_s = random.uniform(warmup_min, warmup_max)
                    self._class_warmup_until = now + warmup_s
                    self._warmup_completed = False  # warmup per-subject, not per-service-lifetime
            self._sensor_touch_prev = True
            return should_reset_smoother

        # Sensor not touched — clear face-wait flag (no point waiting for face without sensor).
        self._waiting_for_face = False
        if self._sensor_touch_missing_since is None:
            self._sensor_touch_missing_since = now

        if (now - self._sensor_touch_missing_since) >= grace_s:
            if not self._sensor_touch_paused:
                self._sensor_touch_paused_at = now
                should_reset_smoother = True
            self._sensor_touch_paused = True
        self._sensor_touch_prev = False
        return should_reset_smoother

    def apply_gate(self, result: dict, now: float) -> dict:
        """Suppress class output during warmup window."""
        if now >= self._class_warmup_until:
            return result
        label = result.get("model_label_top1")
        if label not in config.MODEL_CLASSES:
            return result
        remaining = max(0.0, self._class_warmup_until - now)
        warmed = dict(result)
        warmed["model_label_top1"] = "unknown"
        warmed["model_confidence_top1"] = None
        warmed["model_probs_normal"] = 0.0
        warmed["model_probs_anxiety"] = 0.0
        warmed["model_probs_stress"] = 0.0
        warmed["model_probs_depression"] = 0.0
        warmed["model_chance_normal"] = 0.0
        warmed["model_chance_anxiety"] = 0.0
        warmed["model_chance_stress"] = 0.0
        warmed["model_chance_depression"] = 0.0
        warmed["model_alert_active"] = False
        warmed["model_alert_reasons"] = f"CLASS_WARMUP:{round(remaining, 1)}s"
        return warmed

    def annotate_runtime(self, result: dict, now: float) -> dict:
        """Attach runtime state and warmup countdown fields to payload."""
        out = dict(result)
        reason = str(out.get("model_alert_reasons") or "")
        label = str(out.get("model_label_top1") or "").lower()

        remaining = max(0.0, self._class_warmup_until - now)
        warmup_active = (
            not self._sensor_touch_paused
            and not self._waiting_for_face
            and remaining > 0.0
        )

        if self._sensor_touch_paused:
            state = "WAITING_SENSOR"
            runtime_reason = "SENSOR_NOT_TOUCHED"
            warmup_active = False
            remaining = 0.0
        elif self._waiting_for_face:
            state = "WAITING_FACE"
            runtime_reason = "FACE_NOT_DETECTED"
            warmup_active = False
            remaining = 0.0
        elif warmup_active:
            state = "WARMUP"
            runtime_reason = f"CLASS_WARMUP:{round(remaining, 1)}s"
        elif label in config.MODEL_CLASSES:
            state = "RUNNING"
            runtime_reason = "LIVE"
            self._warmup_completed = True
        elif reason.startswith("SERVICE_INIT"):
            state = "INIT"
            runtime_reason = reason
        else:
            state = "DEGRADED"
            runtime_reason = reason or "UNKNOWN"

        out["model_warmup_active"] = warmup_active
        out["model_warmup_remaining_s"] = round(remaining, 2) if warmup_active else 0.0
        out["model_runtime_state"] = state
        out["model_runtime_reason"] = runtime_reason
        return out
