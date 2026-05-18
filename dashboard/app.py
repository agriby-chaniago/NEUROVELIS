"""
dashboard/app.py  –  Flask web dashboard with Server-Sent Events (SSE).

Routes
------
GET  /                              → dashboard HTML page
GET  /stream                        → SSE stream
GET  /health                        → JSON health check
GET  /camera/stream                 → MJPEG live video
GET  /camera/snapshot               → high-res JPEG

Scan routes
-----------
GET  /scan/qr_image/<scan_id>       → QR PNG for scan report URL
GET  /report/<scan_id>              → user-facing report form (phone)
POST /report/<scan_id>/generate     → generate + stream PDF download

Experiment routes
-----------------
GET  /experiment                    → session control panel
GET  /experiment/respondents        → respondent management
POST /experiment/respondents/add    → register new respondent
DEL  /experiment/respondents/<id>   → delete respondent
GET  /experiment/sessions           → all sessions list
GET  /experiment/session/<id>       → session detail JSON
GET  /experiment/session/active     → active session JSON
POST /experiment/session/start      → start recording session
POST /experiment/session/stop       → stop recording session
"""

import io
import json
import logging
import shutil
import time
from types import SimpleNamespace
from typing import Optional

from flask import (
    Flask, Response, jsonify, redirect, render_template,
    request, send_file, stream_with_context, url_for,
)

import config

logger = logging.getLogger(__name__)


def _safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default

# Injected by main.py after startup
_sensor_manager    = None
_camera_reader:    Optional[object] = None
_session_manager:  Optional[object] = None
_respondent_registry: Optional[object] = None
_model_inference_service: Optional[object] = None
_scan_state_machine: Optional[object] = None


def create_app(
    sensor_manager,
    camera_reader=None,
    session_manager=None,
    respondent_registry=None,
    model_inference_service=None,
    scan_state_machine=None,
) -> Flask:
    """
    Factory function — creates and configures the Flask app.

    Parameters
    ----------
    sensor_manager      : SensorManager         (required)
    camera_reader       : CameraReader          (optional)
    session_manager     : SessionManager        (optional)
    respondent_registry : RespondentRegistry    (optional)
    model_inference_service : ModelInferenceService (optional)
    scan_state_machine  : ScanStateMachine      (optional)
    """
    global _sensor_manager, _camera_reader, _session_manager, _respondent_registry
    global _model_inference_service, _scan_state_machine
    _sensor_manager          = sensor_manager
    _camera_reader           = camera_reader
    _session_manager         = session_manager
    _respondent_registry     = respondent_registry
    _model_inference_service = model_inference_service
    _scan_state_machine      = scan_state_machine

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = "neurovelis-dev-key"

    # ── Routes ────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("pages/dashboard.html")

    @app.route("/stream")
    def stream():
        """
        SSE endpoint.  The browser opens one persistent connection here and
        receives JSON data frames every DASHBOARD_SSE_INTERVAL_S seconds.
        """
        def event_generator():
            # Per-connection seq: captured at connection open so each client
            # tracks its own position — no shared state, no cross-tab interference.
            last_seq = _model_inference_service._result_seq if _model_inference_service is not None else 0
            while True:
                if _model_inference_service is not None:
                    with _model_inference_service._result_cond:
                        _model_inference_service._result_cond.wait_for(
                            lambda: _model_inference_service._result_seq != last_seq,
                            timeout=config.DASHBOARD_SSE_INTERVAL_S,
                        )
                        last_seq = _model_inference_service._result_seq
                else:
                    time.sleep(config.DASHBOARD_SSE_INTERVAL_S)
                data = _sensor_manager.get_latest()
                if _model_inference_service is not None:
                    if hasattr(_model_inference_service, "get_latest_compact"):
                        data.update(_model_inference_service.get_latest_compact())
                    else:
                        data.update(_model_inference_service.get_latest())
                # Attach live camera FPS so the dashboard can display it
                if _camera_reader is not None:
                    _fps = _camera_reader.fps
                    data["camera_fps"] = round(_fps, 1) if _fps is not None else None
                # Scan state machine fields
                if _scan_state_machine is not None:
                    data.update(_scan_state_machine.get_state())
                # Disk free space on the data partition
                try:
                    _du = shutil.disk_usage(config.DATA_DIR)
                    data["disk_free_gb"] = round(_du.free / 1_073_741_824, 2)
                except Exception:
                    data["disk_free_gb"] = None
                # Convert None to JSON null cleanly
                payload = json.dumps(data, default=lambda x: None)
                yield f"data: {payload}\n\n"

        return Response(
            stream_with_context(event_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control":  "no-cache",
                "X-Accel-Buffering": "no",   # disable nginx buffering if proxied
            },
        )

    @app.route("/model/mesh_stream")
    def model_mesh_stream():
        """SSE stream dedicated to face mesh overlay payload."""
        if _model_inference_service is None:
            return Response("Model service not available", status=503)

        interval = max(
            0.05,
            float(getattr(config, "MODEL_MESH_STREAM_INTERVAL_S", 0.15)),
        )

        def event_generator():
            while True:
                data = _model_inference_service.get_mesh_latest()
                payload = json.dumps(data, default=lambda x: None)
                yield f"data: {payload}\n\n"
                time.sleep(interval)

        return Response(
            stream_with_context(event_generator()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.route("/model/mesh_topology")
    def model_mesh_topology():
        """Return MediaPipe FaceMesh tesselation edges for wireframe drawing."""
        if _model_inference_service is None:
            return jsonify({"edges": []})
        return jsonify({"edges": _model_inference_service.get_mesh_topology()})

    @app.route("/health")
    def health():
        """JSON health check — shows per-sensor status."""
        sensors = _sensor_manager.health()
        if _camera_reader is not None:
            sensors.append(_camera_reader.health())
        model = None
        if _model_inference_service is not None:
            model = _model_inference_service.health()
        return jsonify({"status": "ok", "sensors": sensors, "model": model})

    @app.route("/camera/stream")
    def camera_stream():
        """
        MJPEG multipart stream — point an <img> src here for live video.
        Falls back to a 503 if the camera is not enabled/available.
        """
        if _camera_reader is None:
            return Response("Camera not enabled", status=503)

        def generate():
            while True:
                # Blocks until capture thread signals a new frame (zero delay).
                # Timeout 0.5 s keeps connection alive if camera stalls briefly.
                frame = _camera_reader.get_new_frame(timeout=0.5)
                if frame is not None:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame
                        + b"\r\n"
                )

        return Response(
            generate(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control":     "no-cache",
                "X-Accel-Buffering": "no",
                # Tell browser to display frames immediately, don't buffer
                "Transfer-Encoding": "chunked",
            },
        )

    @app.route("/camera/snapshot")
    def camera_snapshot():
        """Return a high-res JPEG from the main stream (1920x1080)."""
        if _camera_reader is None:
            return Response("Camera not enabled", status=503)
        frame = _camera_reader.capture_snapshot()
        if frame is None:
            return Response("No frame yet", status=503)
        return Response(frame, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

    @app.route("/snapshot")
    def snapshot():
        """Return a single JSON snapshot of latest sensor readings."""
        return jsonify(_sensor_manager.get_latest())

    @app.route("/model")
    def model_dashboard():
        """Dedicated realtime 4-class model dashboard (fixed 1024x600 layout)."""
        return render_template("pages/model.html")

    @app.route("/model/snapshot")
    def model_snapshot():
        """Return current model inference snapshot (plus latest sensor state)."""
        payload = _sensor_manager.get_latest()
        if _model_inference_service is not None:
            payload.update(_model_inference_service.get_latest())
        return jsonify(payload)

    @app.route("/model/debug")
    def model_debug():
        """Detailed model diagnostics: adapter state, features, and latest output."""
        if _model_inference_service is None:
            return jsonify({"status": "error", "message": "Model service not available"}), 503
        return jsonify(_model_inference_service.debug_snapshot())

    @app.route("/scan/qr_image/<scan_id>")
    def scan_qr_image(scan_id: str):
        """Generate QR PNG from the frozen scan's qr_url field."""
        if _scan_state_machine is None:
            return Response("Scan engine not available", status=503)

        try:
            import qrcode
        except ImportError:
            return Response("qrcode dependency not available", status=503)

        freezer = _scan_state_machine._freezer
        data = freezer.load(scan_id)
        if data is None:
            return Response(status=404)

        qr_url = data.get("qr_url", "")
        if not qr_url:
            logger.error("scan_qr_image: qr_url empty for scan_id=%s", scan_id)
            return Response("QR URL not found in scan data", status=500)

        try:
            qr_obj = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=2,
            )
            qr_obj.add_data(qr_url)
            qr_obj.make(fit=True)
            image = qr_obj.make_image(fill_color="black", back_color="white")

            buf = io.BytesIO()
            image.save(buf, format="PNG")
            buf.seek(0)
        except Exception as exc:
            logger.error("scan_qr_image: qrcode generation failed for scan_id=%s: %s", scan_id, exc)
            return Response(f"QR generation error: {exc}", status=500)

        return Response(
            buf.getvalue(),
            mimetype="image/png",
            headers={"Cache-Control": "no-cache"},
        )

    @app.route("/report/<scan_id>")
    def report_form(scan_id: str):
        """User-facing report page — verify token, show result + name form."""
        if _scan_state_machine is None:
            return render_template("pages/report.html", error="Scan engine not available.")

        token = request.args.get("token", "")
        freezer = _scan_state_machine._freezer

        if not freezer.verify_token(scan_id, token):
            return render_template("pages/report.html", error="Link tidak valid atau sudah kedaluwarsa."), 403

        scan_data = freezer.load(scan_id)
        if scan_data is None:
            return render_template("pages/report.html", error="Data scan tidak ditemukan."), 404

        from dashboard.report_content import (
            get_display_label, get_recommendation, get_severity_label, get_summary,
        )

        result  = scan_data.get("result") or {}
        metrics = result.get("metrics") or {}
        scores  = result.get("scores") or {}

        dominant   = result.get("dominant") or "normal"
        confidence = _safe_float(result.get("confidence"), 0.0)

        non_normal = {
            "stress":     _safe_float(scores.get("stress"), 0.0),
            "anxiety":    _safe_float(scores.get("anxiety"), 0.0),
            "depression": _safe_float(scores.get("depression"), 0.0),
        }
        overridden = dominant == "normal"
        if overridden:
            display_dominant   = max(non_normal, key=non_normal.get)
            display_confidence = non_normal[display_dominant]
        else:
            display_dominant   = dominant
            display_confidence = confidence

        return render_template(
            "pages/report.html",
            error=None,
            scan_id=scan_id,
            token=token,
            dominant=display_dominant,
            display_label=get_display_label(display_dominant),
            severity=get_severity_label(display_confidence),
            confidence=display_confidence,
            overridden=overridden,
            summary=get_summary(display_dominant, display_confidence),
            recommendation=get_recommendation(display_dominant, display_confidence),
            hr=_safe_float(metrics.get("hr"), 0.0),
            gsr_level=metrics.get("gsr_level") or "-",
            gsr_value=_safe_float(metrics.get("gsr_value"), 0.0),
            spo2=_safe_float(metrics.get("spo2"), 0.0),
            temperature=_safe_float(metrics.get("temperature"), 0.0),
            scores=SimpleNamespace(
                stress=non_normal["stress"],
                anxiety=non_normal["anxiety"],
                depression=non_normal["depression"],
            ),
            timestamp=scan_data.get("timestamp_end", "-"),
        )

    @app.route("/report/<scan_id>/generate", methods=["POST"])
    def report_generate(scan_id: str):
        """Generate PDF report and return as file download."""
        from dashboard.report_pdf import build_pdf

        if _scan_state_machine is None:
            return jsonify({"error": "Scan engine not available"}), 503

        token = request.args.get("token", "")
        freezer = _scan_state_machine._freezer

        if not freezer.verify_token(scan_id, token):
            return jsonify({"error": "Invalid or expired link"}), 403

        scan_data = freezer.load(scan_id)
        if scan_data is None:
            return jsonify({"error": "Scan data not found"}), 404

        name = (request.form.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name is required"}), 400

        try:
            buf = build_pdf(name, scan_data)
        except RuntimeError:
            return jsonify({"error": "Server busy, please retry"}), 429
        except Exception as exc:
            logger.error("report_generate: PDF build failed scan_id=%s: %s", scan_id, exc)
            return jsonify({"error": "Gagal membuat PDF"}), 500

        filename = f"neurovelis_{scan_id[:8]}.pdf"
        return send_file(
            buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/recalibrate/<sensor_name>", methods=["POST"])
    def recalibrate(sensor_name: str):
        """
        Trigger runtime recalibration for a sensor.
        POST /recalibrate/gsr  → retakes GSR baseline (sensor must NOT be worn)
        """
        try:
            result = _sensor_manager.recalibrate_sensor(sensor_name)
            logger.info("Recalibrated '%s': %s", sensor_name, result)
            return jsonify({"status": "ok", "sensor": sensor_name, **result})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 404
        except NotImplementedError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
        except Exception as exc:
            logger.error("Recalibrate '%s' failed: %s", sensor_name, exc)
            return jsonify({"status": "error", "message": str(exc)}), 500

    # ──────────────────────────────────────────────────────────────────
    # Experiment routes
    # ──────────────────────────────────────────────────────────────────

    def _require_sm():
        """Return (session_manager, respondent_registry) or raise 503."""
        if _session_manager is None or _respondent_registry is None:
            return None, None
        return _session_manager, _respondent_registry

    @app.route("/experiment")
    def experiment():
        """Main experiment control panel."""
        sm, rr = _require_sm()
        if sm is None:
            return Response("Experiment module not initialised", status=503)
        respondents = rr.get_all()
        active      = sm.get_active_session()
        duration    = getattr(config, "EXPERIMENT_SESSION_DURATION_S", 60)
        return render_template(
            "pages/experiment.html",
            respondents=respondents,
            active_session=active,
            default_duration=duration,
        )

    @app.route("/experiment/respondents")
    def experiment_respondents():
        """Respondent management page."""
        sm, rr = _require_sm()
        if rr is None:
            return Response("Experiment module not initialised", status=503)
        return render_template(
            "pages/respondents.html",
            respondents=rr.get_all(),
            next_id=rr.next_id(),
        )

    @app.route("/experiment/respondents/add", methods=["POST"])
    def experiment_respondents_add():
        """Register a new respondent (form POST)."""
        sm, rr = _require_sm()
        if rr is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        rid    = request.form.get("respondent_id", "").strip().upper()
        gender = request.form.get("gender", "M").strip().upper()
        age    = request.form.get("age", "0").strip()
        notes  = request.form.get("notes", "").strip()
        try:
            if not rid:
                rid = rr.next_id()
            rr.add(rid, gender, int(age), notes)
            return redirect(url_for("experiment_respondents"))
        except ValueError as exc:
            return render_template(
                "pages/respondents.html",
                respondents=rr.get_all(),
                next_id=rr.next_id(),
                error=str(exc),
            ), 400

    @app.route("/experiment/respondents/<respondent_id>", methods=["DELETE"])
    def experiment_respondents_delete(respondent_id: str):
        sm, rr = _require_sm()
        if rr is None:
            return jsonify({"status": "error"}), 503
        deleted = rr.delete(respondent_id)
        return jsonify({"status": "ok" if deleted else "not_found"})

    @app.route("/experiment/sessions")
    def experiment_sessions():
        """All sessions list."""
        sm, rr = _require_sm()
        if sm is None:
            return Response("Experiment module not initialised", status=503)
        sessions = sm.list_sessions()
        return render_template("pages/sessions.html", sessions=sessions)

    @app.route("/experiment/session/<session_id>", methods=["DELETE"])
    def experiment_session_delete(session_id: str):
        """Permanently delete a session and all its data."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        try:
            deleted = sm.delete_session(session_id)
            if not deleted:
                return jsonify({"status": "not_found"}), 404
            return jsonify({"status": "ok"})
        except RuntimeError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

    @app.route("/experiment/session/active")
    def experiment_session_active():
        """JSON: active session info (or null)."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify(None)
        return jsonify(sm.get_active_session())

    @app.route("/experiment/session/<session_id>")
    def experiment_session_detail(session_id: str):
        """JSON: metadata for a specific session."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error"}), 503
        meta = sm.get_session(session_id)
        if meta is None:
            return jsonify({"status": "not_found"}), 404
        return jsonify(meta)

    @app.route("/experiment/session/start", methods=["POST"])
    def experiment_session_start():
        """Start a new recording session (JSON or form POST)."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        data         = request.get_json(silent=True) or request.form
        respondent   = data.get("respondent_id", "")
        category     = data.get("category", "").strip().lower()
        duration_raw = data.get("duration_sec", None)
        try:
            duration = int(duration_raw) if duration_raw else None
        except (ValueError, TypeError):
            return jsonify({"status": "error",
                            "message": "duration_sec must be an integer"}), 400
        if not respondent:
            return jsonify({"status": "error",
                            "message": "respondent_id required"}), 400
        valid_categories = {"normal", "anxiety", "stress", "depression"}
        if category and category not in valid_categories:
            return jsonify({"status": "error",
                            "message": f"category must be one of {sorted(valid_categories)}"}), 400
        try:
            meta = sm.start_session(respondent, duration_sec=duration, category=category or None)
            # If form POST (browser), redirect back to experiment page
            if request.form:
                return redirect(url_for("experiment"))
            return jsonify({"status": "ok", "session": meta})
        except (RuntimeError, ValueError) as exc:
            if request.form:
                return redirect(url_for("experiment"))
            return jsonify({"status": "error", "message": str(exc)}), 400

    @app.route("/experiment/session/stop", methods=["POST"])
    def experiment_session_stop():
        """Stop the active recording session."""
        sm, _ = _require_sm()
        if sm is None:
            return jsonify({"status": "error", "message": "Module not ready"}), 503
        meta = sm.stop_session()
        if meta is None:
            if request.form:
                return redirect(url_for("experiment"))
            return jsonify({"status": "error", "message": "No active session"}), 400
        if request.form:
            return redirect(url_for("experiment_sessions"))
        return jsonify({"status": "ok", "session": meta})

    return app
