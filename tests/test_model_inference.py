"""Tests for model inference integration (adapter + Flask routes)."""

from model_inference.model_adapter import (
    _compute_independent_chances_from_scores,
)
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
