"""Model adapter abstraction for realtime 4-class inference.

This module is framework-agnostic by design. The default backend is
`rule_based` so the dashboard can run before a trained model artifact is
available. Future backends (pytorch/onnx/tensorflow) can plug into the same
`predict` interface.
"""

from __future__ import annotations

import math
import joblib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _softmax(scores: dict[str, float]) -> dict[str, float]:
    max_v = max(scores.values())
    exp_scores = {k: math.exp(v - max_v) for k, v in scores.items()}
    total = sum(exp_scores.values())
    if total <= 0:
        n = len(exp_scores)
        return {k: 1.0 / n for k in exp_scores}
    return {k: v / total for k, v in exp_scores.items()}


def _stabilize_class_probs(probs: dict[str, float]) -> dict[str, float]:
    """Normalize probabilities and optionally exclude normal class."""
    classes = list(config.MODEL_CLASSES)
    safe = {k: max(0.0, float(probs.get(k, 0.0))) for k in classes}
    total = sum(safe.values())
    if total <= 0.0:
        uniform = 1.0 / max(len(classes), 1)
        return {k: uniform for k in classes}

    safe = {k: v / total for k, v in safe.items()}

    if bool(getattr(config, "MODEL_EXCLUDE_NORMAL_CLASS", False)):
        focus_classes = ["anxiety", "stress", "depression"]
        focus_total = sum(safe.get(k, 0.0) for k in focus_classes)
        if focus_total <= 0.0:
            return {
                "normal": 0.0,
                "anxiety": 0.0,
                "stress": 0.0,
                "depression": 0.0,
            }
        return {
            "normal": 0.0,
            "anxiety": safe.get("anxiety", 0.0) / focus_total,
            "stress": safe.get("stress", 0.0) / focus_total,
            "depression": safe.get("depression", 0.0) / focus_total,
        }

    return safe


def _unknown_payload(reason: str) -> dict:
    return {
        "model_label_top1": "unknown",
        "model_confidence_top1": None,
        "model_probs_normal": 0.0,
        "model_probs_anxiety": 0.0,
        "model_probs_stress": 0.0,
        "model_probs_depression": 0.0,
        "model_alert_active": False,
        "model_alert_reasons": reason,
    }


def _apply_confidence_guard(result: dict) -> dict:
    """Force unknown label when confidence is too low."""
    conf = result.get("model_confidence_top1")
    label = result.get("model_label_top1")
    if label in config.MODEL_CLASSES and conf is not None:
        try:
            conf_f = float(conf)
        except Exception:
            return result
        if conf_f < config.MODEL_UNKNOWN_CONFIDENCE_THRESHOLD:
            guarded = dict(result)
            guarded["model_label_top1"] = "unknown"
            guarded["model_alert_active"] = False
            guarded["model_alert_reasons"] = f"LOW_CONF:{round(conf_f, 3)}"
            return guarded
    return result


@dataclass
class ModelAdapter:
    """Simple adapter that dispatches to the configured backend type."""

    backend: str = config.MODEL_INFERENCE_BACKEND
    model_path: Optional[str] = config.MODEL_INFERENCE_MODEL_PATH
    scaler_path: Optional[str] = config.MODEL_INFERENCE_SCALER_PATH

    def __post_init__(self):
        self._model = None
        self._scaler = None
        self._classes: list[str] = list(config.MODEL_CLASSES)
        self._feature_names: list[str] = []
        self._load_error: Optional[str] = None

    def load_model(self):
        """Load model backend resources.

        For MVP, `rule_based` needs no artifact. Other backends are placeholders
        and can be implemented later without changing callers.
        """
        backend = (self.backend or "").lower().strip()
        if backend in ("", "rule_based"):
            self.backend = "rule_based"
            return None
        if backend == "sklearn_pickle":
            self.backend = "sklearn_pickle"
            self._load_sklearn_artifacts()
            return None
        # Keep non-sklearn backends explicit for future integration.
        raise NotImplementedError(f"Backend '{backend}' is not implemented yet.")

    def _load_sklearn_artifacts(self):
        model_path = Path(self.model_path or "")
        scaler_path = Path(self.scaler_path or "")
        if not model_path.exists():
            self._load_error = f"MODEL_FILE_NOT_FOUND:{model_path}"
            return
        if not scaler_path.exists():
            self._load_error = f"SCALER_FILE_NOT_FOUND:{scaler_path}"
            return

        try:
            self._model = joblib.load(model_path)
            self._scaler = joblib.load(scaler_path)
            self._classes = [str(c) for c in getattr(self._model, "classes_", config.MODEL_CLASSES)]
            self._feature_names = [
                str(f) for f in getattr(self._scaler, "feature_names_in_", [])
            ]
            self._load_error = None
        except ModuleNotFoundError as exc:
            self._load_error = f"SKLEARN_DEPENDENCY_MISSING:{exc}"
        except Exception as exc:
            self._load_error = f"MODEL_LOAD_ERROR:{exc}"

    def health(self) -> dict:
        """Return adapter state for diagnostics."""
        return {
            "backend": self.backend,
            "model_path": self.model_path,
            "scaler_path": self.scaler_path,
            "model_loaded": self._model is not None,
            "scaler_loaded": self._scaler is not None,
            "load_error": self._load_error,
            "classes": list(self._classes),
            "feature_names": list(self._feature_names),
        }

    def predict(
        self,
        sensor_data: dict,
        frame_bytes: Optional[bytes],
        feature_vector: Optional[dict[str, float]] = None,
    ) -> dict:
        backend = (self.backend or "rule_based").lower().strip()
        if getattr(config, "MODEL_REQUIRE_TRAINED_BACKEND", False) and backend != "sklearn_pickle":
            return _unknown_payload("MODEL_ONLY_MODE")
        if backend == "sklearn_pickle":
            return self._predict_sklearn(
                sensor_data=sensor_data,
                frame_bytes=frame_bytes,
                feature_vector=feature_vector,
            )
        if backend != "rule_based":
            raise NotImplementedError(
                f"Backend '{backend}' is not implemented yet."
            )
        return _predict_rule_based(sensor_data=sensor_data, frame_bytes=frame_bytes)

    def _predict_sklearn(
        self,
        sensor_data: dict,
        frame_bytes: Optional[bytes],
        feature_vector: Optional[dict[str, float]],
    ) -> dict:
        if self._load_error:
            return _unknown_payload(self._load_error)
        if self._model is None or self._scaler is None:
            return _unknown_payload("MODEL_NOT_READY")

        sensor_stale = bool(sensor_data.get("sensor_stale"))
        missing_camera = frame_bytes is None
        missing_sensor_core = (
            (not bool(sensor_data.get("hr_valid")))
            or (sensor_data.get("gsr_conductance_us") is None)
        )
        if config.MODEL_UNKNOWN_ON_MISSING_DATA and (
            missing_camera or sensor_stale or missing_sensor_core
        ):
            reason_parts = []
            if missing_camera:
                reason_parts.append("NO_CAMERA")
            if sensor_stale:
                reason_parts.append("SENSOR_STALE")
            if missing_sensor_core:
                reason_parts.append("NO_SENSOR_CORE")
            return _unknown_payload("|".join(reason_parts) or "NO_SIGNAL")

        if not feature_vector:
            return _unknown_payload("NO_FEATURE_VECTOR")

        names = self._feature_names or list(feature_vector.keys())
        try:
            x = [[float(feature_vector.get(name, 0.0)) for name in names]]
            x_scaled = self._scaler.transform(x)
            if hasattr(self._model, "predict_proba"):
                proba = self._model.predict_proba(x_scaled)[0]
                classes = self._classes
                class_probs = {str(cls): float(proba[idx]) for idx, cls in enumerate(classes)}
                # Ensure the dashboard always gets all 4 target classes.
                normal = float(class_probs.get("normal", 0.0))
                anxiety = float(class_probs.get("anxiety", 0.0))
                stress = float(class_probs.get("stress", 0.0))
                depression = float(class_probs.get("depression", 0.0))
                probs = {
                    "normal": normal,
                    "anxiety": anxiety,
                    "stress": stress,
                    "depression": depression,
                }
                probs = _stabilize_class_probs(probs)
                label_top1 = max(probs, key=lambda k: probs[k])
                conf_top1 = float(probs[label_top1])
            else:
                pred = str(self._model.predict(x_scaled)[0])
                label_top1 = pred if pred in config.MODEL_CLASSES else "unknown"
                conf_top1 = None
                probs = {k: 0.0 for k in config.MODEL_CLASSES}

            model_alert = (
                label_top1 in config.MODEL_CLASSES
                and label_top1 != "normal"
                and conf_top1 is not None
                and conf_top1 >= config.MODEL_ALERT_CONFIDENCE_THRESHOLD
            )

            result = {
                "model_label_top1": label_top1,
                "model_confidence_top1": round(conf_top1, 6) if conf_top1 is not None else None,
                "model_probs_normal": round(float(probs["normal"]), 6),
                "model_probs_anxiety": round(float(probs["anxiety"]), 6),
                "model_probs_stress": round(float(probs["stress"]), 6),
                "model_probs_depression": round(float(probs["depression"]), 6),
                "model_alert_active": model_alert,
                "model_alert_reasons": (
                    f"CLASS={label_top1.upper()} CONF={round(conf_top1, 3)}"
                    if model_alert and conf_top1 is not None
                    else ""
                ),
            }
            return _apply_confidence_guard(result)
        except Exception as exc:
            return _unknown_payload(f"MODEL_PREDICT_ERROR:{exc}")


def _predict_rule_based(sensor_data: dict, frame_bytes: Optional[bytes]) -> dict:
    """Deterministic baseline mapping sensor state to 4 classes.

    This is a temporary baseline to keep the dashboard operational until the
    trained multimodal model is available. Camera presence is validated to
    enforce Unknown/No Signal behavior when data is incomplete.
    """
    sensor_stale = bool(sensor_data.get("sensor_stale"))
    hr = sensor_data.get("heart_rate_bpm")
    hr_valid = bool(sensor_data.get("hr_valid"))
    gsr = sensor_data.get("gsr_conductance_us")
    spo2 = sensor_data.get("spo2_percent")
    spo2_valid = bool(sensor_data.get("spo2_valid"))

    missing_camera = frame_bytes is None
    missing_sensor_core = (not hr_valid) or (gsr is None)
    if config.MODEL_UNKNOWN_ON_MISSING_DATA and (missing_camera or sensor_stale or missing_sensor_core):
        reason_parts = []
        if missing_camera:
            reason_parts.append("NO_CAMERA")
        if sensor_stale:
            reason_parts.append("SENSOR_STALE")
        if missing_sensor_core:
            reason_parts.append("NO_SENSOR_CORE")
        return _unknown_payload(reason="|".join(reason_parts) or "NO_SIGNAL")

    hr_f = float(hr) if hr is not None else 0.0
    gsr_f = float(gsr) if gsr is not None else 0.0
    spo2_f = float(spo2) if (spo2 is not None and spo2_valid) else 98.0

    # Convert domain signals into normalized feature scores.
    hr_stress = _clamp01((hr_f - 75.0) / 45.0)
    hr_low = _clamp01((60.0 - hr_f) / 30.0)
    gsr_stress = _clamp01((gsr_f - 6.0) / 18.0)
    spo2_penalty = _clamp01((95.0 - spo2_f) / 10.0)

    # Handcrafted logits for 4 classes.
    scores = {
        "normal": 1.2 - (0.7 * hr_stress + 0.8 * gsr_stress + 0.4 * spo2_penalty),
        "anxiety": 0.3 + (0.9 * hr_stress + 0.6 * gsr_stress) - (0.2 * hr_low),
        "stress": 0.2 + (0.7 * hr_stress + 1.1 * gsr_stress + 0.2 * spo2_penalty),
        "depression": 0.2 + (0.8 * hr_low) + (0.2 * (1.0 - gsr_stress)),
    }
    probs = _softmax(scores)
    probs = _stabilize_class_probs(probs)

    label_top1 = max(probs, key=lambda k: probs[k])
    conf_top1 = float(probs[label_top1])
    model_alert = label_top1 != "normal" and conf_top1 >= config.MODEL_ALERT_CONFIDENCE_THRESHOLD

    result = {
        "model_label_top1": label_top1,
        "model_confidence_top1": round(conf_top1, 6),
        "model_probs_normal": round(float(probs["normal"]), 6),
        "model_probs_anxiety": round(float(probs["anxiety"]), 6),
        "model_probs_stress": round(float(probs["stress"]), 6),
        "model_probs_depression": round(float(probs["depression"]), 6),
        "model_alert_active": model_alert,
        "model_alert_reasons": (
            f"CLASS={label_top1.upper()} CONF={round(conf_top1, 3)}"
            if model_alert
            else ""
        ),
    }
    return _apply_confidence_guard(result)
