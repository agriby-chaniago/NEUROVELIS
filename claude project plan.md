# Plan: Scan → Freeze Result → QR → HP Form → PDF (Hardened)

## Context

Project NEUROVELIS already has a QR stability system inside `model_inference_service.py` (30s window → `neurovelis.qr.v1` payload for psychiatrist app forwarding). That system is **not** user-facing and will be **removed** per user request.

This plan adds a **parallel scan flow**: face-triggered, state-machine-driven, generates a per-scan JSON, shows QR pointing to a self-hosted form page (`/report/<scan_id>?token=xxx`), user inputs name on phone → downloads PDF.

This revision incorporates 10 runtime hardening fixes for Raspberry Pi stability.

---

## Architecture

```
ScanStateMachine (scan_engine/scan_state_machine.py)
  ↑ polls  model_inference_service.get_latest()     (confidence, label, face_detected)
  ↑ polls  model_inference_service.get_mesh_latest() (landmarks for bbox stability)
  ↓ writes data/scans/<scan_id>.json  (via ResultFreezer)
  ↓ exposes get_state() → Flask reads for SSE + /report endpoints

dashboard/app.py
  /stream SSE  ← adds scan_state fields to existing event stream
  GET  /report/<scan_id>             ← render report.html form
  POST /report/<scan_id>/generate    ← generate + stream PDF
  GET  /scan/qr_image/<scan_id>      ← serve QR PNG

scan_engine/result_freezer.py
  freeze(latest, label, confidence) → frozen dict
  verify_token(scan_id, token)       → bool
  cleanup_old_scans()                → delete files >24h

dashboard/report_pdf.py
  build_pdf(name, scan_data) → BytesIO  (Semaphore-guarded)

dashboard/templates/pages/report.html
  name input form + result preview
```

---

## State Machine

```
IDLE
  → face_detected == True           → DETECTING (timer 8s)

DETECTING
  → face gone                       → IDLE
  → 8s elapsed                      → WARMUP (5s countdown)

WARMUP
  → face gone                       → IDLE
  → 5s elapsed                      → STABILIZING (lock bbox)

STABILIZING (20s)
  → face gone > 2s                  → IDLE (cancel)
  → bbox drift > CONSISTENCY_THRESH → IDLE (cancel)
  → 20s done + confidence≥0.7 + face_stable + face_consistent + valid label
                                    → RESULT_READY

RESULT_READY
  → freeze result, save JSON        → QR_DISPLAY (immediate)

QR_DISPLAY (default 120s)
  → face absent > 3s                → COOLDOWN
  → timer expires                   → COOLDOWN

COOLDOWN
  → face absent > COOLDOWN_DUR      → IDLE
```

---

## New Files

### 1. `scan_engine/__init__.py`
Empty.

### 2. `scan_engine/scan_state_machine.py`

```python
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
    "COOLDOWN":     "Thank you!",
}


class ScanStateMachine:
    def __init__(self, model_inference_service):
        self._mis = model_inference_service
        self._freezer = ResultFreezer()
        self._lock = threading.Lock()

        # state
        self._state = "IDLE"
        self._state_entered = time.monotonic()

        # face tracking
        self._prev_bbox = None          # FIX 1: bbox-based stability (not model_face_motion)
        self._locked_bbox = None        # locked at STABILIZING entry
        self._face_absent_since = None

        # frozen result (thread-safe copy via local var in get_state)
        self._frozen_result = None

        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._freezer.cleanup_old_scans()   # FIX 5: cleanup on startup
        self._thread.start()

    # ── public ──────────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        with self._lock:                    # FIX 3: lock for all shared reads
            state = self._state
            elapsed = time.monotonic() - self._state_entered
            frozen = self._frozen_result    # local copy — safe to use outside lock

        remaining = 0.0
        dur_map = {
            "DETECTING":   config.SCAN_DETECTING_DURATION,
            "WARMUP":      config.SCAN_WARMUP_DURATION,
            "STABILIZING": config.SCAN_STABILIZING_DURATION,
            "QR_DISPLAY":  config.QR_DISPLAY_DURATION,
            "COOLDOWN":    config.SCAN_COOLDOWN_DURATION,
        }
        if state in dur_map:
            remaining = max(0.0, dur_map[state] - elapsed)

        return {
            "scan_state":        state,
            "scan_elapsed_s":    round(elapsed, 1),
            "scan_remaining_s":  round(remaining, 1),
            "scan_hint_message": _HINTS.get(state, ""),   # FIX 10: UX hint
            "scan_id":           frozen["scan_id"] if frozen else None,
            "scan_qr_url":       frozen.get("qr_url") if frozen else None,
        }

    # ── internal helpers ────────────────────────────────────────────────────

    def _transition(self, new_state):
        self._state = new_state
        self._state_entered = time.monotonic()

    def _elapsed(self) -> float:
        return time.monotonic() - self._state_entered

    @staticmethod
    def _bbox_from_landmarks(landmarks):
        """Return (cx, cy, w) from normalized landmark list, or None."""
        if not landmarks:
            return None
        xs = [lm[0] for lm in landmarks]
        ys = [lm[1] for lm in landmarks]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        return ((xmin + xmax) / 2, (ymin + ymax) / 2, xmax - xmin)

    def _face_stable(self, bbox) -> bool:
        """FIX 1: stability = movement of bbox center relative to face width."""
        if bbox is None or self._prev_bbox is None:
            return True             # no prev frame yet → don't penalize
        cx0, cy0, w0 = self._prev_bbox
        cx1, cy1, _  = bbox
        if w0 == 0:
            return True
        movement = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
        return movement < config.FACE_STABLE_THRESHOLD

    def _face_consistent(self, bbox) -> bool:
        """Bbox must not drift from locked position (STABILIZING guard)."""
        if self._locked_bbox is None or bbox is None:
            return True
        cx0, cy0, w0 = self._locked_bbox
        cx1, cy1, _  = bbox
        if w0 == 0:
            return True
        drift = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
        return drift < config.FACE_CONSISTENCY_THRESHOLD

    # ── tick ────────────────────────────────────────────────────────────────

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(0.1)

    def _tick(self):
        latest   = self._mis.get_latest()
        mesh     = self._mis.get_mesh_latest()
        face     = latest.get("model_face_detected", False)
        landmarks = (mesh or {}).get("model_face_landmarks", [])

        # FIX 8: safe sensor reads
        confidence = float(latest.get("model_confidence_top1") or 0.0)
        label      = latest.get("model_label_top1") or ""

        bbox = self._bbox_from_landmarks(landmarks)

        with self._lock:
            state = self._state

            if state == "IDLE":
                if face:
                    self._transition("DETECTING")
                    self._face_absent_since = None
                    self._prev_bbox = bbox      # FIX 1: reset prev on entry

            elif state == "DETECTING":
                if not face:
                    self._transition("IDLE")
                else:
                    self._prev_bbox = bbox
                    if self._elapsed() >= config.SCAN_DETECTING_DURATION:
                        self._transition("WARMUP")

            elif state == "WARMUP":
                if not face:
                    self._transition("IDLE")
                else:
                    self._prev_bbox = bbox
                    if self._elapsed() >= config.SCAN_WARMUP_DURATION:
                        self._locked_bbox = bbox
                        self._transition("STABILIZING")

            elif state == "STABILIZING":                # FIX 4: no premature return
                if not face:
                    if self._face_absent_since is None:
                        self._face_absent_since = time.monotonic()
                    elif time.monotonic() - self._face_absent_since > 2.0:
                        self._transition("IDLE")
                        self._locked_bbox = None
                        self._face_absent_since = None
                        self._prev_bbox = None
                else:
                    self._face_absent_since = None
                    stable     = self._face_stable(bbox)
                    consistent = self._face_consistent(bbox)
                    self._prev_bbox = bbox      # FIX 1: always update

                    if not consistent:
                        self._transition("IDLE")
                        self._locked_bbox = None
                        self._prev_bbox   = None
                    elif (
                        self._elapsed() >= config.SCAN_STABILIZING_DURATION
                        and stable
                        and confidence >= config.CONFIDENCE_THRESHOLD
                        and label not in (None, "", "unknown")   # FIX 9
                    ):
                        frozen = self._freezer.freeze(latest, label, confidence)
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
                        self._transition("IDLE")
                        self._face_absent_since = None
                        self._prev_bbox = None
                else:
                    self._face_absent_since = None
```

### 3. `scan_engine/result_freezer.py`

```python
import os
import json
import math
import time
import socket
import secrets          # FIX 6: cryptographically secure token
import threading
from datetime import datetime, timezone, timedelta

import requests
import config

SCANS_DIR = os.path.join(config.DATA_DIR, "scans")

# ── FIX 2: Ngrok URL cache (TTL 30s) ────────────────────────────────────────
_url_cache_lock = threading.Lock()
_last_base_url: str | None = None
_last_checked: float = 0.0
_URL_CACHE_TTL = 30.0


def _resolve_base_url() -> str:
    global _last_base_url, _last_checked
    now = time.monotonic()
    with _url_cache_lock:
        if _last_base_url and (now - _last_checked) < _URL_CACHE_TTL:
            return _last_base_url          # cache hit

    # cache miss → try ngrok
    url = None
    try:
        resp = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1)
        tunnels = resp.json().get("tunnels", [])
        for t in tunnels:
            if t.get("proto") == "https":
                url = t["public_url"].rstrip("/")
                break
        if url is None and tunnels:
            url = tunnels[0]["public_url"].rstrip("/")
    except Exception:
        pass

    if url is None:
        # fallback: LAN IP via UDP socket trick
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "localhost"
        url = f"http://{ip}:{config.DASHBOARD_PORT}"

    with _url_cache_lock:
        _last_base_url = url
        _last_checked  = now
    return url


def _safe_float(val, default=0.0) -> float:
    """FIX 8: None/NaN-safe float."""
    try:
        f = float(val)
        return default if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=0) -> int:
    """FIX 8: None-safe int."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _gsr_level(uS: float) -> str:
    if uS < 5:
        return "Low"
    if uS < 15:
        return "Medium"
    return "High"


class ResultFreezer:
    _scan_count = 0
    _CLEANUP_EVERY = 10     # FIX 5: also clean every 10 scans

    def __init__(self):
        os.makedirs(SCANS_DIR, exist_ok=True)

    def freeze(self, latest: dict, label: str, confidence: float) -> dict:
        scan_id = str(__import__("uuid").uuid4())
        token   = secrets.token_urlsafe(16)   # FIX 6: secure random token
        ts      = datetime.now(timezone.utc).isoformat()
        base    = _resolve_base_url()
        qr_url  = f"{base}/report/{scan_id}?token={token}"

        hr_val  = _safe_int(latest.get("heart_rate_bpm"), 0)   # FIX 8
        gsr_val = _safe_float(latest.get("gsr_conductance_us"), 0.0)

        payload = {
            "scan_id":         scan_id,
            "timestamp_start": latest.get("model_timestamp_utc") or ts,
            "timestamp_end":   ts,
            "result": {
                "dominant":   label,
                "confidence": round(_safe_float(confidence), 4),
                "metrics": {
                    "hr":        hr_val,
                    "gsr_level": _gsr_level(gsr_val),
                    "gsr_value": round(gsr_val, 3),
                },
                "scores": {
                    "stress":     round(_safe_float(latest.get("model_probs_stress")), 4),
                    "anxiety":    round(_safe_float(latest.get("model_probs_anxiety")), 4),
                    "depression": round(_safe_float(latest.get("model_probs_depression")), 4),
                },
            },
            "token":  token,
            "qr_url": qr_url,
        }

        path = os.path.join(SCANS_DIR, f"{scan_id}.json")
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)

        # FIX 5: periodic cleanup
        ResultFreezer._scan_count += 1
        if ResultFreezer._scan_count % self._CLEANUP_EVERY == 0:
            self.cleanup_old_scans()

        return payload

    def load(self, scan_id: str) -> dict | None:
        path = os.path.join(SCANS_DIR, f"{scan_id}.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def verify_token(self, scan_id: str, token: str) -> bool:
        data = self.load(scan_id)
        if not data:
            return False
        return data.get("token") == token      # FIX 6: compare stored token (no recompute)

    def cleanup_old_scans(self):
        """FIX 5: delete scan JSON files older than 24 hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for fname in os.listdir(SCANS_DIR):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(SCANS_DIR, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
                if mtime < cutoff:
                    os.remove(fpath)
            except Exception:
                pass
```

### 4. `dashboard/report_pdf.py`

```python
import threading
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)

# FIX 7: max 1 concurrent PDF build — prevents CPU spike on Pi
_pdf_semaphore = threading.Semaphore(1)

SUMMARIES = {
    "stress":     "Hasil deteksi menunjukkan indikasi stres. Respons tubuh terhadap tekanan terdeteksi melalui variasi detak jantung dan konduktansi kulit yang meningkat.",
    "anxiety":    "Hasil deteksi menunjukkan indikasi kecemasan. Pola biometrik yang terdeteksi mencerminkan aktivasi sistem saraf simpatik.",
    "depression": "Hasil deteksi menunjukkan indikasi depresi. Parameter fisiologis menunjukkan pola aktivasi rendah yang konsisten.",
    "normal":     "Hasil deteksi menunjukkan kondisi dalam batas normal. Parameter fisiologis berada dalam rentang yang diharapkan.",
}


def build_pdf(name: str, scan_data: dict) -> BytesIO:
    """Generate PDF report. Semaphore-guarded (1 at a time)."""
    acquired = _pdf_semaphore.acquire(blocking=True, timeout=30)
    if not acquired:
        raise RuntimeError("PDF generation timed out (another in progress)")
    try:
        return _build(name, scan_data)
    finally:
        _pdf_semaphore.release()


def _build(name: str, scan_data: dict) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    story  = []

    def h(text, style="Heading1"):
        story.append(Paragraph(text, styles[style]))

    def p(text):
        story.append(Paragraph(text, styles["Normal"]))
        story.append(Spacer(1, 0.3*cm))

    def gap():
        story.append(Spacer(1, 0.5*cm))

    result   = scan_data.get("result", {})
    dominant = result.get("dominant") or "normal"     # FIX 8: None-safe
    conf     = float(result.get("confidence") or 0.0)
    metrics  = result.get("metrics") or {}
    scores   = result.get("scores") or {}

    h("Laporan Deteksi NeuroVELIS")
    gap()

    h("Identitas", "Heading2")
    p(f"<b>Nama:</b> {name}")
    p(f"<b>Scan ID:</b> {scan_data.get('scan_id', '-')}")
    p(f"<b>Waktu Scan:</b> {scan_data.get('timestamp_end', '-')}")
    gap()

    h("Hasil Deteksi", "Heading2")
    p(f"<b>Kondisi Dominan:</b> {dominant.title()}")
    p(f"<b>Confidence:</b> {conf * 100:.1f}%")
    gap()

    hr_val  = metrics.get("hr") or 0
    gsr_lv  = metrics.get("gsr_level") or "-"
    gsr_val = metrics.get("gsr_value")
    gsr_str = f"{float(gsr_val):.2f}" if gsr_val is not None else "-"

    h("Parameter Biometrik", "Heading2")
    tdata = [
        ["Parameter",       "Nilai"],
        ["Heart Rate (BPM)", str(hr_val)],
        ["GSR Level",        gsr_lv],
        ["GSR Value (µS)",   gsr_str],
    ]
    _table(story, tdata)
    gap()

    h("Distribusi Skor Kelas", "Heading2")
    sdata = [
        ["Kondisi",   "Skor"],
        ["Stres",     f"{float(scores.get('stress') or 0) * 100:.1f}%"],
        ["Kecemasan", f"{float(scores.get('anxiety') or 0) * 100:.1f}%"],
        ["Depresi",   f"{float(scores.get('depression') or 0) * 100:.1f}%"],
    ]
    _table(story, sdata)
    gap()

    h("Ringkasan", "Heading2")
    p(SUMMARIES.get(dominant, SUMMARIES["normal"]))
    p("<i>Catatan: Hasil ini bersifat informatif dan tidak menggantikan diagnosis klinis oleh profesional kesehatan jiwa.</i>")

    doc.build(story)
    buf.seek(0)
    return buf


def _table(story, data):
    t = Table(data, colWidths=[8*cm, 8*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0),  colors.HexColor("#2D5BE3")),
        ("TEXTCOLOR",  (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",   (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2ff")]),
    ]))
    story.append(t)
```

### 5. `dashboard/templates/pages/report.html`
Minimal responsive form page. Receives scan data from Flask, POSTs name to `/report/<scan_id>/generate`.

---

## Modified Files

### `config.py` — add at bottom
```python
# Scan state machine
SCAN_DETECTING_DURATION      = 8      # s face present before warmup
SCAN_WARMUP_DURATION         = 5      # s warmup countdown
SCAN_STABILIZING_DURATION    = 20     # s stabilizing window
SCAN_COOLDOWN_DURATION       = 5      # s wait for user to leave
QR_DISPLAY_DURATION          = 120    # s QR visible
FACE_STABLE_THRESHOLD        = 0.02   # bbox movement/width — stable if below
FACE_CONSISTENCY_THRESHOLD   = 0.08   # bbox drift from locked position
CONFIDENCE_THRESHOLD         = 0.70   # min confidence to freeze result
```

### `dashboard/app.py`
1. Import `ScanStateMachine`, `ResultFreezer`
2. `create_app()` accepts `scan_state_machine` param
3. `/stream` SSE generator: merge `_scan_state_machine.get_state()` into event dict
4. Remove `/model/qr_payload` and `/model/qr_image` routes (psychiatrist QR — deleted)
5. Add routes:
   - `GET /scan/qr_image/<scan_id>` → load frozen `qr_url`, generate QR PNG, serve
   - `GET /report/<scan_id>` → verify token, load JSON, render `report.html`
   - `POST /report/<scan_id>/generate` → verify token, call `build_pdf`, return as download

### `main.py`
1. Import `ScanStateMachine`
2. After inference service starts: `ssm = ScanStateMachine(model_inference_service)` → `ssm.start()`
3. Pass to `create_app(scan_state_machine=ssm)`

### `dashboard/templates/pages/model.html`
- Remove entire `#result-qr-panel` section
- Add `#scan-panel` below metrics grid:
  - State badge: color-coded per state
  - Progress bar: visible during STABILIZING (width = elapsed/20s × 100%)
  - Hint message: `scan_hint_message` field from SSE
  - QR section: visible during QR_DISPLAY — image from `/scan/qr_image/<scan_id>` + countdown + URL

### `dashboard/static/js/pages/model.js`
- Remove all QR psychiatrist code: `updateQrPanelState`, `refreshQrPayload`, `updateQrPanelContents`, `showQrPanel`, `hideQrPanel`, `qrLastRevision`, `qrLastLabel`, `qrPayloadCache`
- Add `updateScanState(data)`:
  - Sets badge text/color for each state
  - Shows/hides progress bar, QR section based on state
  - On QR_DISPLAY: sets `<img src="/scan/qr_image/${data.scan_id}">` once per scan_id change
  - Shows `scan_remaining_s` countdown
  - Shows `scan_hint_message`

### `dashboard/static/css/pages/model.css`
- Remove `.result-qr-panel` styles
- Add `#scan-panel`: badge colors, progress bar animation, QR display layout

### `requirements.txt`
Add `reportlab>=4.0.0`

---

## Critical Files

| File | Action |
|------|--------|
| `scan_engine/__init__.py` | **CREATE** |
| `scan_engine/scan_state_machine.py` | **CREATE** |
| `scan_engine/result_freezer.py` | **CREATE** |
| `dashboard/report_pdf.py` | **CREATE** |
| `dashboard/templates/pages/report.html` | **CREATE** |
| `config.py` | **MODIFY** — add scan config |
| `dashboard/app.py` | **MODIFY** — add routes, remove QR psikiater, SSE merge |
| `main.py` | **MODIFY** — init + start ScanStateMachine |
| `dashboard/templates/pages/model.html` | **MODIFY** — scan panel UI |
| `dashboard/static/js/pages/model.js` | **MODIFY** — remove QR, add updateScanState |
| `dashboard/static/css/pages/model.css` | **MODIFY** — scan panel styles |
| `requirements.txt` | **MODIFY** — add reportlab |

---

## Fix Summary

| # | Fix | Location |
|---|-----|----------|
| 1 | Face stability via bbox diff (not model_face_motion) | `scan_state_machine._face_stable()` + `_prev_bbox` |
| 2 | Ngrok URL cached 30s (not re-requested every freeze) | `result_freezer._resolve_base_url()` |
| 3 | All `_state`/`_frozen_result` access inside lock, local copy in `get_state` | `scan_state_machine.get_state()` |
| 4 | STABILIZING uses if/else — no premature return skipping frames | `scan_state_machine._tick()` STABILIZING branch |
| 5 | `cleanup_old_scans()` — deletes JSON >24h, called on startup + every 10 scans | `result_freezer.cleanup_old_scans()` |
| 6 | Token = `secrets.token_urlsafe(16)` (not SHA-256 of scan_id+secret) | `result_freezer.freeze()` |
| 7 | `Semaphore(1)` around PDF build — 1 concurrent max | `report_pdf.build_pdf()` |
| 8 | `_safe_float/_safe_int` + `or` fallbacks — no None/NaN in PDF | `result_freezer.freeze()` + `report_pdf._build()` |
| 9 | Label validated: not None, not `""`, not `"unknown"` | `scan_state_machine._tick()` STABILIZING freeze condition |
| 10 | `scan_hint_message` in `get_state()` output | `scan_state_machine.get_state()` + frontend |

---

## Integration Notes

1. `ScanStateMachine` is a read-only consumer of `model_inference_service` — no changes to inference service
2. `data/scans/` is separate from `data/sessions/` (experiment sessions)
3. `requests` library: verify in `requirements.txt` (Flask apps typically include it)
4. Ngrok: user runs ngrok independently; SSM just reads local API at `127.0.0.1:4040`
5. Face `model_face_motion` NOT used — stability computed purely from landmark bbox delta between frames

---

## Hardening Patch — Fix 11–18

These are surgical patches to the code already defined above. Apply on top of Fix 1–10.

---

### `scan_engine/scan_state_machine.py` — patches

**`__init__` — add new fields (Fix 12, 13)**
```python
# after self._face_absent_since = None:
self._inconsistent_count = 0   # FIX 12: jitter counter
```

**`_HINTS` — update COOLDOWN (Fix 17)**
```python
"COOLDOWN": "Please step away to start a new scan",
# was: "Thank you!"
```

**`_face_stable()` — first-frame fix (Fix 11)**
```python
def _face_stable(self, bbox) -> bool:
    if bbox is None:
        return False
    if self._prev_bbox is None:
        self._prev_bbox = bbox          # FIX 11: seed prev, but NOT stable yet
        return False
    cx0, cy0, w0 = self._prev_bbox
    cx1, cy1, _  = bbox
    if w0 == 0:
        return True
    movement = ((cx1 - cx0) ** 2 + (cy1 - cy0) ** 2) ** 0.5 / w0
    return movement < config.FACE_STABLE_THRESHOLD
```

**`_reset_to_idle()` — new helper (Fix 13, 18)**
```python
def _reset_to_idle(self):
    """FIX 13/18: full clean reset — no stale data leaks into next scan."""
    self._transition("IDLE")
    self._frozen_result      = None   # clear stale result
    self._prev_bbox          = None   # clear bbox history
    self._locked_bbox        = None
    self._inconsistent_count = 0      # FIX 12: clear jitter counter
    self._face_absent_since  = None
```

**STABILIZING branch — jitter tolerance (Fix 12) + use `_reset_to_idle` (Fix 13, 18)**

Replace the existing STABILIZING `else` block:
```python
else:
    self._face_absent_since = None
    stable     = self._face_stable(bbox)
    consistent = self._face_consistent(bbox)
    self._prev_bbox = bbox

    if not consistent:
        self._inconsistent_count += 1       # FIX 12: count, don't instant-cancel
        if self._inconsistent_count > 3:
            self._reset_to_idle()           # FIX 13/18: clean reset
    else:
        self._inconsistent_count = 0        # FIX 12: reset on stable frame
        if (
            self._elapsed() >= config.SCAN_STABILIZING_DURATION
            and stable
            and confidence >= config.CONFIDENCE_THRESHOLD
            and label not in (None, "", "unknown")
        ):
            frozen = self._freezer.freeze(latest, label, confidence)
            self._frozen_result = frozen
            self._transition("RESULT_READY")
```

**DETECTING, WARMUP, COOLDOWN — use `_reset_to_idle` (Fix 13, 18)**
```python
# DETECTING: face gone
elif state == "DETECTING":
    if not face:
        self._reset_to_idle()           # FIX 13/18: was self._transition("IDLE")
    else:
        ...

# WARMUP: face gone
elif state == "WARMUP":
    if not face:
        self._reset_to_idle()           # FIX 13/18: was self._transition("IDLE")
    else:
        ...

# STABILIZING: face absent > 2s
elif time.monotonic() - self._face_absent_since > 2.0:
    self._reset_to_idle()               # FIX 13/18: was manual reset + transition

# COOLDOWN: elapsed → IDLE
elif time.monotonic() - self._face_absent_since >= config.SCAN_COOLDOWN_DURATION:
    self._reset_to_idle()               # FIX 13/18: was self._transition("IDLE") + manual clears
```

---

### `scan_engine/result_freezer.py` — patches

**`_resolve_base_url()` — ngrok TTL (Fix 14)**
```python
# Replace the single _URL_CACHE_TTL = 30.0 with:
_URL_CACHE_TTL_LAN   = 30.0
_URL_CACHE_TTL_NGROK = 10.0   # FIX 14: ngrok restarts change URL — shorter TTL

# In _resolve_base_url(), replace the cache check:
is_ngrok = bool(_last_base_url and "ngrok" in _last_base_url)
ttl = _URL_CACHE_TTL_NGROK if is_ngrok else _URL_CACHE_TTL_LAN
if _last_base_url and (now - _last_checked) < ttl:
    return _last_base_url
```

**`cleanup_old_scans()` — safe delete (Fix 15)**
```python
try:
    os.remove(fpath)
except FileNotFoundError:
    pass                    # FIX 15: already gone — not a crash
except Exception:
    pass
```

---

### `dashboard/app.py` — patches

**PDF endpoint — return 429 on semaphore timeout (Fix 16)**
```python
@app.route("/report/<scan_id>/generate", methods=["POST"])
def report_generate(scan_id):
    ...
    try:
        buf = build_pdf(name, scan_data)
    except RuntimeError:
        return {"error": "Server busy, please retry"}, 429   # FIX 16
    ...
```

---

### Fix 11–18 summary

| # | Fix | Where |
|---|-----|-------|
| 11 | First frame → seed `prev_bbox`, return `False` (not stable) | `_face_stable()` |
| 12 | Jitter counter — cancel only after >3 inconsistent frames | `_tick()` STABILIZING + `__init__` |
| 13 | `_reset_to_idle()` clears all state vars on every IDLE transition | new helper, all cancel paths |
| 14 | Ngrok TTL = 10s, LAN TTL = 30s | `_resolve_base_url()` |
| 15 | `except FileNotFoundError: pass` in cleanup | `cleanup_old_scans()` |
| 16 | `RuntimeError` from semaphore → HTTP 429 | app.py PDF endpoint |
| 17 | COOLDOWN hint = "Please step away to start a new scan" | `_HINTS` dict |
| 18 | All cancel/reset paths call `_reset_to_idle()` (bbox + frozen + counter) | `_tick()` all states |

---

## Verification

1. `python main.py` → dashboard loads without error
2. Stand in front of camera → states cycle: IDLE → DETECTING → WARMUP → STABILIZING → QR_DISPLAY
3. QR appears → scan with phone → `http://<ngrok-or-lan>/report/<scan_id>?token=xxx` opens
4. Enter name → POST → PDF downloads
5. `data/scans/<scan_id>.json` exists with correct schema
6. Leave camera view → COOLDOWN → IDLE (new cycle ready)
7. Bad token → 403
8. `python -m pytest tests/` → no regressions
9. Verify scan JSON files older than 24h deleted on next startup
