"""Realtime model inference service (multimodal: sensor + camera)."""

from __future__ import annotations

from collections import deque
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

import config
from model_inference.model_adapter import ModelAdapter, _unknown_payload
from model_inference.visual_feature_extractor import VisualFeatureExtractor
from model_inference.smoother import _Smoother
from model_inference.warmup_gate import _WarmupGate
from model_inference.stability_tracker import _StabilityTracker

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
        self._prev_frame: Optional[bytes] = None  # kept for compatibility; unused after fix
        self._last_feature_vector: Optional[dict[str, float]] = None
        self._last_feature_timestamp_utc: Optional[str] = None
        self._smoother = _Smoother()
        self._warmup_gate = _WarmupGate()
        self._stability = _StabilityTracker()
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
        self._result_seq: int = 0
        self._result_cond: threading.Condition = threading.Condition()

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
        stable_reset_reason, stable_window_points = self._stability.snapshot()
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

    def _update_stability_tracker(self, result: dict, sensor_data: dict, now: float) -> dict:
        return self._stability.update(result, sensor_data, now)

    def _loop(self):
        interval = max(0.1, float(config.MODEL_INFERENCE_INTERVAL_S))
        visual_interval = max(
            0.05,
            float(getattr(config, "MODEL_VISUAL_UPDATE_INTERVAL_S", 0.10)),
        )
        timeout_ms = max(1, int(config.MODEL_INFERENCE_TIMEOUT_MS))
        min_points = max(3, int(config.MODEL_INFERENCE_MIN_POINTS))
        next_inference_at = 0.0
        last_visual_time: float = 0.0
        last_frame_seq: int = 0

        while not self._stop_event.is_set():
            start = time.monotonic()
            sensor_data = self._sensor_manager.get_latest()

            # Event-driven frame acquisition: blocks until new frame or timeout.
            # Replaces fixed-timer polling — inference thread wakes on actual frames.
            if self._camera_reader is not None:
                last_frame_seq, frame = self._camera_reader.wait_new_frame(
                    last_frame_seq, timeout=visual_interval
                )
            else:
                frame = None

            # Soft throttle: cap visual extraction at ~20 Hz regardless of camera FPS.
            # Without this, event-driven at 60fps → 6x current CPU load.
            now = time.monotonic()
            if frame is not None and (now - last_visual_time) < 0.05:
                # Still record sensor data on throttled frames so history stays dense.
                # Visual features omitted (empty dict) — only extraction is skipped.
                self._append_history(sensor_data=sensor_data, frame=frame, visual_features={})
                continue
            if frame is not None:
                last_visual_time = now

            # Cache backend once per iteration — avoids 3 redundant dict allocs.
            _vfe_backend = self._visual_extractor.health().get("backend")

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
                    "model_face_backend": _vfe_backend,
                    "model_timestamp_utc": mesh_timestamp_utc,
                }

            self._append_history(
                sensor_data=sensor_data,
                frame=frame,
                visual_features=visual_features,
            )

            if self._warmup_gate.update(sensor_data=sensor_data, now=start):
                self._smoother.reset()
            if start >= next_inference_at:
                if self._warmup_gate.is_paused:
                    result = _unknown_payload(reason="SENSOR_NOT_TOUCHED_PAUSED")
                    result["model_latency_ms"] = 0
                    result["model_pipeline_latency_ms"] = 0
                    result["model_loop_latency_ms"] = 0
                    result = self._warmup_gate.annotate_runtime(result, start)
                    result["model_face_detected"] = face_detected
                    result["model_face_count"]    = int(visual_features.get("face_count", 0))
                    result["model_face_landmarks"] = landmarks_norm
                    result["model_face_backend"] = _vfe_backend
                    result["model_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
                    result = self._update_stability_tracker(
                        result=result,
                        sensor_data=sensor_data,
                        now=start,
                    )

                    publish_result = dict(result)
                    publish_result.pop("model_face_landmarks", None)
                    with self._lock:
                        self._last_feature_vector = None
                        self._last_feature_timestamp_utc = None
                        self._latest = dict(result)
                        self._sensor_manager.set_model_inference(publish_result)

                    with self._result_cond:
                        self._result_seq += 1
                        self._result_cond.notify_all()

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

                result = self._smoother.apply(result)
                result = self._warmup_gate.apply_gate(result, start)
                result["model_face_detected"] = face_detected
                result["model_face_count"]    = int(visual_features.get("face_count", 0))
                result["model_face_landmarks"] = landmarks_norm
                result["model_face_backend"] = _vfe_backend
                result = self._warmup_gate.annotate_runtime(result, start)

                result["model_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
                result = self._update_stability_tracker(
                    result=result,
                    sensor_data=sensor_data,
                    now=start,
                )

                # Build publish snapshot before acquiring lock (landmarks removed for
                # sensor snapshot — too large for CSV/SSE consumers).
                publish_result = dict(result)
                publish_result.pop("model_face_landmarks", None)

                with self._lock:
                    self._latest = dict(result)
                    # Publish under the same lock so dashboard consumers reading
                    # _latest and sensor snapshot always see a consistent frame.
                    # set_model_inference() only acquires sensor_manager._lock (no
                    # callback / SSE path) — no deadlock risk.
                    self._sensor_manager.set_model_inference(publish_result)

                with self._result_cond:
                    self._result_seq += 1
                    self._result_cond.notify_all()
                next_inference_at = start + interval

            # When camera is available, wait_new_frame at top already paces the loop.
            # Only add explicit wait when camera is absent.
            if self._camera_reader is None:
                elapsed = time.monotonic() - start
                self._stop_event.wait(timeout=max(0.0, visual_interval - elapsed))

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

        # wait_new_frame returns None on timeout → non-None frame is always a new frame.
        # O(1) check replaces the previous O(n) byte comparison.
        frame_changed = 1.0 if frame is not None else 0.0

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
        if len(self._history) < min_points:
            return None
        rows = list(self._history)

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
