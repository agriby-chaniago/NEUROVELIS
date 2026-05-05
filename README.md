
```
NEUROSENSE + Forward Psikiater
├─ camera_worker.py
├─ config.py
├─ dashboard
│  ├─ __init__.py
│  ├─ app.py
│  ├─ static
│  │  ├─ css
│  │  │  ├─ core
│  │  │  │  ├─ base.css
│  │  │  │  ├─ components.css
│  │  │  │  └─ theme.css
│  │  │  ├─ layouts
│  │  │  │  └─ fixed_dashboard.css
│  │  │  ├─ pages
│  │  │  │  ├─ dashboard.css
│  │  │  │  ├─ experiment.css
│  │  │  │  ├─ model.css
│  │  │  │  ├─ respondents.css
│  │  │  │  └─ sessions.css
│  │  │  └─ shared
│  │  │     └─ warmup_ui.css
│  │  └─ js
│  │     ├─ pages
│  │     │  ├─ dashboard.js
│  │     │  ├─ experiment.js
│  │     │  ├─ model.js
│  │     │  ├─ respondents.js
│  │     │  └─ sessions.js
│  │     └─ shared
│  │        ├─ chart_utils.js
│  │        ├─ sse_client.js
│  │        └─ warmup_ui.js
│  └─ templates
│     ├─ base.html
│     ├─ experiment.html
│     ├─ index.html
│     ├─ model.html
│     ├─ pages
│     │  ├─ dashboard.html
│     │  ├─ experiment.html
│     │  ├─ model.html
│     │  ├─ respondents.html
│     │  └─ sessions.html
│     ├─ respondents.html
│     └─ sessions.html
├─ data
│  └─ sessions
├─ data_logging
│  ├─ __init__.py
│  └─ csv_logger.py
├─ docs
│  ├─ FRONTEND_STRUCTURE.md
│  ├─ MODEL_DEPLOYMENT.md
│  └─ RASPI_AUTOSTART.md
├─ experiments
│  ├─ __init__.py
│  ├─ respondent_registry.py
│  └─ session_manager.py
├─ main.py
├─ model_inference
│  ├─ __init__.py
│  ├─ artifacts
│  │  ├─ face_landmarker.task
│  │  ├─ features.pkl
│  │  └─ model.pkl
│  ├─ model_adapter.py
│  ├─ model_inference_service.py
│  └─ visual_feature_extractor.py
├─ neurovelis.service
├─ packages.txt
├─ requirements.txt
├─ scripts
│  ├─ install_raspi_autoboot.sh
│  └─ open_model_kiosk.sh
├─ sensors
│  ├─ __init__.py
│  ├─ ads1115_reader.py
│  ├─ bme280_reader.py
│  ├─ bmp280_reader.py
│  ├─ buzzer.py
│  ├─ camera_reader.py
│  ├─ gsr_reader.py
│  ├─ hrcalc.py
│  ├─ max30102.py
│  ├─ max30102_reader.py
│  └─ sensor_manager.py
└─ tests
   ├─ __init__.py
   ├─ test_model_inference.py
   └─ test_sensors.py

```