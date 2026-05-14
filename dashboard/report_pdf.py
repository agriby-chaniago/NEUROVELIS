"""
dashboard/report_pdf.py

PDF report generator using reportlab.
FIX 7: Semaphore(1) limits concurrent builds to 1 — prevents CPU spike on Pi.
"""

import threading
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# FIX 7: only 1 PDF build at a time
_pdf_semaphore = threading.Semaphore(1)

# Format per kondisi: list (threshold_pct, text)
# Ambil bucket pertama dengan threshold >= confidence%
SUMMARY_BUCKETS = {
    "stress": [
        (25,  "Terlihat sedikit kecenderungan respons stres selama pemeriksaan. "
              "Perubahan biometrik masih dalam rentang ringan dan dapat dipengaruhi kondisi sementara."),
        (50,  "Hasil biometrik menunjukkan kecenderungan respons stres ringan hingga sedang "
              "selama pemeriksaan berlangsung."),
        (75,  "Pola biometrik menunjukkan respons stres yang cukup konsisten selama pemeriksaan."),
        (100, "Respons biometrik menunjukkan tingkat stres yang dominan selama pemeriksaan berlangsung."),
    ],
    "anxiety": [
        (25,  "Terlihat sedikit peningkatan respons fisiologis selama pemeriksaan, "
              "namun masih dalam tingkat ringan."),
        (50,  "Pola biometrik menunjukkan kecenderungan ketegangan atau kecemasan ringan "
              "selama pemeriksaan."),
        (75,  "Respons biometrik menunjukkan pola kecemasan yang cukup konsisten selama pemeriksaan."),
        (100, "Pola biometrik menunjukkan tingkat kecemasan yang dominan selama pemeriksaan."),
    ],
    "depression": [
        (25,  "Aktivitas biometrik terlihat relatif tenang selama pemeriksaan dan "
              "masih dapat dipengaruhi banyak faktor sementara."),
        (50,  "Pola biometrik menunjukkan kecenderungan aktivitas fisiologis yang lebih rendah dari biasanya."),
        (75,  "Hasil biometrik menunjukkan pola aktivitas fisiologis rendah yang cukup konsisten."),
        (100, "Pola biometrik menunjukkan kecenderungan aktivitas fisiologis rendah yang dominan "
              "selama pemeriksaan berlangsung."),
    ],
    "normal": [
        (100, "Parameter fisiologis berada dalam rentang yang diharapkan selama pemeriksaan. "
              "Tetap jaga pola hidup sehat dan istirahat yang cukup."),
    ],
}

_DISCLAIMER = (
    "Hasil ini merupakan interpretasi biometrik berbasis AI dan tidak menggantikan "
    "evaluasi medis atau psikologis profesional."
)


def _safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def get_summary(condition: str, confidence: float) -> str:
    buckets = SUMMARY_BUCKETS.get(condition, SUMMARY_BUCKETS["normal"])
    pct = confidence * 100
    for threshold, text in buckets:
        if pct <= threshold:
            return text
    return buckets[-1][1]


def build_pdf(name: str, scan_data: dict) -> BytesIO:
    """
    Generate PDF report for a scan result.
    Raises RuntimeError if semaphore times out (FIX 7 — caller returns HTTP 429).
    """
    acquired = _pdf_semaphore.acquire(blocking=True, timeout=30)
    if not acquired:
        raise RuntimeError("PDF generation timed out — another build in progress")
    try:
        return _build(name, scan_data)
    finally:
        _pdf_semaphore.release()


def _build(name: str, scan_data: dict) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = getSampleStyleSheet()
    story  = []

    def h(text, level="Heading1"):
        story.append(Paragraph(text, styles[level]))

    def p(text):
        story.append(Paragraph(text, styles["Normal"]))
        story.append(Spacer(1, 0.3 * cm))

    def gap():
        story.append(Spacer(1, 0.5 * cm))

    result   = scan_data.get("result") or {}
    dominant = result.get("dominant") or "normal"
    conf     = _safe_float(result.get("confidence"), 0.0)
    metrics  = result.get("metrics") or {}
    scores   = result.get("scores") or {}

    # Konsisten dengan HTML: tidak tampilkan "normal" — gunakan kondisi tertinggi non-normal
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
        display_confidence = conf

    hr_val   = int(_safe_float(metrics.get("hr"), 0))
    gsr_lv   = metrics.get("gsr_level") or "-"
    gsr_str  = f"{_safe_float(metrics.get('gsr_value'), 0.0):.2f}"
    spo2_val = _safe_float(metrics.get("spo2"), 0.0)
    temp_val = _safe_float(metrics.get("temperature"), 0.0)

    h("Laporan Hasil Pemeriksaan NeuroVelis AI")
    gap()

    h("Identitas", "Heading2")
    p(f"<b>Nama:</b> {name}")
    p(f"<b>Scan ID:</b> {scan_data.get('scan_id', '-')}")
    p(f"<b>Waktu Scan:</b> {scan_data.get('timestamp_end', '-')}")
    gap()

    section_title = "Kecenderungan Dominan" if overridden else "Hasil Deteksi"
    h(section_title, "Heading2")
    p(f"<b>Kondisi:</b> {display_dominant.title()}")
    p(f"<b>Confidence:</b> {display_confidence * 100:.1f}%")
    gap()

    h("Parameter Biometrik", "Heading2")
    _table(story, [
        ["Parameter",            "Nilai"],
        ["Heart Rate (BPM)",     str(hr_val)],
        ["SpO2 (%)",             f"{spo2_val:.1f}"],
        ["Suhu Ruangan * (°C)",  f"{temp_val:.1f}"],
        ["GSR Level",            gsr_lv],
        ["GSR Value (µS)",       gsr_str],
    ])
    p("<i>* Suhu yang tercatat adalah suhu ruangan, bukan suhu tubuh.</i>")
    gap()

    h("Distribusi Skor Kelas", "Heading2")
    _table(story, [
        ["Kondisi",    "Skor"],
        ["Stres",      f"{non_normal['stress'] * 100:.1f}%"],
        ["Anxiety",    f"{non_normal['anxiety'] * 100:.1f}%"],
        ["Depresi",    f"{non_normal['depression'] * 100:.1f}%"],
    ])
    gap()

    h("Ringkasan", "Heading2")
    p(get_summary(display_dominant, display_confidence))
    p(f"<i>{_DISCLAIMER}</i>")

    doc.build(story)
    buf.seek(0)
    return buf


def _table(story: list, data: list):
    t = Table(data, colWidths=[8 * cm, 8 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#2D5BE3")),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2ff")]),
    ]))
    story.append(t)
