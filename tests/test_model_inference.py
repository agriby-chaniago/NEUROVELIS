"""Tests for model inference integration (adapter + Flask routes)."""

from model_inference.model_adapter import (
    _compute_independent_chances_from_scores,
)
from model_inference.model_inference_service import ModelInferenceService
from model_inference.warmup_gate import _WarmupGate
from model_inference.stability_tracker import _StabilityTracker
from dashboard.app import create_app
import config


class _DummySensorManager:
    def get_latest(self):
        return {
            "timestamp_utc": "2026-03-18T00:00:00+00:00",
            "sensor_stale": False,
        }

    def health(self):
        return [{"sensor": "dummy", "ok": True}]


class _DummyModelService:
    def get_latest(self):
        return {
            "model_label_top1": "stress",
            "model_confidence_top1": 0.9,
            "model_probs_normal": 0.02,
            "model_probs_anxiety": 0.08,
            "model_probs_stress": 0.9,
            "model_probs_depression": 0.0,
            "model_alert_active": True,
            "model_alert_reasons": "CLASS=STRESS CONF=0.9",
            "model_latency_ms": 50,
            "model_timestamp_utc": "2026-03-18T00:00:00+00:00",
        }

    def health(self):
        return {
            "sensor": "model_inference",
            "ok": True,
            "backend": "rule_based",
            "model_loaded": True,
            "scaler_loaded": True,
            "last_error": None,
            "last_label": "stress",
            "last_confidence": 0.9,
            "last_latency_ms": 50,
        }

    def debug_snapshot(self):
        return {
            "latest": self.get_latest(),
            "feature_vector": {"hr_mean": 80.0, "gsr_mean": 10.0},
            "feature_timestamp_utc": "2026-03-18T00:00:00+00:00",
            "history_points": 10,
            "adapter": {"backend": "rule_based", "load_error": None},
        }


def test_independent_chance_not_normalized(monkeypatch):
    import config

    monkeypatch.setattr(config, "MODEL_CHANCE_LOGIT_TEMPERATURE", 1.0)
    monkeypatch.setattr(config, "MODEL_CHANCE_LOGIT_BIAS", 0.0)
    monkeypatch.setattr(config, "MODEL_CHANCE_SHRINKAGE", 1.0)
    monkeypatch.setattr(config, "MODEL_CHANCE_MIN", 0.0)
    monkeypatch.setattr(config, "MODEL_CHANCE_MAX", 1.0)

    scores = {
        "normal": 0.8,
        "anxiety": 0.2,
        "stress": 2.0,
        "depression": -0.3,
    }
    out = _compute_independent_chances_from_scores(scores)
    assert 0.0 <= out["normal"] <= 1.0
    assert 0.0 <= out["anxiety"] <= 1.0
    assert 0.0 <= out["stress"] <= 1.0
    assert 0.0 <= out["depression"] <= 1.0
    assert out["stress"] > out["normal"] > out["anxiety"] > out["depression"]
    assert sum(out.values()) > 1.0


def test_independent_chance_extreme_is_compressed(monkeypatch):
    import config

    monkeypatch.setattr(config, "MODEL_CHANCE_LOGIT_TEMPERATURE", 1.0)
    monkeypatch.setattr(config, "MODEL_CHANCE_LOGIT_BIAS", 0.0)
    monkeypatch.setattr(config, "MODEL_CHANCE_SHRINKAGE", 0.70)
    monkeypatch.setattr(config, "MODEL_CHANCE_MIN", 0.03)
    monkeypatch.setattr(config, "MODEL_CHANCE_MAX", 0.92)

    scores = {
        "normal": -10.0,
        "anxiety": -3.0,
        "stress": 20.0,
        "depression": 8.0,
    }
    out = _compute_independent_chances_from_scores(scores)
    assert out["stress"] <= 0.92
    assert out["depression"] <= 0.92
    assert out["normal"] >= 0.03


def test_model_routes_exposed():
    app = create_app(
        _DummySensorManager(),
        camera_reader=None,
        session_manager=None,
        respondent_registry=None,
        model_inference_service=_DummyModelService(),
    )
    client = app.test_client()

    resp_snapshot = client.get("/model/snapshot")
    assert resp_snapshot.status_code == 200
    payload = resp_snapshot.get_json()
    assert payload["model_label_top1"] == "stress"
    assert "model_probs_stress" in payload

    resp_debug = client.get("/model/debug")
    assert resp_debug.status_code == 200
    dbg = resp_debug.get_json()
    assert "feature_vector" in dbg
    assert dbg["adapter"]["backend"] == "rule_based"

    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    health = resp_health.get_json()
    assert "model" in health


def _make_touch_gate() -> _WarmupGate:
    gate = _WarmupGate()
    gate._sensor_touch_paused = True
    return gate


def _make_stability_tracker() -> _StabilityTracker:
    return _StabilityTracker()


def _running_result(label: str, confidence: float = 0.82) -> dict:
    return {
        "model_label_top1": label,
        "model_confidence_top1": confidence,
        "model_probs_normal": 0.05,
        "model_probs_anxiety": 0.12 if label == "anxiety" else 0.08,
        "model_probs_stress": 0.82 if label == "stress" else 0.12,
        "model_probs_depression": 0.12 if label == "depression" else 0.05,
        "model_chance_normal": 0.12,
        "model_chance_anxiety": 0.2,
        "model_chance_stress": 0.84,
        "model_chance_depression": 0.13,
        "model_runtime_state": "RUNNING",
    }


def _valid_sensor_snapshot() -> dict:
    return {
        "sensor_stale": False,
        "heart_rate_bpm": 84,
        "hr_valid": True,
        "spo2_percent": 97,
        "spo2_valid": True,
        "gsr_conductance_us": 8.3,
        "temperature_celsius": 36.4,
    }


def test_sensor_touch_heuristic_requires_valid_hr_spo2_and_gsr():
    assert not _WarmupGate._is_sensor_touched(
        {
            "heart_rate_bpm": 82,
            "hr_valid": True,
            "spo2_percent": None,
            "spo2_valid": False,
            "gsr_conductance_us": 6.0,
        }
    )

    assert not _WarmupGate._is_sensor_touched(
        {
            "heart_rate_bpm": 170,
            "hr_valid": True,
            "spo2_percent": 97,
            "spo2_valid": True,
            "gsr_conductance_us": 6.0,
        }
    )

    assert _WarmupGate._is_sensor_touched(
        {
            "heart_rate_bpm": 82,
            "hr_valid": True,
            "spo2_percent": 97,
            "spo2_valid": True,
            "gsr_conductance_us": 6.0,
        }
    )


def test_touch_debounce_requires_consecutive_hits_before_warmup(monkeypatch):
    monkeypatch.setattr(config, "MODEL_SENSOR_TOUCH_ON_HITS", 2)
    monkeypatch.setattr(config, "MODEL_SENSOR_TOUCH_OFF_HITS", 2)
    monkeypatch.setattr(config, "MODEL_SENSOR_TOUCH_GRACE_S", 2.0)
    monkeypatch.setattr(config, "MODEL_CLASS_WARMUP_S", 4.0)
    monkeypatch.setattr(config, "MODEL_CLASS_WARMUP_MIN_S", 4.0)
    monkeypatch.setattr(config, "MODEL_CLASS_WARMUP_MAX_S", 4.0)

    gate = _make_touch_gate()
    touch_payload = {
        "heart_rate_bpm": 81,
        "hr_valid": True,
        "spo2_percent": 97,
        "spo2_valid": True,
        "gsr_conductance_us": 6.0,
    }

    # First hit should not start warmup yet.
    gate.update(touch_payload, now=10.0)
    assert gate._sensor_touch_prev is False
    assert gate._class_warmup_until == 0.0

    # Second consecutive hit confirms touch and starts warmup.
    gate.update(touch_payload, now=11.0)
    assert gate._sensor_touch_prev is True
    assert gate._sensor_touch_paused is False
    assert gate._class_warmup_until == 15.0


def test_stability_window_resets_on_label_runtime_and_sensor(monkeypatch):
    monkeypatch.setattr(config, "MODEL_STABLE_RUN_SECONDS", 30.0)
    monkeypatch.setattr(config, "MODEL_STABLE_VARIANCE_ENABLED", False)

    tracker = _make_stability_tracker()
    sensor = _valid_sensor_snapshot()

    first = tracker.update(_running_result("stress"), sensor, now=10.0)
    assert first["model_stable_elapsed_s"] == 0.0
    assert first["model_stable_ready"] is False

    progressed = tracker.update(_running_result("stress"), sensor, now=20.0)
    assert progressed["model_stable_elapsed_s"] > 0.0

    changed_label = tracker.update(_running_result("anxiety"), sensor, now=21.0)
    assert changed_label["model_stable_elapsed_s"] == 0.0
    assert changed_label["model_stable_ready"] is False

    warmup_state = _running_result("anxiety")
    warmup_state["model_runtime_state"] = "WARMUP"
    reset_runtime = tracker.update(warmup_state, sensor, now=22.0)
    assert reset_runtime["model_stable_reset_reason"] == "RUNTIME_NOT_RUNNING"
    assert reset_runtime["model_stable_elapsed_s"] == 0.0

    invalid_sensor = dict(sensor)
    invalid_sensor["hr_valid"] = False
    reset_sensor = tracker.update(_running_result("stress"), invalid_sensor, now=23.0)
    assert reset_sensor["model_stable_reset_reason"] == "SENSOR_MISSING_OR_INVALID"
    assert reset_sensor["model_stable_elapsed_s"] == 0.0


