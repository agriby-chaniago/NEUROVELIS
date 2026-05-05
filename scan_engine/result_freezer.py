"""
scan_engine/result_freezer.py

Freezes a scan result into JSON and manages the data/scans/ directory.
Resolves the QR base URL via ngrok (if running) with LAN IP fallback.
"""

import json
import math
import os
import secrets      # FIX 6: cryptographically secure token
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

import config

SCANS_DIR = os.path.join(config.DATA_DIR, "scans")

# ── FIX 2 + 14: ngrok URL cache with separate TTLs ───────────────────────────
_url_cache_lock  = threading.Lock()
_last_base_url: str | None = None
_last_checked:   float = 0.0
_URL_CACHE_TTL_LAN   = 30.0   # LAN IP is stable — refresh every 30s
_URL_CACHE_TTL_NGROK = 10.0   # FIX 14: ngrok URL changes on restart — 10s TTL


def _resolve_base_url() -> str:
    """Try ngrok tunnel first, fallback to LAN IP. Result cached per TTL."""
    global _last_base_url, _last_checked
    now = time.monotonic()

    with _url_cache_lock:
        is_ngrok = bool(_last_base_url and "ngrok" in _last_base_url)
        ttl = _URL_CACHE_TTL_NGROK if is_ngrok else _URL_CACHE_TTL_LAN  # FIX 14
        if _last_base_url and (now - _last_checked) < ttl:
            return _last_base_url   # FIX 2: cache hit

    # cache miss — try ngrok local API with retry (guards against startup race condition)
    url = None
    for _ in range(3):
        try:
            resp = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=1)
            tunnels = resp.json().get("tunnels", [])
            for t in tunnels:
                if t.get("proto") == "https":
                    url = t["public_url"].rstrip("/")
                    break
            if url is None and tunnels:
                url = tunnels[0]["public_url"].rstrip("/")
            if url:
                break
        except Exception:
            time.sleep(0.5)  # only on failure — no overhead when ngrok ready

    if url is None:
        # fallback: LAN IP via UDP socket trick (never actually sends data)
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


# ── FIX 8: safe type conversions ─────────────────────────────────────────────

def _safe_float(val, default: float = 0.0) -> float:
    """None / NaN / Inf -safe float conversion."""
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int = 0) -> int:
    """None-safe int conversion."""
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


# ── ResultFreezer ─────────────────────────────────────────────────────────────

class ResultFreezer:
    _scan_count   = 0
    _CLEANUP_EVERY = 10   # FIX 5: periodic cleanup every N scans

    def __init__(self):
        os.makedirs(SCANS_DIR, exist_ok=True)

    def freeze(self, latest: dict, label: str, confidence: float) -> dict:
        """Freeze inference snapshot → JSON, return payload dict."""
        scan_id = str(uuid.uuid4())
        token   = secrets.token_urlsafe(16)   # FIX 6: secure random token
        ts      = datetime.now(timezone.utc).isoformat()
        base    = _resolve_base_url()
        qr_url  = f"{base}/report/{scan_id}?token={token}"

        hr_val  = _safe_int(latest.get("heart_rate_bpm"), 0)      # FIX 8
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
        with open(path, "w", encoding="utf-8") as f:
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
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def verify_token(self, scan_id: str, token: str) -> bool:
        data = self.load(scan_id)
        if not data:
            return False
        return data.get("token") == token   # FIX 6: compare stored token

    def cleanup_old_scans(self):
        """FIX 5 + 15: delete scan JSON files older than 24 hours, safe delete."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        try:
            entries = os.listdir(SCANS_DIR)
        except Exception:
            return
        for fname in entries:
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(SCANS_DIR, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(fpath), tz=timezone.utc)
                if mtime < cutoff:
                    os.remove(fpath)
            except FileNotFoundError:
                pass   # FIX 15: already deleted — not a crash
            except Exception:
                pass
