"""
scan_engine/scan_state_machine.py

Face-triggered scan state machine. Runs as a background daemon thread.
Polls ModelInferenceService (read-only) for face + inference data.
Writes frozen scan results to data/scans/ via ResultFreezer.

State flow:
  IDLE → DETECTING → WARMUP → STABILIZING → RESULT_READY → QR_DISPLAY → COOLDOWN → IDLE
"""

import time
import threading

import config
from scan_engine.result_freezer import ResultFreezer

_HINTS = {
    "IDLE":         "",
    "DETECTING":    "Please stay in front of the camera",
    "WARMUP":       "Preparing...",
    "STABILIZING":  "Hold still...",
    "RESULT_READY": "Processing result...",
    "QR_DISPLAY":   "Scan QR with your phone",
    "COOLDOWN":     "Please step away to start a new scan",  # FIX 17
}

_DURATIONS = {
    "DETECTING":   "SCAN_DETECTING_DURATION",
    "WARMUP":      "SCAN_WARMUP_DURATION",
    "STABILIZING": "SCAN_STABILIZING_DURATION",
    "QR_DISPLAY":  "QR_DISPLAY_DURATION",
    "COOLDOWN":    "SCAN_COOLDOWN_DURATION",
}


class ScanStateMachine:
    def __init__(self, model_inference_service, sensor_manager=None):
        self._mis            = model_inference_service
        self._sensor_manager = sensor_manager
        self._freezer        = ResultFreezer()
        self._lock           = threading.Lock()

        # state
        self._state        = "IDLE"
        self._state_entered = time.monotonic()

        # face tracking
        self._prev_bbox          = None   # FIX 1: bbox-based stability
        self._locked_bbox        = None   # locked at STABILIZING entry
        self._face_absent_since  = None
        self._inconsistent_count = 0      # FIX 12: jitter counter

        # frozen result — local copy used by get_state (FIX 3)
        self._frozen_result = None

        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScanStateMachine")

    # ── public API ────────────────────────────────────────────────────────────

    def start(self):
        self._freezer.cleanup_old_scans()   # FIX 5: cleanup on startup
        self._thread.start()

    def get_state(self) -> dict:
        with self._lock:                    # FIX 3: lock for all shared reads
            state  = self._state
            entered = self._state_entered
            frozen = self._frozen_result    # local copy — safe to read outside lock

        elapsed   = time.monotonic() - entered
        cfg_key   = _DURATIONS.get(state)
        duration  = getattr(config, cfg_key, 0) if cfg_key else 0
        remaining = max(0.0, duration - elapsed) if duration else 0.0

        return {
            "scan_state":        state,
            "scan_elapsed_s":    round(elapsed, 1),
            "scan_remaining_s":  round(remaining, 1),
            "scan_hint_message": _HINTS.get(state, ""),   # FIX 10, 17
            "scan_id":           frozen["scan_id"] if frozen else None,
            "scan_qr_url":       frozen.get("qr_url") if frozen else None,
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _transition(self, new_state: str):
        self._state        = new_state
        self._state_entered = time.monotonic()

    def _elapsed(self) -> float:
        return time.monotonic() - self._state_entered

    def _reset_to_idle(self):
        """FIX 13/18: full clean reset — no stale data leaks into next scan."""
        self._transition("IDLE")
        self._frozen_result      = None   # clear stale result
        self._prev_bbox          = None   # clear bbox history
        self._locked_bbox        = None
        self._inconsistent_count = 0      # FIX 12: clear jitter counter
        self._face_absent_since  = None

    @staticmethod
    def _bbox_from_landmarks(landmarks):
        """Return (cx, cy, w) from normalized 468-landmark list, or None."""
        if not landmarks:
            return None
        xs = [lm[0] for lm in landmarks]
        ys = [lm[1] for lm in landmarks]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        return ((xmin + xmax) / 2, (ymin + ymax) / 2, xmax - xmin)

    def _face_stable(self, bbox) -> bool:
        """FIX 1+11: stability via bbox center delta, first frame seeds prev and returns False."""
        if bbox is None:
            return False
        if self._prev_bbox is None:
            self._prev_bbox = bbox          # FIX 11: seed prev, NOT stable yet
            return False
        cx0, cy0, w0 = self._prev_bbox
        cx1, cy1, _  = bbox
        if w0 == 0:
            return True
        movement = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
        return movement < config.FACE_STABLE_THRESHOLD

    def _face_consistent(self, bbox) -> bool:
        """Bbox must not drift from locked position (guard during STABILIZING)."""
        if self._locked_bbox is None or bbox is None:
            return True
        cx0, cy0, w0 = self._locked_bbox
        cx1, cy1, _  = bbox
        if w0 == 0:
            return True
        drift = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
        return drift < config.FACE_CONSISTENCY_THRESHOLD

    # ── main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(0.1)

    def _tick(self):
        latest    = self._mis.get_latest()
        mesh      = self._mis.get_mesh_latest()
        face      = latest.get("model_face_detected", False)
        landmarks = (mesh or {}).get("model_face_landmarks", [])

        # FIX 8: safe reads — no None/NaN propagation
        confidence = float(latest.get("model_confidence_top1") or 0.0)
        label      = latest.get("model_label_top1") or ""

        # Merge sensor data for freeze (HR, GSR live in sensor_manager, not MIS)
        sensor_snapshot = {}
        if self._sensor_manager is not None:
            try:
                sensor_snapshot = self._sensor_manager.get_latest()
            except Exception:
                pass
        merged_latest = {**sensor_snapshot, **latest}   # model keys take priority

        bbox = self._bbox_from_landmarks(landmarks)

        with self._lock:
            state = self._state

            if state == "IDLE":
                if face:
                    self._transition("DETECTING")
                    self._face_absent_since = None
                    self._prev_bbox = bbox

            elif state == "DETECTING":
                if not face:
                    self._reset_to_idle()           # FIX 13/18
                else:
                    self._prev_bbox = bbox
                    if self._elapsed() >= config.SCAN_DETECTING_DURATION:
                        self._transition("WARMUP")

            elif state == "WARMUP":
                if not face:
                    self._reset_to_idle()           # FIX 13/18
                else:
                    self._prev_bbox = bbox
                    if self._elapsed() >= config.SCAN_WARMUP_DURATION:
                        self._locked_bbox = bbox
                        self._transition("STABILIZING")

            elif state == "STABILIZING":            # FIX 4: no premature return
                if not face:
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since > 2.0:
                        self._reset_to_idle()       # FIX 13/18
                else:
                    self._face_absent_since = None
                    stable     = self._face_stable(bbox)
                    consistent = self._face_consistent(bbox)
                    self._prev_bbox = bbox          # FIX 1/11: always update

                    if not consistent:
                        self._inconsistent_count += 1   # FIX 12: count, not instant cancel
                        if self._inconsistent_count > 3:
                            self._reset_to_idle()       # FIX 13/18
                    else:
                        self._inconsistent_count = 0    # FIX 12: reset on stable frame
                        if (
                            self._elapsed() >= config.SCAN_STABILIZING_DURATION
                            and stable
                            and confidence >= config.CONFIDENCE_THRESHOLD
                            and label not in (None, "", "unknown")   # FIX 9
                        ):
                            frozen = self._freezer.freeze(merged_latest, label, confidence)
                            self._frozen_result = frozen
                            self._transition("RESULT_READY")

            elif state == "RESULT_READY":
                self._transition("QR_DISPLAY")

            elif state == "QR_DISPLAY":
                if not face:
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since > 3.0:
                        self._transition("COOLDOWN")
                        self._face_absent_since = None
                else:
                    self._face_absent_since = None

                if self._elapsed() >= config.QR_DISPLAY_DURATION:
                    self._transition("COOLDOWN")

            elif state == "COOLDOWN":
                if not face:
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since >= config.SCAN_COOLDOWN_DURATION:
                        self._reset_to_idle()       # FIX 13/18
                else:
                    self._face_absent_since = None
