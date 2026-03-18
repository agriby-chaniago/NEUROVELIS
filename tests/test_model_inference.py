"""Tests for model inference integration (adapter + Flask routes)."""

from model_inference.model_adapter import _apply_confidence_guard
from dashboard.app import create_app


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


def test_confidence_guard_forces_unknown(monkeypatch):
    import config

    monkeypatch.setattr(config, "MODEL_UNKNOWN_CONFIDENCE_THRESHOLD", 0.5)

    result = {
        "model_label_top1": "stress",
        "model_confidence_top1": 0.41,
        "model_probs_normal": 0.1,
        "model_probs_anxiety": 0.2,
        "model_probs_stress": 0.41,
        "model_probs_depression": 0.29,
        "model_alert_active": True,
        "model_alert_reasons": "CLASS=STRESS",
    }
    out = _apply_confidence_guard(result)
    assert out["model_label_top1"] == "unknown"
    assert out["model_alert_active"] is False
    assert "LOW_CONF" in out["model_alert_reasons"]


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
