"""Stability tracker — accumulates a sliding window of inference results
and gates final scan output behind a consecutive-label + variance check."""
from __future__ import annotations

import threading
from collections import deque
from typing import Optional

import numpy as np

import config


class _StabilityTracker:
    """Sliding-window label stability gate.

    Resets whenever:
    - runtime state is not RUNNING
    - label changes
    - eligible label set is left (only anxiety/stress/depression count)
    - sensor becomes invalid

    Publishes stability metadata fields into the result dict on every tick.
    Uses its own lock so it can be called from the inference loop without
    holding the outer ModelInferenceService lock.
    """

    def __init__(self) -> None:
        self._window: deque = deque()
        self._window_label: Optional[str] = None
        self._last_reset_reason: str = "SERVICE_INIT"
        self._lock = threading.Lock()

    def snapshot(self) -> tuple[str, int]:
        """Return (last_reset_reason, window_point_count) atomically."""
        with self._lock:
            return self._last_reset_reason, len(self._window)

    @staticmethod
    def _sensor_valid(sensor_data: dict) -> bool:
        if bool(sensor_data.get("sensor_stale")):
            return False
        if not bool(sensor_data.get("hr_valid")):
            return False
        if not bool(sensor_data.get("spo2_valid")):
            return False
        for key in ("heart_rate_bpm", "spo2_percent", "gsr_conductance_us"):
            if sensor_data.get(key) is None:
                return False
        return True

    def _reset(self, reason: str) -> None:
        self._window.clear()
        self._window_label = None
        self._last_reset_reason = reason

    def _compute_variance_gate(self, label: str) -> tuple[bool, dict[str, float]]:
        confidence_values: list[float] = []
        label_prob_values: list[float] = []

        for row in self._window:
            try:
                conf = float(row.get("confidence"))
                if np.isfinite(conf):
                    confidence_values.append(conf)
            except (TypeError, ValueError):
                pass

            probs = row.get("probs") or {}
            try:
                label_prob = float(probs.get(label, 0.0))
                if np.isfinite(label_prob):
                    label_prob_values.append(label_prob)
            except (TypeError, ValueError):
                pass

        conf_var = float(np.var(confidence_values)) if len(confidence_values) >= 2 else 0.0
        label_prob_var = float(np.var(label_prob_values)) if len(label_prob_values) >= 2 else 0.0
        variance_stats = {
            "confidence_variance": round(conf_var, 8),
            "label_prob_variance": round(label_prob_var, 8),
        }

        variance_enabled = bool(getattr(config, "MODEL_STABLE_VARIANCE_ENABLED", False))
        if not variance_enabled:
            return True, variance_stats

        conf_max = max(0.0, float(getattr(config, "MODEL_STABLE_CONFIDENCE_VAR_MAX", 0.0025)))
        label_prob_max = max(0.0, float(getattr(config, "MODEL_STABLE_LABEL_PROB_VAR_MAX", 0.0025)))
        variance_ok = conf_var <= conf_max and label_prob_var <= label_prob_max
        return variance_ok, variance_stats

    def update(self, result: dict, sensor_data: dict, now: float) -> dict:
        out = dict(result)
        stable_window_s = max(1.0, float(getattr(config, "MODEL_STABLE_RUN_SECONDS", 30.0)))
        runtime_state = str(out.get("model_runtime_state") or "").upper()
        label = str(out.get("model_label_top1") or "").lower()

        with self._lock:
            elapsed = 0.0
            variance_ok = False
            ready = False

            if runtime_state != "RUNNING":
                self._reset("RUNTIME_NOT_RUNNING")
            elif label not in {"anxiety", "stress", "depression"}:
                self._reset("LABEL_NOT_ELIGIBLE")
            elif not self._sensor_valid(sensor_data):
                self._reset("SENSOR_MISSING_OR_INVALID")
            else:
                if self._window_label is not None and label != self._window_label:
                    self._reset("LABEL_CHANGED")
                if self._window_label is None:
                    self._window_label = label
                self._last_reset_reason = "TRACKING"

                self._window.append(
                    {
                        "t": float(now),
                        "confidence": out.get("model_confidence_top1"),
                        "probs": {
                            "normal":     out.get("model_probs_normal"),
                            "anxiety":    out.get("model_probs_anxiety"),
                            "stress":     out.get("model_probs_stress"),
                            "depression": out.get("model_probs_depression"),
                        },
                        "chances": {
                            "normal":     out.get("model_chance_normal"),
                            "anxiety":    out.get("model_chance_anxiety"),
                            "stress":     out.get("model_chance_stress"),
                            "depression": out.get("model_chance_depression"),
                        },
                        "heart_rate_bpm":      sensor_data.get("heart_rate_bpm"),
                        "spo2_percent":        sensor_data.get("spo2_percent"),
                        "gsr_conductance_us":  sensor_data.get("gsr_conductance_us"),
                        "temperature_celsius": sensor_data.get("temperature_celsius"),
                    }
                )

                cutoff = float(now) - stable_window_s
                while self._window and float(self._window[0]["t"]) < cutoff:
                    self._window.popleft()

                if self._window:
                    elapsed = max(
                        0.0,
                        float(self._window[-1]["t"]) - float(self._window[0]["t"]),
                    )

                variance_ok, _ = self._compute_variance_gate(label=label)
                ready = elapsed >= stable_window_s and variance_ok

            out["model_stable_window_s"] = round(stable_window_s, 2)
            out["model_stable_elapsed_s"] = round(elapsed, 2)
            out["model_stable_ready"] = bool(ready)
            out["model_stable_variance_ok"] = bool(variance_ok)
            out["model_stable_reset_reason"] = self._last_reset_reason
        return out
