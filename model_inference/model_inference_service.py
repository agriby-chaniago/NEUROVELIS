"""Realtime model inference service (multimodal: sensor + camera)."""

from __future__ import annotations

from collections import deque
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import config
from model_inference.model_adapter import ModelAdapter, _unknown_payload

logger = logging.getLogger(__name__)


class ModelInferenceService:
    """Runs periodic inference and publishes latest model outputs."""

    def __init__(self, sensor_manager, camera_reader=None):
        self._sensor_manager = sensor_manager
        self._camera_reader = camera_reader
        self._adapter = ModelAdapter(
            backend=config.MODEL_INFERENCE_BACKEND,
            model_path=config.MODEL_INFERENCE_MODEL_PATH,
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._history: deque = deque()
        self._prev_frame: Optional[bytes] = None
        self._smoothed_probs: Optional[dict[str, float]] = None
        self._last_feature_vector: Optional[dict[str, float]] = None
        self._last_feature_timestamp_utc: Optional[str] = None
        self._latest: dict = {
            "model_label_top1": "unknown",
            "model_confidence_top1": None,
            "model_probs_normal": 0.0,
            "model_probs_anxiety": 0.0,
            "model_probs_stress": 0.0,
            "model_probs_depression": 0.0,
            "model_alert_active": False,
            "model_alert_reasons": "SERVICE_INIT",
            "model_latency_ms": None,
            "model_timestamp_utc": None,
        }

    def start(self):
        if not config.MODEL_INFERENCE_ENABLED:
            logger.info("ModelInferenceService disabled by config")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._adapter.load_model()
        self._thread = threading.Thread(
            target=self._loop,
            name="model-inference",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "ModelInferenceService started (backend=%s, interval=%.2fs)",
            config.MODEL_INFERENCE_BACKEND,
            config.MODEL_INFERENCE_INTERVAL_S,
        )

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("ModelInferenceService stopped")

    def get_latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def health(self) -> dict:
        """Return service health for /health endpoint."""
        with self._lock:
            latest = dict(self._latest)
        adapter_health = self._adapter.health()
        return {
            "sensor": "model_inference",
            "ok": latest.get("model_label_top1") != "unknown" or not adapter_health.get("load_error"),
            "backend": adapter_health.get("backend"),
            "model_loaded": adapter_health.get("model_loaded"),
            "scaler_loaded": adapter_health.get("scaler_loaded"),
            "last_error": adapter_health.get("load_error") or latest.get("model_alert_reasons"),
            "last_label": latest.get("model_label_top1"),
            "last_confidence": latest.get("model_confidence_top1"),
            "last_latency_ms": latest.get("model_latency_ms"),
        }

    def debug_snapshot(self) -> dict:
        """Return detailed model debug payload for troubleshooting."""
        with self._lock:
            latest = dict(self._latest)
            fv = dict(self._last_feature_vector) if self._last_feature_vector else None
        return {
            "latest": latest,
            "feature_vector": fv,
            "feature_timestamp_utc": self._last_feature_timestamp_utc,
            "history_points": len(self._history),
            "adapter": self._adapter.health(),
        }

    def _loop(self):
        interval = max(0.1, float(config.MODEL_INFERENCE_INTERVAL_S))
        timeout_ms = max(1, int(config.MODEL_INFERENCE_TIMEOUT_MS))
        min_points = max(3, int(config.MODEL_INFERENCE_MIN_POINTS))

        while not self._stop_event.is_set():
            start = time.monotonic()
            sensor_data = self._sensor_manager.get_latest()
            frame = self._camera_reader.get_frame() if self._camera_reader is not None else None
            self._append_history(sensor_data=sensor_data, frame=frame)
            feature_vector = self._build_feature_vector(min_points=min_points)
            if feature_vector is not None:
                with self._lock:
                    self._last_feature_vector = dict(feature_vector)
                    self._last_feature_timestamp_utc = datetime.now(timezone.utc).isoformat()

            try:
                result = self._adapter.predict(
                    sensor_data=sensor_data,
                    frame_bytes=frame,
                    feature_vector=feature_vector,
                )
            except NotImplementedError as exc:
                result = _unknown_payload(reason="BACKEND_NOT_IMPLEMENTED")
                logger.warning("Model backend not implemented: %s", exc)
            except Exception as exc:
                result = _unknown_payload(reason="INFERENCE_ERROR")
                logger.error("Model inference error: %s", exc)

            latency_ms = int((time.monotonic() - start) * 1000)
            if latency_ms > timeout_ms:
                result = _unknown_payload(reason="INFERENCE_TIMEOUT")
                result["model_latency_ms"] = latency_ms
            else:
                result["model_latency_ms"] = latency_ms

            result = self._apply_smoothing(result)

            result["model_timestamp_utc"] = datetime.now(timezone.utc).isoformat()

            with self._lock:
                self._latest = dict(result)

            # Publish inference into shared sensor snapshot for dashboard/SSE/CSV.
            self._sensor_manager.set_model_inference(result)

            elapsed = time.monotonic() - start
            self._stop_event.wait(timeout=max(0.0, interval - elapsed))

    def _apply_smoothing(self, result: dict) -> dict:
        """EMA smoothing on class probabilities to reduce UI flicker."""
        probs = {
            "normal": float(result.get("model_probs_normal", 0.0) or 0.0),
            "anxiety": float(result.get("model_probs_anxiety", 0.0) or 0.0),
            "stress": float(result.get("model_probs_stress", 0.0) or 0.0),
            "depression": float(result.get("model_probs_depression", 0.0) or 0.0),
        }
        total = sum(probs.values())
        if total <= 0.0 or result.get("model_label_top1") == "unknown":
            self._smoothed_probs = None
            return result

        alpha = max(0.01, min(1.0, float(config.MODEL_SMOOTHING_ALPHA)))
        if self._smoothed_probs is None:
            self._smoothed_probs = dict(probs)
        else:
            for k in self._smoothed_probs:
                self._smoothed_probs[k] = alpha * probs[k] + (1.0 - alpha) * self._smoothed_probs[k]

        smooth_total = sum(self._smoothed_probs.values()) or 1.0
        normalized = {k: v / smooth_total for k, v in self._smoothed_probs.items()}
        label_top1 = max(normalized, key=lambda k: normalized[k])
        conf_top1 = normalized[label_top1]

        out = dict(result)
        out["model_label_top1"] = label_top1
        out["model_confidence_top1"] = round(conf_top1, 4)
        out["model_probs_normal"] = round(normalized["normal"], 4)
        out["model_probs_anxiety"] = round(normalized["anxiety"], 4)
        out["model_probs_stress"] = round(normalized["stress"], 4)
        out["model_probs_depression"] = round(normalized["depression"], 4)
        out["model_alert_active"] = (
            label_top1 != "normal" and conf_top1 >= config.MODEL_ALERT_CONFIDENCE_THRESHOLD
        )
        if out["model_alert_active"]:
            out["model_alert_reasons"] = f"CLASS={label_top1.upper()} CONF={round(conf_top1, 3)}"
        elif str(out.get("model_alert_reasons", "")).startswith("CLASS="):
            out["model_alert_reasons"] = ""
        return out

    def _append_history(self, sensor_data: dict, frame: Optional[bytes]):
        now = time.monotonic()
        hr = sensor_data.get("heart_rate_bpm")
        gsr = sensor_data.get("gsr_conductance_us")
        hr_val = float(hr) if hr is not None else None
        gsr_val = float(gsr) if gsr is not None else None
        frame_changed = 1.0 if (frame is not None and frame != self._prev_frame) else 0.0
        self._prev_frame = frame

        self._history.append(
            {
                "t": now,
                "hr": hr_val,
                "gsr": gsr_val,
                "frame_changed": frame_changed,
            }
        )

        # Keep only last ~60 seconds (more than enough for engineered features).
        cutoff = now - 60.0
        while self._history and self._history[0]["t"] < cutoff:
            self._history.popleft()

    @staticmethod
    def _mean(values: list[float]) -> float:
        return sum(values) / len(values)

    @staticmethod
    def _std(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        m = sum(values) / len(values)
        return (sum((x - m) ** 2 for x in values) / (len(values) - 1)) ** 0.5

    @staticmethod
    def _slope(values: list[float]) -> float:
        n = len(values)
        if n < 2:
            return 0.0
        x_mean = (n - 1) / 2.0
        y_mean = sum(values) / n
        num = 0.0
        den = 0.0
        for idx, val in enumerate(values):
            dx = idx - x_mean
            num += dx * (val - y_mean)
            den += dx * dx
        return num / den if den > 0 else 0.0

    def _build_feature_vector(self, min_points: int) -> Optional[dict[str, float]]:
        rows = list(self._history)
        if len(rows) < min_points:
            return None

        hr_values = [r["hr"] for r in rows if r["hr"] is not None]
        gsr_values = [r["gsr"] for r in rows if r["gsr"] is not None]
        if len(hr_values) < min_points or len(gsr_values) < min_points:
            return None

        hr_mean = self._mean(hr_values)
        hr_std = self._std(hr_values)
        hr_slope = self._slope(hr_values)
        hr_delta = hr_values[-1] - hr_values[0]
        hr_range = max(hr_values) - min(hr_values)

        gsr_mean = self._mean(gsr_values)
        gsr_std = self._std(gsr_values)
        gsr_slope = self._slope(gsr_values)
        gsr_delta = gsr_values[-1] - gsr_values[0]
        gsr_range = max(gsr_values) - min(gsr_values)

        motion_rate = sum(r["frame_changed"] for r in rows) / len(rows)
        motion_per_hr = motion_rate / (hr_mean + 1e-6)

        return {
            "hr_mean": hr_mean,
            "hr_std": hr_std,
            "hr_slope": hr_slope,
            "hr_delta": hr_delta,
            "hr_range": hr_range,
            "gsr_mean": gsr_mean,
            "gsr_std": gsr_std,
            "gsr_slope": gsr_slope,
            "gsr_delta": gsr_delta,
            "gsr_range": gsr_range,
            "motion_per_hr": motion_per_hr,
        }
