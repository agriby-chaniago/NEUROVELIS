"""EMA probability smoother — reduces UI flicker on class probability outputs."""
from __future__ import annotations

from typing import Optional

import config


class _Smoother:
    """Exponential moving average over class probabilities.

    Stateful per-subject: call reset() when sensor touch pauses or a new
    subject session begins so stale EMA doesn't bleed into the next read.
    """

    def __init__(self) -> None:
        self._probs: Optional[dict[str, float]] = None

    def reset(self) -> None:
        self._probs = None

    def apply(self, result: dict) -> dict:
        exclude_normal = bool(getattr(config, "MODEL_EXCLUDE_NORMAL_CLASS", False))
        active_classes = (
            ["anxiety", "stress", "depression"]
            if exclude_normal
            else ["normal", "anxiety", "stress", "depression"]
        )
        if str(result.get("model_label_top1") or "").lower() not in active_classes:
            self._probs = None
            return result

        probs = {
            "normal": 0.0 if exclude_normal else float(result.get("model_probs_normal", 0.0) or 0.0),
            "anxiety": float(result.get("model_probs_anxiety", 0.0) or 0.0),
            "stress": float(result.get("model_probs_stress", 0.0) or 0.0),
            "depression": float(result.get("model_probs_depression", 0.0) or 0.0),
        }
        total = sum(probs[k] for k in active_classes)
        if total <= 0.0:
            self._probs = None
            return result

        alpha = max(0.01, min(1.0, float(config.MODEL_SMOOTHING_ALPHA)))
        if self._probs is None:
            self._probs = dict(probs)
        else:
            for k in self._probs:
                self._probs[k] = alpha * probs[k] + (1.0 - alpha) * self._probs[k]

        if exclude_normal:
            self._probs["normal"] = 0.0

        smooth_total = sum(self._probs[k] for k in active_classes) or 1.0
        normalized = {
            "normal": 0.0,
            "anxiety": self._probs["anxiety"] / smooth_total,
            "stress": self._probs["stress"] / smooth_total,
            "depression": self._probs["depression"] / smooth_total,
        }
        if not exclude_normal:
            normalized["normal"] = self._probs["normal"] / smooth_total

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
