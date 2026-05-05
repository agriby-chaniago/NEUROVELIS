"""Realtime model inference service (multimodal: sensor + camera)."""

from __future__ import annotations

from collections import deque
import logging
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

import config
from model_inference.model_adapter import ModelAdapter, _unknown_payload
from model_inference.visual_feature_extractor import VisualFeatureExtractor

try:
    import neurokit2 as nk  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency
    nk = None

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
            features_path=getattr(config, "MODEL_INFERENCE_FEATURES_PATH", None),
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
        self._sensor_touch_prev = False
        self._sensor_touch_missing_since: Optional[float] = None
        self._sensor_touch_paused = False
        self._sensor_touch_present_hits = 0
        self._sensor_touch_absent_hits = 0
        self._class_warmup_until = 0.0
        self._stable_window: deque = deque()
        self._stable_window_label: Optional[str] = None
        self._stable_last_reset_reason = "SERVICE_INIT"
        self._latest: dict = {
            "model_label_top1": "unknown",
            "model_confidence_top1": None,
            "model_probs_normal": 0.0,
            "model_probs_anxiety": 0.0,
            "model_probs_stress": 0.0,
            "model_probs_depression": 0.0,
            "model_chance_normal": 0.0,
            "model_chance_anxiety": 0.0,
            "model_chance_stress": 0.0,
            "model_chance_depression": 0.0,
            "model_alert_active": False,
            "model_alert_reasons": "SERVICE_INIT",
            "model_face_detected": False,
            "model_face_landmarks": [],
            "model_face_backend": self._visual_extractor.health().get("backend"),
            "model_latency_ms": None,
            "model_pipeline_latency_ms": None,
            "model_loop_latency_ms": None,
            "model_timestamp_utc": None,
            "model_warmup_active": False,
            "model_warmup_remaining_s": 0.0,
            "model_runtime_state": "INIT",
            "model_runtime_reason": "SERVICE_INIT",
            "model_stable_window_s": float(getattr(config, "MODEL_STABLE_RUN_SECONDS", 30.0)),
            "model_stable_elapsed_s": 0.0,
            "model_stable_ready": False,
            "model_stable_variance_ok": False,
            "model_stable_reset_reason": "SERVICE_INIT",
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
            stable_reset_reason = self._stable_last_reset_reason
            stable_window_points = len(self._stable_window)
        return {
            "latest": latest,
            "feature_vector": fv,
            "feature_timestamp_utc": self._last_feature_timestamp_utc,
            "history_points": len(self._history),
            "adapter": self._adapter.health(),
            "visual": self._visual_extractor.health(),
            "stable": {
                "last_reset_reason": stable_reset_reason,
                "window_points": stable_window_points,
            },
        }

    @staticmethod
    def _sensor_valid_for_stability(sensor_data: dict) -> bool:
        if bool(sensor_data.get("sensor_stale")):
            return False
        if not bool(sensor_data.get("hr_valid")):
            return False
        if not bool(sensor_data.get("spo2_valid")):
            return False
        required_keys = ["heart_rate_bpm", "spo2_percent", "gsr_conductance_us"]
        for key in required_keys:
            if sensor_data.get(key) is None:
                return False
        return True

    def _reset_stable_window(self, reason: str) -> None:
        self._stable_window.clear()
        self._stable_window_label = None
        self._stable_last_reset_reason = reason

    def _compute_variance_gate(self, label: str) -> tuple[bool, dict[str, float]]:
        confidence_values: list[float] = []
        label_prob_values: list[float] = []

        for row in self._stable_window:
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

    def _update_stability_tracker(self, result: dict, sensor_data: dict, now: float) -> dict:
        out = dict(result)
        stable_window_s = max(1.0, float(getattr(config, "MODEL_STABLE_RUN_SECONDS", 30.0)))
        runtime_state = str(out.get("model_runtime_state") or "").upper()
        label = str(out.get("model_label_top1") or "").lower()

        with self._lock:
            elapsed = 0.0
            variance_ok = False
            ready = False

            if runtime_state != "RUNNING":
                self._reset_stable_window("RUNTIME_NOT_RUNNING")
            elif label not in {"anxiety", "stress", "depression"}:
                self._reset_stable_window("LABEL_NOT_ELIGIBLE")
            elif not self._sensor_valid_for_stability(sensor_data=sensor_data):
                self._reset_stable_window("SENSOR_MISSING_OR_INVALID")
            else:
                if self._stable_window_label is not None and label != self._stable_window_label:
                    self._reset_stable_window("LABEL_CHANGED")

                if self._stable_window_label is None:
                    self._stable_window_label = label
                self._stable_last_reset_reason = "TRACKING"

                self._stable_window.append(
                    {
                        "t": float(now),
                        "confidence": out.get("model_confidence_top1"),
                        "probs": {
                            "normal": out.get("model_probs_normal"),
                            "anxiety": out.get("model_probs_anxiety"),
                            "stress": out.get("model_probs_stress"),
                            "depression": out.get("model_probs_depression"),
                        },
                        "chances": {
                            "normal": out.get("model_chance_normal"),
                            "anxiety": out.get("model_chance_anxiety"),
                            "stress": out.get("model_chance_stress"),
                            "depression": out.get("model_chance_depression"),
                        },
                        "heart_rate_bpm": sensor_data.get("heart_rate_bpm"),
                        "spo2_percent": sensor_data.get("spo2_percent"),
                        "gsr_conductance_us": sensor_data.get("gsr_conductance_us"),
                        "temperature_celsius": sensor_data.get("temperature_celsius"),
                    }
                )

                cutoff = float(now) - stable_window_s
                while self._stable_window and float(self._stable_window[0]["t"]) < cutoff:
                    self._stable_window.popleft()

                if self._stable_window:
                    elapsed = max(
                        0.0,
                        float(self._stable_window[-1]["t"]) - float(self._stable_window[0]["t"]),
                    )

                variance_ok, variance_stats = self._compute_variance_gate(label=label)
                ready = elapsed >= stable_window_s and variance_ok

            out["model_stable_window_s"] = round(stable_window_s, 2)
            out["model_stable_elapsed_s"] = round(elapsed, 2)
            out["model_stable_ready"] = bool(ready)
            out["model_stable_variance_ok"] = bool(variance_ok)
            out["model_stable_reset_reason"] = self._stable_last_reset_reason
        return out

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

            self._update_sensor_touch_state(sensor_data=sensor_data, now=start)
            if start >= next_inference_at:
                if self._sensor_touch_paused:
                    result = _unknown_payload(reason="SENSOR_NOT_TOUCHED_PAUSED")
                    result["model_latency_ms"] = 0
                    result["model_pipeline_latency_ms"] = 0
                    result["model_loop_latency_ms"] = 0
                    result = self._annotate_runtime_state(result=result, now=start)
                    result["model_face_detected"] = face_detected
                    result["model_face_landmarks"] = landmarks_norm
                    result["model_face_backend"] = self._visual_extractor.health().get("backend")
                    result["model_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
                    result = self._update_stability_tracker(
                        result=result,
                        sensor_data=sensor_data,
                        now=start,
                    )

                    with self._lock:
                        self._last_feature_vector = None
                        self._last_feature_timestamp_utc = None
                        self._latest = dict(result)

                    publish_result = dict(result)
                    publish_result.pop("model_face_landmarks", None)
                    self._sensor_manager.set_model_inference(publish_result)
                    next_inference_at = start + interval

                    elapsed = time.monotonic() - start
                    self._stop_event.wait(timeout=max(0.0, visual_interval - elapsed))
                    continue

                inference_started = time.monotonic()
                feature_vector = self._build_feature_vector(min_points=min_points)
                if feature_vector is not None:
                    with self._lock:
                        self._last_feature_vector = dict(feature_vector)
                        self._last_feature_timestamp_utc = datetime.now(timezone.utc).isoformat()
                else:
                    with self._lock:
                        self._last_feature_vector = None
                        self._last_feature_timestamp_utc = None

                predict_started = time.monotonic()
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

                predict_latency_ms = int((time.monotonic() - predict_started) * 1000)
                pipeline_latency_ms = int((time.monotonic() - inference_started) * 1000)
                loop_latency_ms = int((time.monotonic() - start) * 1000)

                if pipeline_latency_ms > timeout_ms:
                    result = _unknown_payload(reason="INFERENCE_TIMEOUT")

                result["model_latency_ms"] = predict_latency_ms
                result["model_pipeline_latency_ms"] = pipeline_latency_ms
                result["model_loop_latency_ms"] = loop_latency_ms

                result = self._apply_smoothing(result)
                result = self._apply_class_warmup_gate(result=result, now=start)
                result["model_face_detected"] = face_detected
                result["model_face_landmarks"] = landmarks_norm
                result["model_face_backend"] = self._visual_extractor.health().get("backend")
                result = self._annotate_runtime_state(result=result, now=start)

                result["model_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
                result = self._update_stability_tracker(
                    result=result,
                    sensor_data=sensor_data,
                    now=start,
                )

                with self._lock:
                    self._latest = dict(result)

                # Publish inference into shared sensor snapshot for dashboard/SSE/CSV.
                publish_result = dict(result)
                publish_result.pop("model_face_landmarks", None)
                self._sensor_manager.set_model_inference(publish_result)
                next_inference_at = start + interval

            elapsed = time.monotonic() - start
            self._stop_event.wait(timeout=max(0.0, visual_interval - elapsed))

    @staticmethod
    def _is_sensor_touched(sensor_data: dict) -> bool:
        """Heuristic for active sensor touch (finger/contact present)."""
        hr_valid = bool(sensor_data.get("hr_valid"))
        spo2_valid = bool(sensor_data.get("spo2_valid"))

        hr = sensor_data.get("heart_rate_bpm")
        spo2 = sensor_data.get("spo2_percent")
        gsr = sensor_data.get("gsr_conductance_us")

        try:
            hr_val = float(hr)
        except (TypeError, ValueError):
            hr_val = None
        try:
            spo2_val = float(spo2)
        except (TypeError, ValueError):
            spo2_val = None
        try:
            gsr_val = float(gsr)
        except (TypeError, ValueError):
            gsr_val = None

        # Require valid HR+SpO2 with plausible ranges and non-null GSR.
        # This avoids false touch detection from one noisy/stale metric alone.
        hr_ok = hr_valid and hr_val is not None and 40.0 <= hr_val <= 160.0
        spo2_ok = spo2_valid and spo2_val is not None and 70.0 <= spo2_val <= 100.0
        gsr_ok = gsr_val is not None and gsr_val >= 0.0
        return hr_ok and spo2_ok and gsr_ok

    def _is_sensor_touched_debounced(self, sensor_data: dict) -> bool:
        """Debounce touch state so a transient spike doesn't trigger warmup."""
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

    def _update_sensor_touch_state(self, sensor_data: dict, now: float) -> None:
        touched = self._is_sensor_touched_debounced(sensor_data)
        grace_s = max(1.0, min(3.0, float(getattr(config, "MODEL_SENSOR_TOUCH_GRACE_S", 2.0))))
        warmup_default = max(0.0, float(getattr(config, "MODEL_CLASS_WARMUP_S", 4.0)))
        warmup_min = max(0.0, float(getattr(config, "MODEL_CLASS_WARMUP_MIN_S", warmup_default)))
        warmup_max = max(warmup_min, float(getattr(config, "MODEL_CLASS_WARMUP_MAX_S", warmup_default)))
        warmup_s = random.uniform(warmup_min, warmup_max)

        if touched:
            self._sensor_touch_missing_since = None
            if self._sensor_touch_paused or not self._sensor_touch_prev:
                self._sensor_touch_paused = False
                self._smoothed_probs = None
                self._class_warmup_until = now + warmup_s
            self._sensor_touch_prev = True
            return

        if self._sensor_touch_missing_since is None:
            self._sensor_touch_missing_since = now

        if (now - self._sensor_touch_missing_since) >= grace_s:
            self._sensor_touch_paused = True
            self._smoothed_probs = None
        self._sensor_touch_prev = False

    def _apply_class_warmup_gate(self, result: dict, now: float) -> dict:
        """Hold class output briefly after touch/start so model can stabilize."""
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

    def _apply_smoothing(self, result: dict) -> dict:
        """EMA smoothing on class probabilities to reduce UI flicker."""
        exclude_normal = bool(getattr(config, "MODEL_EXCLUDE_NORMAL_CLASS", False))
        active_classes = ["anxiety", "stress", "depression"] if exclude_normal else [
            "normal", "anxiety", "stress", "depression"
        ]
        if str(result.get("model_label_top1") or "").lower() not in active_classes:
            self._smoothed_probs = None
            return result

        probs = {
            "normal": 0.0 if exclude_normal else float(result.get("model_probs_normal", 0.0) or 0.0),
            "anxiety": float(result.get("model_probs_anxiety", 0.0) or 0.0),
            "stress": float(result.get("model_probs_stress", 0.0) or 0.0),
            "depression": float(result.get("model_probs_depression", 0.0) or 0.0),
        }
        total = sum(probs[k] for k in active_classes)
        if total <= 0.0:
            self._smoothed_probs = None
            return result

        alpha = max(0.01, min(1.0, float(config.MODEL_SMOOTHING_ALPHA)))
        if self._smoothed_probs is None:
            self._smoothed_probs = dict(probs)
        else:
            for k in self._smoothed_probs:
                self._smoothed_probs[k] = alpha * probs[k] + (1.0 - alpha) * self._smoothed_probs[k]

        if exclude_normal:
            self._smoothed_probs["normal"] = 0.0

        smooth_total = sum(self._smoothed_probs[k] for k in active_classes) or 1.0
        normalized = {
            "normal": 0.0,
            "anxiety": self._smoothed_probs["anxiety"] / smooth_total,
            "stress": self._smoothed_probs["stress"] / smooth_total,
            "depression": self._smoothed_probs["depression"] / smooth_total,
        }
        if not exclude_normal:
            normalized["normal"] = self._smoothed_probs["normal"] / smooth_total

        label_top1 = max(active_classes, key=lambda k: normalized[k])
        conf_top1 = normalized[label_top1]

        out = dict(result)
        out["model_label_top1"] = label_top1
        out["model_confidence_top1"] = round(conf_top1, 6)
        out["model_probs_normal"] = round(normalized["normal"], 6)
        out["model_probs_anxiety"] = round(normalized["anxiety"], 6)
        out["model_probs_stress"] = round(normalized["stress"], 6)
        out["model_probs_depression"] = round(normalized["depression"], 6)
        out["model_alert_active"] = (
            label_top1 != "normal" and conf_top1 >= config.MODEL_ALERT_CONFIDENCE_THRESHOLD
        )
        if out["model_alert_active"]:
            out["model_alert_reasons"] = f"CLASS={label_top1.upper()} CONF={round(conf_top1, 3)}"
        elif str(out.get("model_alert_reasons", "")).startswith("CLASS="):
            out["model_alert_reasons"] = ""
        return out

    def _annotate_runtime_state(self, result: dict, now: float) -> dict:
        """Attach explicit runtime state and warmup countdown to payload."""
        out = dict(result)
        reason = str(out.get("model_alert_reasons") or "")
        label = str(out.get("model_label_top1") or "").lower()

        remaining = max(0.0, self._class_warmup_until - now)
        warmup_active = (not self._sensor_touch_paused) and remaining > 0.0

        if self._sensor_touch_paused:
            state = "WAITING_SENSOR"
            runtime_reason = "SENSOR_NOT_TOUCHED"
            warmup_active = False
            remaining = 0.0
        elif warmup_active:
            state = "WARMUP"
            runtime_reason = f"CLASS_WARMUP:{round(remaining, 1)}s"
        elif label in config.MODEL_CLASSES:
            state = "RUNNING"
            runtime_reason = "LIVE"
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

    def _append_history(
        self,
        sensor_data: dict,
        frame: Optional[bytes],
        visual_features: Optional[dict[str, object]] = None,
    ):
        now = time.monotonic()
        hr = sensor_data.get("heart_rate_bpm")
        hr_valid = bool(sensor_data.get("hr_valid"))
        gsr = sensor_data.get("gsr_conductance_us")
        spo2 = sensor_data.get("spo2_percent")
        spo2_valid = bool(sensor_data.get("spo2_valid"))
        hr_val = float(hr) if (hr is not None and hr_valid) else None
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
                "hr_valid": hr_valid,
                "gsr": gsr_val,
                "spo2": spo2_val,
                "spo2_valid": spo2_valid,
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

    @staticmethod
    def _smooth_hr_signal(hr_signal: list[float], window: int = 15) -> list[float]:
        """Apply rolling median smoothing to HR, matching training preprocessing."""
        if not hr_signal:
            return []
        arr = np.asarray(hr_signal, dtype=float)
        w = max(1, int(window))
        half = w // 2
        out = np.empty_like(arr)
        for i in range(arr.size):
            left = max(0, i - half)
            right = min(arr.size, i + half + 1)
            out[i] = float(np.median(arr[left:right]))
        return out.tolist()

    @staticmethod
    def _interpolate_signal(values: list[Optional[float]]) -> list[float]:
        """Fill small missing gaps using linear interpolation."""
        if not values:
            return []
        normalized: list[float] = []
        for v in values:
            try:
                f = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                f = float("nan")
            normalized.append(f if np.isfinite(f) else float("nan"))
        arr = np.asarray(
            normalized,
            dtype=float,
        )
        valid = np.isfinite(arr)
        if valid.sum() == 0:
            return []

        idx = np.arange(arr.size, dtype=float)
        if valid.sum() == 1:
            arr[~valid] = float(arr[valid][0])
        else:
            arr[~valid] = np.interp(idx[~valid], idx[valid], arr[valid])
        return arr.tolist()

    @staticmethod
    def _moving_average(signal: list[float], window_size: int) -> list[float]:
        if not signal:
            return []
        arr = np.asarray(signal, dtype=float)
        w = max(1, int(window_size))
        if w == 1:
            return arr.tolist()
        pad = w // 2
        padded = np.pad(arr, (pad, pad), mode="edge")
        kernel = np.ones(w, dtype=float) / float(w)
        smooth = np.convolve(padded, kernel, mode="valid")
        if smooth.size > arr.size:
            smooth = smooth[: arr.size]
        return smooth.tolist()

    @staticmethod
    def _estimate_sampling_rate(rows: list[dict]) -> float:
        if len(rows) < 2:
            return float(getattr(config, "MODEL_SENSOR_SAMPLING_RATE_HZ", 10.0))
        elapsed = max(float(rows[-1]["t"] - rows[0]["t"]), 1e-6)
        estimated = (len(rows) - 1) / elapsed
        return max(1.0, float(estimated))

    def _eda_decomposition(self, gsr_signal: list[float], sampling_rate: float) -> tuple[list[float], list[float]]:
        """Decompose EDA into tonic and phasic components."""
        if nk is not None:
            try:
                signals, _ = nk.eda_process(
                    np.asarray(gsr_signal, dtype=float),
                    sampling_rate=float(sampling_rate),
                )
                tonic = np.asarray(signals["EDA_Tonic"], dtype=float)
                phasic = np.asarray(signals["EDA_Phasic"], dtype=float)
                return tonic.tolist(), phasic.tolist()
            except Exception as exc:
                logger.debug("ModelInferenceService: neurokit2 eda_process fallback: %s", exc)

        tonic_window = max(3, int(round(float(sampling_rate) * 4.0)))
        tonic = np.asarray(self._moving_average(gsr_signal, tonic_window), dtype=float)
        raw = np.asarray(gsr_signal, dtype=float)
        phasic = raw - tonic
        return tonic.tolist(), phasic.tolist()

    def _extract_scr_features(self, phasic_signal: list[float], sampling_rate: float) -> dict[str, float]:
        """Extract SCR count/amplitude/frequency from EDA phasic signal."""
        if nk is not None:
            try:
                signals, _ = nk.eda_peaks(
                    np.asarray(phasic_signal, dtype=float),
                    sampling_rate=float(sampling_rate),
                )
                scr_peaks = np.asarray(signals["SCR_Peaks"], dtype=float)
                scr_amplitude = np.asarray(signals["SCR_Amplitude"], dtype=float)
                peak_indices = np.where(scr_peaks == 1)[0]
                scr_count = int(peak_indices.size)

                if scr_count > 0:
                    amps = scr_amplitude[peak_indices]
                    scr_amp_mean = float(np.nanmean(amps))
                    scr_amp_max = float(np.nanmax(amps))
                else:
                    scr_amp_mean = 0.0
                    scr_amp_max = 0.0

                duration_sec = max(len(phasic_signal) / float(max(sampling_rate, 1.0)), 1e-6)
                scr_frequency = float(scr_count / duration_sec)

                return {
                    "scr_count": float(scr_count),
                    "scr_amp_mean": float(scr_amp_mean),
                    "scr_amp_max": float(scr_amp_max),
                    "scr_frequency": float(scr_frequency),
                }
            except Exception as exc:
                logger.debug("ModelInferenceService: neurokit2 eda_peaks fallback: %s", exc)

        phasic_arr = np.asarray(phasic_signal, dtype=float)
        if phasic_arr.size < 2:
            return {
                "scr_count": 0.0,
                "scr_amp_mean": 0.0,
                "scr_amp_max": 0.0,
                "scr_frequency": 0.0,
            }

        diffs = np.diff(phasic_arr)
        threshold = float(getattr(config, "MODEL_EDA_SCR_DIFF_THRESHOLD_US", 0.03))
        amps = diffs[diffs > threshold]
        scr_count = int(amps.size)
        scr_amp_mean = float(np.mean(amps)) if amps.size > 0 else 0.0
        scr_amp_max = float(np.max(amps)) if amps.size > 0 else 0.0
        duration_sec = max(phasic_arr.size / float(max(sampling_rate, 1.0)), 1e-6)
        scr_frequency = float(scr_count / duration_sec)

        return {
            "scr_count": float(scr_count),
            "scr_amp_mean": float(scr_amp_mean),
            "scr_amp_max": float(scr_amp_max),
            "scr_frequency": float(scr_frequency),
        }

    def _build_feature_vector(self, min_points: int) -> Optional[dict[str, float]]:
        rows = list(self._history)
        if len(rows) < min_points:
            return None

        window_seconds_cfg = max(5.0, float(getattr(config, "MODEL_FEATURE_WINDOW_S", 30.0)))
        t_latest = float(rows[-1]["t"])
        recent_rows = [r for r in rows if (t_latest - float(r["t"])) <= window_seconds_cfg]
        if len(recent_rows) < min_points:
            return None

        # Match training pipeline semantics: keep rows with valid HR for feature windows.
        recent_rows = [r for r in recent_rows if bool(r.get("hr_valid"))]
        if len(recent_rows) < min_points:
            return None

        if bool(getattr(config, "MODEL_REQUIRE_SPO2_VALID_FOR_FEATURE_WINDOW", False)):
            recent_rows = [r for r in recent_rows if bool(r.get("spo2_valid"))]
            if len(recent_rows) < min_points:
                return None

        effective_window_seconds = max(
            float(recent_rows[-1]["t"] - recent_rows[0]["t"]),
            0.0,
        )
        window_coverage_ratio = min(
            1.0,
            effective_window_seconds / max(window_seconds_cfg, 1e-6),
        )
        min_coverage = max(
            0.1,
            min(1.0, float(getattr(config, "MODEL_MIN_WINDOW_COVERAGE_RATIO", 0.8))),
        )
        if window_coverage_ratio < min_coverage:
            return None

        hr_values = self._interpolate_signal([r.get("hr") for r in recent_rows])
        gsr_values = self._interpolate_signal([r.get("gsr") for r in recent_rows])
        spo2_values = [
            float(r["spo2"])
            for r in recent_rows
            if r["spo2"] is not None and np.isfinite(float(r["spo2"]))
        ]
        ear_values = [
            float(r["ear"])
            for r in recent_rows
            if r["ear"] is not None and np.isfinite(float(r["ear"]))
        ]
        mar_values = [
            float(r["mar"])
            for r in recent_rows
            if r["mar"] is not None and np.isfinite(float(r["mar"]))
        ]
        motion_values = [
            float(r["motion"])
            for r in recent_rows
            if r["motion"] is not None and np.isfinite(float(r["motion"]))
        ]

        if len(hr_values) < min_points or len(gsr_values) < min_points:
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

        if len(hr_values) < 5 or len(gsr_values) < 5:
            return None

        if bool(getattr(config, "MODEL_USE_FIXED_SENSOR_SAMPLING_RATE", True)):
            sampling_rate = max(
                1.0,
                float(getattr(config, "MODEL_SENSOR_SAMPLING_RATE_HZ", 10.0)),
            )
        else:
            sampling_rate = self._estimate_sampling_rate(recent_rows)
        hr_values = self._smooth_hr_signal(hr_values, window=15)
        tonic_values, phasic_values = self._eda_decomposition(gsr_values, sampling_rate=sampling_rate)
        if not tonic_values or not phasic_values:
            return None
        scr = self._extract_scr_features(phasic_values, sampling_rate=sampling_rate)

        window_seconds = max(float(recent_rows[-1]["t"] - recent_rows[0]["t"]), 1.0)

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

        blink_count = sum(
            float(r.get("blink_event", 0.0) or 0.0)
            for r in recent_rows
            if np.isfinite(float(r.get("blink_event", 0.0) or 0.0))
        )
        # Keep blink_rate semantics aligned with training notebook (blinks/second).
        blink_rate = blink_count / window_seconds

        eda_tonic_mean = self._mean(tonic_values)
        eda_phasic_mean = self._mean(phasic_values)
        scr_count = float(scr.get("scr_count", 0.0))
        scr_amp_max = float(scr.get("scr_amp_max", 0.0))
        scr_amp_mean = float(scr.get("scr_amp_mean", 0.0))
        scr_frequency = float(scr.get("scr_frequency", 0.0))

        motion_rate = sum(float(r["frame_changed"]) for r in recent_rows) / len(recent_rows)
        motion_per_hr = motion_rate / (abs(hr_mean) + 1e-6)

        spo2_mean = self._mean(spo2_values) if spo2_values else 0.0

        return {
            # Feature set aligned with trained multimodal model.
            "eda_tonic_mean": eda_tonic_mean,
            "eda_phasic_mean": eda_phasic_mean,
            "scr_amp_max": scr_amp_max,
            "scr_amp_mean": scr_amp_mean,
            "scr_frequency": scr_frequency,
            "scr_count": scr_count,
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
            "window_effective_seconds": effective_window_seconds,
            "window_expected_seconds": float(window_seconds_cfg),
            "window_coverage_ratio": window_coverage_ratio,
        }
