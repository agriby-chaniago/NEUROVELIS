"""Realtime visual feature extraction using MediaPipe Face Landmark model.

This module decodes JPEG frames and extracts facial dynamics features used by
model inference:
- EAR (eye aspect ratio)
- MAR (mouth aspect ratio)
- landmark motion
- blink events

It is dependency-tolerant: if mediapipe or model assets are unavailable,
extraction returns empty payloads and the inference service can decide fallback
behavior.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency handling
    cv2 = None

try:
    import mediapipe as mp  # type: ignore
except Exception:  # pragma: no cover - optional runtime dependency handling
    mp = None

if mp is not None:
    try:
        from mediapipe.tasks import python as mp_python  # type: ignore
        from mediapipe.tasks.python import vision as mp_vision  # type: ignore
    except Exception:  # pragma: no cover - optional runtime dependency handling
        mp_python = None
        mp_vision = None
else:
    mp_python = None
    mp_vision = None

logger = logging.getLogger(__name__)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class VisualFeatureExtractor:
    """Extract visual features from camera frames using MediaPipe facial landmarks."""

    LEFT_EYE = (362, 385, 387, 263, 373, 380)
    RIGHT_EYE = (33, 160, 158, 133, 153, 144)
    MOUTH = {
        "left": 61,
        "right": 291,
        "up1": 13,
        "low1": 14,
        "up2": 81,
        "low2": 178,
        "up3": 311,
        "low3": 402,
    }
    MOTION_POINTS = (1, 10, 152, 234, 454)

    def __init__(
        self,
        enabled: bool = True,
        landmarker_model_path: Optional[str] = None,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        blink_ear_threshold: float = 0.21,
    ):
        self._enabled_cfg = bool(enabled)
        self._enabled_runtime = bool(enabled and cv2 is not None and mp is not None)
        self._blink_ear_threshold = float(blink_ear_threshold)
        self._landmarker_model_path = str(landmarker_model_path or "").strip()
        self._eyes_closed = False
        self._prev_motion_points: Optional[list[tuple[float, float]]] = None
        self._last_error: Optional[str] = None
        self._backend = "disabled"

        self._landmarker = None
        self._face_mesh = None
        if not self._enabled_runtime:
            return

        if self._init_face_landmarker_model(
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        ):
            return

        if mp is not None:
            try:
                self._face_mesh = mp.solutions.face_mesh.FaceMesh(
                    static_image_mode=False,
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=float(min_detection_confidence),
                    min_tracking_confidence=float(min_tracking_confidence),
                )
                self._backend = "mediapipe_facemesh_legacy"
            except Exception as exc:
                self._enabled_runtime = False
                self._last_error = f"FACEMESH_INIT_ERROR:{exc}"
                logger.error("VisualFeatureExtractor init failed: %s", exc)

    def _init_face_landmarker_model(
        self,
        min_detection_confidence: float,
        min_tracking_confidence: float,
    ) -> bool:
        if mp_python is None or mp_vision is None:
            return False

        model_path = self._landmarker_model_path
        if not model_path:
            return False

        model_file = Path(model_path)
        if not model_file.exists():
            self._last_error = f"FACELANDMARKER_MODEL_NOT_FOUND:{model_file}"
            logger.warning(
                "VisualFeatureExtractor: face landmark model not found at %s; "
                "falling back to legacy FaceMesh",
                model_file,
            )
            return False

        try:
            base_options = mp_python.BaseOptions(model_asset_path=str(model_file))
            options = mp_vision.FaceLandmarkerOptions(
                base_options=base_options,
                running_mode=mp_vision.RunningMode.IMAGE,
                num_faces=1,
                min_face_detection_confidence=float(min_detection_confidence),
                min_face_presence_confidence=float(min_tracking_confidence),
                min_tracking_confidence=float(min_tracking_confidence),
                output_face_blendshapes=False,
                output_facial_transformation_matrixes=False,
            )
            self._landmarker = mp_vision.FaceLandmarker.create_from_options(options)
            self._backend = "mediapipe_face_landmarker_model"
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = f"FACELANDMARKER_INIT_ERROR:{exc}"
            logger.warning(
                "VisualFeatureExtractor: FaceLandmarker init failed (%s); "
                "falling back to legacy FaceMesh",
                exc,
            )
            self._landmarker = None
            return False

    def close(self):
        if self._landmarker is not None:
            try:
                self._landmarker.close()
            except Exception:
                pass
        if self._face_mesh is not None:
            try:
                self._face_mesh.close()
            except Exception:
                pass

    def health(self) -> dict:
        return {
            "enabled_config": self._enabled_cfg,
            "enabled_runtime": self._enabled_runtime,
            "backend": self._backend,
            "last_error": self._last_error,
            "model_path": self._landmarker_model_path or None,
        }

    def extract(self, frame_bytes: Optional[bytes]) -> dict[str, Optional[float]]:
        """Extract face dynamics features from a JPEG frame."""
        empty = {
            "face_detected": False,
            "ear": None,
            "mar": None,
            "motion": None,
            "blink_event": 0.0,
        }

        if (
            not self._enabled_runtime
            or frame_bytes is None
            or cv2 is None
            or (self._landmarker is None and self._face_mesh is None)
        ):
            return empty

        try:
            _cv2 = cv2
            buffer = np.frombuffer(frame_bytes, dtype=np.uint8)
            if buffer.size == 0:
                return empty
            frame_bgr = _cv2.imdecode(buffer, _cv2.IMREAD_COLOR)
            if frame_bgr is None:
                return empty

            frame_rgb = _cv2.cvtColor(frame_bgr, _cv2.COLOR_BGR2RGB)
            height, width = frame_bgr.shape[:2]

            if self._landmarker is not None and mp is not None:
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)
                result = self._landmarker.detect(mp_image)
                if result.face_landmarks:
                    return self._features_from_landmarks(
                        landmarks=result.face_landmarks[0],
                        width=width,
                        height=height,
                    )

            if self._face_mesh is not None:
                results = self._face_mesh.process(frame_rgb)
                if results.multi_face_landmarks:
                    return self._features_from_landmarks(
                        landmarks=results.multi_face_landmarks[0].landmark,
                        width=width,
                        height=height,
                    )

            self._prev_motion_points = None
            self._eyes_closed = False
            return empty
        except Exception as exc:
            self._last_error = f"FACEMESH_EXTRACT_ERROR:{exc}"
            logger.debug("VisualFeatureExtractor extract error: %s", exc)
            return empty

    def _features_from_landmarks(self, landmarks, width: int, height: int) -> dict[str, float | bool]:
        def point(idx: int) -> tuple[float, float]:
            p = landmarks[idx]
            return (float(p.x * width), float(p.y * height))

        ear_l = self._eye_aspect_ratio(point, self.LEFT_EYE)
        ear_r = self._eye_aspect_ratio(point, self.RIGHT_EYE)
        ear = (ear_l + ear_r) / 2.0
        mar = self._mouth_aspect_ratio(point)
        motion = self._landmark_motion(point)
        blink_event = self._blink_event(ear)

        return {
            "face_detected": True,
            "ear": float(ear),
            "mar": float(mar),
            "motion": float(motion),
            "blink_event": float(blink_event),
        }

    def _eye_aspect_ratio(self, point_fn, idx: tuple[int, int, int, int, int, int]) -> float:
        p1, p2, p3, p4, p5, p6 = [point_fn(i) for i in idx]
        den = 2.0 * _dist(p1, p4)
        if den <= 1e-6:
            return 0.0
        return (_dist(p2, p6) + _dist(p3, p5)) / den

    def _mouth_aspect_ratio(self, point_fn) -> float:
        left = point_fn(self.MOUTH["left"])
        right = point_fn(self.MOUTH["right"])
        up1 = point_fn(self.MOUTH["up1"])
        low1 = point_fn(self.MOUTH["low1"])
        up2 = point_fn(self.MOUTH["up2"])
        low2 = point_fn(self.MOUTH["low2"])
        up3 = point_fn(self.MOUTH["up3"])
        low3 = point_fn(self.MOUTH["low3"])

        mouth_width = _dist(left, right)
        if mouth_width <= 1e-6:
            return 0.0

        v1 = _dist(up1, low1)
        v2 = _dist(up2, low2)
        v3 = _dist(up3, low3)
        return (v1 + v2 + v3) / (3.0 * mouth_width)

    def _landmark_motion(self, point_fn) -> float:
        current = [point_fn(i) for i in self.MOTION_POINTS]
        left_cheek = point_fn(234)
        right_cheek = point_fn(454)
        face_width = max(_dist(left_cheek, right_cheek), 1e-6)

        if self._prev_motion_points is None:
            self._prev_motion_points = current
            return 0.0

        displacement = [
            _dist(curr, prev)
            for curr, prev in zip(current, self._prev_motion_points)
        ]
        self._prev_motion_points = current
        return (sum(displacement) / len(displacement)) / face_width

    def _blink_event(self, ear: float) -> float:
        event = 0.0
        threshold = self._blink_ear_threshold

        if not self._eyes_closed and ear < threshold:
            self._eyes_closed = True
            event = 1.0
        elif self._eyes_closed and ear > (threshold + 0.02):
            self._eyes_closed = False

        return event
