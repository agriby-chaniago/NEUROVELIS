# Model Deployment Checklist

This document covers deployment for the realtime 4-class model dashboard.

## Target Scope

- Classes: `normal`, `anxiety`, `stress`, `depression`
- Inputs: camera stream + sensor stream
- Realtime endpoint: `/model`
- Debug endpoint: `/model/debug`
- Mesh stream endpoint: `/model/mesh_stream`
- Mesh topology endpoint: `/model/mesh_topology`

## Required Files

Place artifacts at:

- `model_inference/artifacts/model.pkl`
- `model_inference/artifacts/features.pkl`
- `model_inference/artifacts/face_landmarker.task`

## Required Dependencies

Install from `requirements.txt` on the target device:

- `scikit-learn`
- `joblib`
- `mediapipe` (for realtime Face Mesh visual features)
- plus existing runtime packages (Flask, numpy, camera/sensor libs)

## Runtime Config

`config.py` keys used by model inference:

- `MODEL_INFERENCE_ENABLED`
- `MODEL_INFERENCE_BACKEND`
- `MODEL_INFERENCE_MODEL_PATH`
- `MODEL_INFERENCE_SCALER_PATH`
- `MODEL_INFERENCE_FEATURES_PATH`
- `MODEL_INFERENCE_INTERVAL_S`
- `MODEL_INFERENCE_MIN_POINTS`
- `MODEL_ALERT_CONFIDENCE_THRESHOLD`
- `MODEL_UNKNOWN_CONFIDENCE_THRESHOLD`
- `MODEL_SMOOTHING_ALPHA`
- `MODEL_FACE_MESH_ENABLED`
- `MODEL_FACE_LANDMARKER_MODEL_PATH`
- `MODEL_FACE_MESH_MIN_DETECTION_CONFIDENCE`
- `MODEL_FACE_MESH_MIN_TRACKING_CONFIDENCE`
- `MODEL_BLINK_EAR_THRESHOLD`
- `MODEL_EDA_SCR_DIFF_THRESHOLD_US`
- `MODEL_MESH_STREAM_INTERVAL_S`

## Startup Validation

1. Start application normally.
2. Open `/model` and verify top class + confidence update.
3. Open `/model/debug` and confirm:
   - adapter backend loaded
   - feature vector present
   - visual extractor runtime enabled
   - no load error
4. Open `/health` and check `model` section is healthy.

## Troubleshooting

- `SKLEARN_DEPENDENCY_MISSING`: install dependencies on target.
- `MODEL_FILE_NOT_FOUND` or `SCALER_FILE_NOT_FOUND`: verify artifact paths.
- `NO_FEATURE_VECTOR`: wait until enough points are collected (`MODEL_INFERENCE_MIN_POINTS`).
- Frequent `NO_CAMERA` or `SENSOR_STALE`: verify hardware stream stability.
- Excessive label flicker: reduce `MODEL_SMOOTHING_ALPHA`.
- Face not detected continuously: improve lighting/camera angle or reduce
  Face Mesh confidence thresholds slightly.
- `visual.backend = disabled` in `/model/debug`:
  - Ensure service runtime has `mediapipe` and `opencv-python-headless` installed.
  - Ensure the `.task` file exists at `MODEL_FACE_LANDMARKER_MODEL_PATH` on the
    same machine/path used by the running service.
  - If using systemd, install dependencies in the exact Python environment from
    `ExecStart` in your service unit.

## Data Logging

Model outputs are logged in:

- global CSV (from `CSV_FIELDNAMES`)
- per-session `sensor_raw.csv` in each session directory

This enables post-session evaluation against experiment categories.
