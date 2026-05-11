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

SUMMARIES = {
    "stress":     (
        "Hasil deteksi menunjukkan indikasi stres. "
        "Respons tubuh terhadap tekanan terdeteksi melalui variasi detak "
        "jantung dan konduktansi kulit yang meningkat. Disarankan untuk "
        "beristirahat dan mengurangi tekanan lingkungan."
    ),
    "anxiety":    (
        "Hasil deteksi menunjukkan indikasi kecemasan. "
        "Pola biometrik yang terdeteksi mencerminkan aktivasi sistem saraf "
        "simpatik. Teknik relaksasi seperti pernapasan dalam dapat membantu."
    ),
    "depression": (
        "Hasil deteksi menunjukkan indikasi depresi. "
        "Parameter fisiologis menunjukkan pola aktivasi rendah yang konsisten. "
        "Konsultasi dengan profesional kesehatan jiwa dianjurkan."
    ),
    "normal":     (
        "Hasil deteksi menunjukkan kondisi dalam batas normal. "
        "Parameter fisiologis berada dalam rentang yang diharapkan. "
        "Tetap jaga pola hidup sehat dan istirahat yang cukup."
    ),
}


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

    # FIX 8: None-safe reads throughout
    result   = scan_data.get("result") or {}
    dominant = result.get("dominant") or "normal"
    conf     = float(result.get("confidence") or 0.0)
    metrics  = result.get("metrics") or {}
    scores   = result.get("scores") or {}

    h("Laporan Deteksi NeuroVelis AI")
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

    hr_val       = metrics.get("hr") or 0
    gsr_lv       = metrics.get("gsr_level") or "-"
    gsr_raw      = metrics.get("gsr_value")
    gsr_str      = f"{float(gsr_raw):.2f}" if gsr_raw is not None else "-"
    spo2_val = metrics.get("spo2") or 0
    temp_val = metrics.get("temperature") or 0

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
        ["Stres",      f"{float(scores.get('stress') or 0) * 100:.1f}%"],
        ["Kecemasan",  f"{float(scores.get('anxiety') or 0) * 100:.1f}%"],
        ["Depresi",    f"{float(scores.get('depression') or 0) * 100:.1f}%"],
    ])
    gap()

    h("Ringkasan", "Heading2")
    p(SUMMARIES.get(dominant, SUMMARIES["normal"]))
    p(
        "<i>Catatan: Hasil ini bersifat informatif dan tidak menggantikan "
        "diagnosis klinis oleh profesional kesehatan jiwa.</i>"
    )

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
