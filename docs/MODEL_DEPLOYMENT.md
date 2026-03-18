# Model Deployment Checklist

This document covers deployment for the realtime 4-class model dashboard.

## Target Scope

- Classes: `normal`, `anxiety`, `stress`, `depression`
- Inputs: camera stream + sensor stream
- Realtime endpoint: `/model`
- Debug endpoint: `/model/debug`

## Required Files

Place artifacts at:

- `model_inference/artifacts/model.pkl`
- `model_inference/artifacts/scaler.pkl`

## Required Dependencies

Install from `requirements.txt` on the target device:

- `scikit-learn`
- `joblib`
- plus existing runtime packages (Flask, numpy, camera/sensor libs)

## Runtime Config

`config.py` keys used by model inference:

- `MODEL_INFERENCE_ENABLED`
- `MODEL_INFERENCE_BACKEND`
- `MODEL_INFERENCE_MODEL_PATH`
- `MODEL_INFERENCE_SCALER_PATH`
- `MODEL_INFERENCE_INTERVAL_S`
- `MODEL_INFERENCE_MIN_POINTS`
- `MODEL_ALERT_CONFIDENCE_THRESHOLD`
- `MODEL_UNKNOWN_CONFIDENCE_THRESHOLD`
- `MODEL_SMOOTHING_ALPHA`

## Startup Validation

1. Start application normally.
2. Open `/model` and verify top class + confidence update.
3. Open `/model/debug` and confirm:
   - adapter backend loaded
   - feature vector present
   - no load error
4. Open `/health` and check `model` section is healthy.

## Troubleshooting

- `SKLEARN_DEPENDENCY_MISSING`: install dependencies on target.
- `MODEL_FILE_NOT_FOUND` or `SCALER_FILE_NOT_FOUND`: verify artifact paths.
- `NO_FEATURE_VECTOR`: wait until enough points are collected (`MODEL_INFERENCE_MIN_POINTS`).
- Frequent `NO_CAMERA` or `SENSOR_STALE`: verify hardware stream stability.
- Excessive label flicker: reduce `MODEL_SMOOTHING_ALPHA`.

## Data Logging

Model outputs are logged in:

- global CSV (from `CSV_FIELDNAMES`)
- per-session `sensor_raw.csv` in each session directory

This enables post-session evaluation against experiment categories.
