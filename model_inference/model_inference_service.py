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
from model_inference.visual_feature_extractor import VisualFeatureExtractor

logger = logging.getLogger(__name__)


class ModelInferenceService:
    """Runs periodic inference and publishes latest model outputs."""

    def __init__(self, sensor_manager, camera_reader=None):
        self._sensor_manager = sensor_manager
        self._camera_reader = camera_reader
        self._adapter = ModelAdapter(
            backend=config.MODEL_INFERENCE_BACKEND,
            model_path=config.MODEL_INFERENCE_MODEL_PATH,
            scaler_path=config.MODEL_INFERENCE_SCALER_PATH,
        )
        self._visual_extractor = VisualFeatureExtractor(
            enabled=getattr(config, "MODEL_FACE_MESH_ENABLED", True),
            landmarker_model_path=getattr(
                config, "MODEL_FACE_LANDMARKER_MODEL_PATH", ""
            ),
            min_detection_confidence=getattr(
                config, "MODEL_FACE_MESH_MIN_DETECTION_CONFIDENCE", 0.5
            ),
            min_tracking_confidence=getattr(
                config, "MODEL_FACE_MESH_MIN_TRACKING_CONFIDENCE", 0.5
            ),
            blink_ear_threshold=getattr(config, "MODEL_BLINK_EAR_THRESHOLD", 0.21),
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
            "model_face_detected": False,
            "model_face_landmarks": [],
            "model_face_backend": self._visual_extractor.health().get("backend"),
            "model_latency_ms": None,
            "model_timestamp_utc": None,
        }
        self._mesh_latest: dict = {
            "model_face_detected": False,
            "model_face_landmarks": [],
            "model_face_backend": self._visual_extractor.health().get("backend"),
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
        self._visual_extractor.close()
        logger.info("ModelInferenceService stopped")

    def get_latest(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def get_latest_compact(self) -> dict:
        with self._lock:
            payload = dict(self._latest)
        # Large landmark arrays are sent through a dedicated mesh stream.
        payload.pop("model_face_landmarks", None)
        return payload

    def get_mesh_latest(self) -> dict:
        with self._lock:
            return dict(self._mesh_latest)

    def get_mesh_topology(self) -> list[list[int]]:
        return self._visual_extractor.tesselation_edges()

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
            "visual": self._visual_extractor.health(),
        }

    def _loop(self):
        interval = max(0.1, float(config.MODEL_INFERENCE_INTERVAL_S))
        visual_interval = max(
            0.05,
            float(getattr(config, "MODEL_VISUAL_UPDATE_INTERVAL_S", 0.10)),
        )
        timeout_ms = max(1, int(config.MODEL_INFERENCE_TIMEOUT_MS))
        min_points = max(3, int(config.MODEL_INFERENCE_MIN_POINTS))
        next_inference_at = 0.0

        while not self._stop_event.is_set():
            start = time.monotonic()
            sensor_data = self._sensor_manager.get_latest()
            frame = self._camera_reader.get_frame() if self._camera_reader is not None else None
            visual_features = self._visual_extractor.extract(frame)
            face_detected = bool(visual_features.get("face_detected"))
            landmarks_norm = visual_features.get("landmarks_norm")
            if not isinstance(landmarks_norm, list):
                landmarks_norm = []

            mesh_timestamp_utc = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._mesh_latest = {
                    "model_face_detected": face_detected,
                    "model_face_landmarks": list(landmarks_norm),
                    "model_face_backend": self._visual_extractor.health().get("backend"),
                    "model_timestamp_utc": mesh_timestamp_utc,
                }

            self._append_history(
                sensor_data=sensor_data,
                frame=frame,
                visual_features=visual_features,
            )
            if start >= next_inference_at:
                feature_vector = self._build_feature_vector(min_points=min_points)
                if feature_vector is not None:
                    with self._lock:
                        self._last_feature_vector = dict(feature_vector)
                        self._last_feature_timestamp_utc = datetime.now(timezone.utc).isoformat()
                else:
                    with self._lock:
                        self._last_feature_vector = None
                        self._last_feature_timestamp_utc = None

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
                result["model_face_detected"] = face_detected
                result["model_face_landmarks"] = landmarks_norm
                result["model_face_backend"] = self._visual_extractor.health().get("backend")

                result["model_timestamp_utc"] = datetime.now(timezone.utc).isoformat()

                with self._lock:
                    self._latest = dict(result)

                # Publish inference into shared sensor snapshot for dashboard/SSE/CSV.
                publish_result = dict(result)
                publish_result.pop("model_face_landmarks", None)
                self._sensor_manager.set_model_inference(publish_result)
                next_inference_at = start + interval

            elapsed = time.monotonic() - start
            self._stop_event.wait(timeout=max(0.0, visual_interval - elapsed))

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

    def _append_history(
        self,
        sensor_data: dict,
        frame: Optional[bytes],
        visual_features: Optional[dict[str, object]] = None,
    ):
        now = time.monotonic()
        hr = sensor_data.get("heart_rate_bpm")
        gsr = sensor_data.get("gsr_conductance_us")
        spo2 = sensor_data.get("spo2_percent")
        spo2_valid = bool(sensor_data.get("spo2_valid"))
        hr_val = float(hr) if hr is not None else None
        gsr_val = float(gsr) if gsr is not None else None
        spo2_val = float(spo2) if (spo2 is not None and spo2_valid) else None

        visual = visual_features or {}
        ear_val = visual.get("ear")
        mar_val = visual.get("mar")
        motion_val = visual.get("motion")
        blink_event = visual.get("blink_event")

        ear_f = float(ear_val) if ear_val is not None else None
        mar_f = float(mar_val) if mar_val is not None else None
        motion_f = float(motion_val) if motion_val is not None else None
        blink_f = float(blink_event) if blink_event is not None else 0.0

        frame_changed = 1.0 if (frame is not None and frame != self._prev_frame) else 0.0
        self._prev_frame = frame

        self._history.append(
            {
                "t": now,
                "hr": hr_val,
                "gsr": gsr_val,
                "spo2": spo2_val,
                "ear": ear_f,
                "mar": mar_f,
                "motion": motion_f,
                "blink_event": blink_f,
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

        # Use only the freshest tail window for strict realtime validity checks.
        recent_rows = rows[-min_points:]

        hr_values = [r["hr"] for r in rows if r["hr"] is not None]
        gsr_values = [r["gsr"] for r in rows if r["gsr"] is not None]
        spo2_values = [r["spo2"] for r in rows if r["spo2"] is not None]
        ear_values = [r["ear"] for r in rows if r["ear"] is not None]
        mar_values = [r["mar"] for r in rows if r["mar"] is not None]
        motion_values = [r["motion"] for r in rows if r["motion"] is not None]

        recent_hr_values = [r["hr"] for r in recent_rows if r["hr"] is not None]
        recent_gsr_values = [r["gsr"] for r in recent_rows if r["gsr"] is not None]
        recent_ear_values = [r["ear"] for r in recent_rows if r["ear"] is not None]
        recent_mar_values = [r["mar"] for r in recent_rows if r["mar"] is not None]
        recent_motion_values = [r["motion"] for r in recent_rows if r["motion"] is not None]

        if len(hr_values) < min_points or len(gsr_values) < min_points:
            return None
        if len(recent_hr_values) < min_points or len(recent_gsr_values) < min_points:
            return None

        visual_runtime_enabled = bool(
            self._visual_extractor.health().get("enabled_runtime")
        )
        if not visual_runtime_enabled:
            return None

        # Visual facemesh features require at least a small stable sample.
        min_visual_points = max(3, min_points // 2)
        if (
            len(ear_values) < min_visual_points
            or len(mar_values) < min_visual_points
            or len(motion_values) < min_visual_points
        ):
            return None
        if (
            len(recent_ear_values) < min_visual_points
            or len(recent_mar_values) < min_visual_points
            or len(recent_motion_values) < min_visual_points
        ):
            return None

        window_seconds = max(rows[-1]["t"] - rows[0]["t"], 1.0)

        hr_mean = self._mean(hr_values)
        hr_std = self._std(hr_values)
        hr_slope = self._slope(hr_values)
        hr_range = max(hr_values) - min(hr_values)
        hr_var_ratio = hr_std / (abs(hr_mean) + 1e-6)

        gsr_mean = self._mean(gsr_values)
        gsr_std = self._std(gsr_values)
        gsr_slope = self._slope(gsr_values)
        gsr_range = max(gsr_values) - min(gsr_values)

        ear_mean = self._mean(ear_values)
        ear_std = self._std(ear_values)
        mar_mean = self._mean(mar_values)
        mar_std = self._std(mar_values)
        motion_mean = self._mean(motion_values)
        motion_std = self._std(motion_values)

        blink_count = sum(float(r.get("blink_event", 0.0) or 0.0) for r in rows)
        blink_rate = (blink_count * 60.0) / window_seconds

        gsr_diffs = [
            gsr_values[idx] - gsr_values[idx - 1]
            for idx in range(1, len(gsr_values))
        ]
        eda_phasic_mean = self._mean([abs(v) for v in gsr_diffs]) if gsr_diffs else 0.0
        scr_threshold = float(getattr(config, "MODEL_EDA_SCR_DIFF_THRESHOLD_US", 0.03))
        scr_amps = [v for v in gsr_diffs if v > scr_threshold]
        scr_count = len(scr_amps)
        scr_amp_max = max(scr_amps) if scr_amps else 0.0
        scr_amp_mean = self._mean(scr_amps) if scr_amps else 0.0
        scr_frequency = scr_count / window_seconds

        motion_rate = sum(r["frame_changed"] for r in rows) / len(rows)
        motion_per_hr = motion_rate / (abs(hr_mean) + 1e-6)

        spo2_mean = self._mean(spo2_values) if spo2_values else 0.0

        return {
            # Feature set aligned with trained multimodal model.
            "eda_tonic_mean": gsr_mean,
            "eda_phasic_mean": eda_phasic_mean,
            "scr_amp_max": scr_amp_max,
            "scr_amp_mean": scr_amp_mean,
            "scr_frequency": scr_frequency,
            "scr_count": float(scr_count),
            "hr_mean": hr_mean,
            "hr_std": hr_std,
            "hr_slope": hr_slope,
            "hr_range": hr_range,
            "hr_var_ratio": hr_var_ratio,
            "ear_mean": ear_mean,
            "ear_std": ear_std,
            "mar_mean": mar_mean,
            "mar_std": mar_std,
            "motion_mean": motion_mean,
            "motion_std": motion_std,
            "blink_rate": blink_rate,
            # Auxiliary diagnostics/fallback features.
            "gsr_mean": gsr_mean,
            "gsr_std": gsr_std,
            "gsr_slope": gsr_slope,
            "gsr_range": gsr_range,
            "spo2_mean": spo2_mean,
            "motion_per_hr": motion_per_hr,
        }
