"""
sensors/buzzer.py  –  Grove Buzzer v1.3 driver + alert logic.

Hardware: Active buzzer (HIGH = ON) connected to D5 port on Grove Base HAT.
D5 port = GPIO 5 on Raspberry Pi.
Uses lgpio (Pi 5 compatible — replaces RPi.GPIO).

Alert conditions and beep patterns:
  - SpO2 < ALERT_SPO2_LOW         → 3 short beeps        (most urgent)
  - HR > ALERT_HR_HIGH            → 2 fast beeps          (tachycardia)
  - HR < ALERT_HR_LOW             → 2 slow beeps          (bradycardia)
  - GSR > ALERT_GSR_HIGH_US       → 1 long beep           (stress)
  - sensor_error in data          → 1 long + 1 short beep (hardware fault)

Each condition has its own independent cooldown — a GSR alert will not
suppress a simultaneous SpO2 alert.
"""

import logging
import time
import threading
from typing import Any

import config

logger = logging.getLogger(__name__)


class Buzzer:
    """
    Controls the Grove Buzzer v1.3 and evaluates alert conditions.

    Usage
    -----
        buzzer = Buzzer()
        buzzer.setup()
        buzzer.check_and_alert(latest_data)   # call after each sensor update
        buzzer.close()
    """

    def __init__(self):
        self._gpio_handle = None
        self._gpio_chip = None
        self._pin = config.BUZZER_GPIO_PIN
        self._enabled = config.BUZZER_ENABLED
        self._min_consecutive_hits = max(
            1, int(getattr(config, "BUZZER_MIN_CONSECUTIVE_HITS", 1))
        )
        # Per-condition cooldown: each key has its own last-fired timestamp.
        # Prevents one condition from suppressing another during cooldown.
        self._last_alert_time: dict[str, float] = {}
        # Per-condition consecutive hit counters to suppress transient noise.
        self._condition_hits: dict[str, int] = {}
        self._lock = threading.Lock()   # prevent concurrent beep calls
        self._active = False            # is buzzer currently ON?

    # ── Setup / teardown ──────────────────────────────────────────────────

    def setup(self):
        """Initialise lgpio and configure the buzzer pin as output.

        IMPORTANT — Raspberry Pi 5 uses gpiochip4 (not gpiochip0).
        config.BUZZER_GPIO_CHIP must be set to 4 for Pi 5.
        """
        if not self._enabled:
            logger.info("Buzzer disabled in config — skipping setup.")
            return
        try:
            import lgpio
            preferred_chip = getattr(config, "BUZZER_GPIO_CHIP", 4)

            # Try configured chip first, then common fallback chips.
            chip_candidates = [preferred_chip]
            for fallback_chip in (4, 0):
                if fallback_chip not in chip_candidates:
                    chip_candidates.append(fallback_chip)

            last_error = None
            for chip in chip_candidates:
                handle = None
                try:
                    handle = lgpio.gpiochip_open(chip)
                    # Drive LOW immediately — prevents floating HIGH buzz on startup
                    lgpio.gpio_claim_output(handle, self._pin, 0)
                    lgpio.gpio_write(handle, self._pin, 0)   # explicit LOW
                    self._gpio_handle = handle
                    self._gpio_chip = chip
                    logger.info(
                        "Buzzer ready on GPIO %d via gpiochip%d (D5, Grove Base HAT)",
                        self._pin, chip,
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    if handle is not None:
                        try:
                            lgpio.gpiochip_close(handle)
                        except Exception:
                            pass
                    logger.warning(
                        "Buzzer init failed on gpiochip%d: %s",
                        chip,
                        exc,
                    )

            self._gpio_handle = None
            self._gpio_chip = None
            logger.error(
                "Buzzer setup failed for chips %s: %s",
                chip_candidates,
                last_error,
            )
        except Exception as exc:
            logger.error("Buzzer setup failed: %s", exc)
            self._gpio_handle = None
            self._gpio_chip = None

    def close(self):
        """Turn off buzzer and release GPIO."""
        self._set_pin(False)
        if self._gpio_handle is not None:
            try:
                import lgpio
                lgpio.gpiochip_close(self._gpio_handle)
            except Exception:
                pass
            self._gpio_handle = None
            self._gpio_chip = None
        logger.info("Buzzer closed.")

    # ── Alert evaluation ──────────────────────────────────────────────────

    def check_and_alert(self, data: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Evaluate latest sensor data against alert thresholds.
        Each condition fires independently — one alert will not suppress
        another during its cooldown window.

        Parameters
        ----------
        data : dict
            Latest reading from SensorManager.get_latest()

        Returns
        -------
        (alert_active, alert_reasons)
            alert_active  : bool — True if any threshold was exceeded
            alert_reasons : list[str] — human-readable descriptions
        """
        reasons = []

        # ── SpO2 (highest priority — check first) ─────────────────────────
        spo2 = data.get("spo2_percent")
        spo2_valid = data.get("spo2_valid", False)
        spo2_low = bool(
            spo2 is not None
            and spo2_valid
            and config.ALERT_SPO2_LOW
            and spo2 < config.ALERT_SPO2_LOW
        )
        if spo2_low:
            reasons.append(f"SPO2_LOW:{spo2:.1f}%<{config.ALERT_SPO2_LOW}")
            if getattr(config, "BUZZER_BEEP_ON_SPO2", True) and self._record_hit("SPO2"):
                self._maybe_beep(
                    "SPO2",
                    durations=[config.BUZZER_SHORT_BEEP_S] * 3,
                    gap=0.1,
                )
        else:
            self._clear_hit("SPO2")

        # ── Heart Rate ────────────────────────────────────────────────────
        hr = data.get("heart_rate_bpm")
        hr_valid = data.get("hr_valid", False)
        hr_high = bool(
            hr is not None
            and hr_valid
            and config.ALERT_HR_HIGH
            and hr > config.ALERT_HR_HIGH
        )
        if hr_high:
            reasons.append(f"HR_HIGH:{hr:.0f}bpm>{config.ALERT_HR_HIGH}")
            if getattr(config, "BUZZER_BEEP_ON_HR_HIGH", False) and self._record_hit("HR_HIGH"):
                self._maybe_beep(
                    "HR_HIGH",
                    durations=[config.BUZZER_SHORT_BEEP_S] * 2,
                    gap=0.08,   # fast gap — tachycardia urgency
                )
        else:
            self._clear_hit("HR_HIGH")

        hr_low = bool(
            hr is not None
            and hr_valid
            and config.ALERT_HR_LOW
            and hr < config.ALERT_HR_LOW
        )
        if hr_low:
            reasons.append(f"HR_LOW:{hr:.0f}bpm<{config.ALERT_HR_LOW}")
            if getattr(config, "BUZZER_BEEP_ON_HR_LOW", False) and self._record_hit("HR_LOW"):
                self._maybe_beep(
                    "HR_LOW",
                    durations=[config.BUZZER_SHORT_BEEP_S] * 2,
                    gap=0.40,   # slow gap — bradycardia rhythm
                )
        else:
            self._clear_hit("HR_LOW")

        # ── GSR ───────────────────────────────────────────────────────────
        gsr = data.get("gsr_conductance_us")
        gsr_high = bool(
            gsr is not None
            and config.ALERT_GSR_HIGH_US
            and gsr > config.ALERT_GSR_HIGH_US
        )
        if gsr_high:
            reasons.append(f"GSR_HIGH:{gsr:.2f}uS>{config.ALERT_GSR_HIGH_US}")
            if getattr(config, "BUZZER_BEEP_ON_GSR", False) and self._record_hit("GSR"):
                self._maybe_beep(
                    "GSR",
                    durations=[config.BUZZER_LONG_BEEP_S],
                    gap=0,
                )
        else:
            self._clear_hit("GSR")

        # ── Sensor disconnect / hardware error ────────────────────────────
        sensor_error = data.get("sensor_error")
        if sensor_error:
            reasons.append(f"SENSOR_ERROR:{sensor_error}")
            if (
                getattr(config, "BUZZER_BEEP_ON_SENSOR_ERROR", True)
                and self._record_hit("SENSOR_ERROR")
            ):
                # 1 long + 1 short: clearly distinguishable from other patterns
                self._maybe_beep(
                    "SENSOR_ERROR",
                    durations=[config.BUZZER_LONG_BEEP_S, config.BUZZER_SHORT_BEEP_S],
                    gap=0.15,
                )
        else:
            self._clear_hit("SENSOR_ERROR")

        alert_active = len(reasons) > 0
        return alert_active, reasons

    def _record_hit(self, key: str) -> bool:
        """Increment condition streak and return True when debounce threshold is reached."""
        with self._lock:
            hits = self._condition_hits.get(key, 0) + 1
            self._condition_hits[key] = hits
        return hits >= self._min_consecutive_hits

    def _clear_hit(self, key: str):
        """Reset condition streak when condition is no longer present."""
        with self._lock:
            if self._condition_hits.get(key, 0):
                self._condition_hits[key] = 0

    # ── Beep logic ────────────────────────────────────────────────────────

    def _maybe_beep(self, key: str, durations: list[float], gap: float):
        """
        Fire beep sequence only if this condition's cooldown has expired.
        Each condition key has an independent timer — SPO2 cooldown does
        not block HR_HIGH from firing at the same time.

        Parameters
        ----------
        key       : condition name — one of SPO2 / HR_HIGH / HR_LOW / GSR / SENSOR_ERROR
        durations : list of on-times (seconds) for each beep in the sequence
        gap       : silence between beeps (seconds)
        """
        # Skip scheduling when buzzer is disabled or GPIO is not initialised.
        if not self._enabled or self._gpio_handle is None:
            return

        now = time.monotonic()
        with self._lock:
            if (now - self._last_alert_time.get(key, 0.0)) < config.BUZZER_COOLDOWN_S:
                return  # this condition is still in cooldown
            self._last_alert_time[key] = now

        logger.warning("ALERT [%s]: %d beep(s)", key, len(durations))
        self._beep_sequence(durations, gap)

    def _beep_sequence(self, durations: list[float], gap: float):
        """Sound the buzzer with a variable-length sequence in a background thread."""
        if not durations:
            return

        safe_gap = max(0.0, float(gap))

        def _play():
            for i, on_time in enumerate(durations):
                safe_on_time = max(0.0, float(on_time))
                self._set_pin(True)
                time.sleep(safe_on_time)
                self._set_pin(False)
                if safe_gap > 0 and i < len(durations) - 1:
                    time.sleep(safe_gap)

        threading.Thread(target=_play, daemon=True).start()

    def _set_pin(self, state: bool):
        """Set buzzer GPIO pin HIGH (on) or LOW (off)."""
        if self._gpio_handle is None:
            return
        try:
            import lgpio
            lgpio.gpio_write(self._gpio_handle, self._pin, 1 if state else 0)
            self._active = state
        except Exception as exc:
            logger.error("Buzzer GPIO write failed: %s", exc)

    # ── Manual test ───────────────────────────────────────────────────────

    def test_beep(self):
        """Two short beeps — call after setup() to verify buzzer works."""
        logger.info("Buzzer test beep...")
        self._beep_sequence([0.05, 0.05], gap=0.05)
