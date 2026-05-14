"""
sensors/camera_reader.py  –  Arducam / Raspberry Pi Camera reader.

Backend priority
----------------
1. picamera2  – native CSI ribbon cable / Arducam CSI (RPi OS Bullseye+)
2. OpenCV     – USB Arducam or any V4L2 /dev/videoX device

Pipeline (picamera2)
--------------------
  ISP hardware
  ├── main  (1920×1080 RGB888)  →  /camera/snapshot  (high-res)
  └── lores (640×360  MJPEG)    →  MJPEG live stream  (hardware-encoded, ~0 CPU)

If the Pi ISP does not support MJPEG lores, falls back to YUV420 + cv2 encode.

Thread-safe: capture thread writes frames; MJPEG generator blocks on
threading.Condition until the next frame arrives (zero polling latency).
"""

import io
import logging
import os
import queue
import select
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

_RESTART_DELAY_S = 3.0


class CameraReader:
    """
    Background-thread camera reader.

    Call start() once; the thread captures frames until stop() is called.
    get_frame() returns the latest JPEG as bytes, or None if not yet ready.
    """

    def __init__(self):
        # threading.Condition wraps a lock; notify_all() wakes the MJPEG
        # generator the instant a new frame arrives — no polling needed.
        self._cond = threading.Condition()
        self._frame: Optional[bytes] = None
        self._frame_seq: int = 0   # increments every new frame
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._backend: Optional[str] = None   # "picamera2" | "worker_subprocess"
        self._error: Optional[str] = None     # last fatal error message
        self._cam = None   # picamera2 Picamera2 instance (set after start)
        self._worker_proc: Optional[subprocess.Popen] = None
        # Rolling FPS: stores monotonic timestamps of last 60 encoded frames.
        # fps = (n-1) / (ts[-1] - ts[0])  — accurate even with variable cadence.
        self._fps_timestamps: deque = deque(maxlen=60)
        # Signalled by encode/worker threads on each new frame.
        # Used by wait_new_frame() for event-driven inference (avoids polling).
        self._new_frame_event: threading.Event = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Initialise camera hardware and start the capture thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="camera",
            daemon=True,
        )
        self._thread.start()
        logger.info("CameraReader: capture thread started")

    def stop(self) -> None:
        """Signal the capture thread to stop and wait for it."""
        self._running = False

        # Force worker shutdown first so blocking pipe reads return promptly.
        proc = self._worker_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()

        with self._cond:
            self._cond.notify_all()   # unblock any waiting get_new_frame()
        if self._thread is not None:
            self._thread.join(timeout=6.0)
        logger.info("CameraReader: stopped")

    # ── Frame access ─────────────────────────────────────────────────────

    def get_frame(self) -> Optional[bytes]:
        """Return latest JPEG frame bytes, or None if not yet available."""
        with self._cond:
            return self._frame

    def get_new_frame(self, timeout: float = 0.25) -> Optional[bytes]:
        """
        Block until a NEW frame is captured (or timeout).
        Used by the MJPEG generator to yield frames with zero polling delay.
        """
        with self._cond:
            seq = self._frame_seq
            self._cond.wait_for(
                lambda: self._frame_seq != seq or not self._running,
                timeout=timeout,
            )
            return self._frame

    def wait_new_frame(self, last_seq: int, timeout: float = 0.11) -> tuple[int, Optional[bytes]]:
        """
        Block until a frame newer than last_seq arrives, or timeout.

        Returns (current_seq, frame_bytes) when a new frame is available.
        Returns (last_seq, None) on timeout with no new frame.

        Seq-based design: safe against the clear/get race that a bare
        threading.Event would have — the seq counter validates that the frame
        returned is actually newer than the last one the caller processed.
        Used by the inference thread for event-driven frame consumption.
        """
        self._new_frame_event.wait(timeout=timeout)
        self._new_frame_event.clear()
        current_seq = self._frame_seq
        if current_seq != last_seq:
            return current_seq, self.get_frame()
        return last_seq, None

    def capture_snapshot(self) -> Optional[bytes]:
        """
        Capture a single high-res JPEG from the main stream (full 1920×1080).
        Blocks briefly (~100ms). Returns None if camera not ready.
        Used by /camera/snapshot route.
        """
        cam = self._cam
        if cam is None:
            return self.get_frame()   # fallback to latest lores frame
        try:
            import cv2 as _cv2
            array = cam.capture_array("main")
            if getattr(config, "CAMERA_SWAP_RB", False):
                array = array[:, :, ::-1].copy()
            bgr = array[:, :, ::-1]
            _, buf = _cv2.imencode(
                ".jpg", bgr,
                [_cv2.IMWRITE_JPEG_QUALITY, 92],   # higher quality for snapshots
            )
            return buf.tobytes()
        except Exception:
            return self.get_frame()

    @property
    def fps(self) -> Optional[float]:
        """Rolling FPS computed over the last 60 encoded frames.

        Returns None until at least 2 frames have been captured.
        """
        with self._cond:
            ts = list(self._fps_timestamps)
        if len(ts) < 2:
            return None
        elapsed = ts[-1] - ts[0]
        if elapsed <= 0:
            return None
        return (len(ts) - 1) / elapsed

    @property
    def backend(self) -> Optional[str]:
        """Which backend is active: 'picamera2', 'worker_subprocess', or None."""
        return self._backend

    @property
    def ready(self) -> bool:
        """True once the first frame has been captured."""
        with self._cond:
            return self._frame is not None

    def health(self) -> dict:
        fps = self.fps
        ready = self.ready
        return {
            "sensor":  "camera",
            "ok":      self._backend is not None and self._error is None and ready,
            "ready":   ready,
            "backend": self._backend,
            "fps":     round(fps, 1) if fps is not None else None,
            "error":   self._error,
        }

    # ── Main capture loop ─────────────────────────────────────────────────

    def _capture_loop(self):
        """Run camera capture through the worker subprocess with restart-on-fail."""
        while self._running:
            try:
                self._loop_opencv()
            except Exception as exc:
                if self._running:
                    self._error = str(exc)
                    logger.error("CameraReader: worker subprocess failed: %s", exc)

            if not self._running:
                break

            logger.warning(
                "CameraReader: restarting worker subprocess in %.1fs",
                _RESTART_DELAY_S,
            )
            time.sleep(_RESTART_DELAY_S)

    # ── picamera2 backend ─────────────────────────────────────────────────

    def _loop_picamera2(self):
        from picamera2 import Picamera2  # type: ignore

        # Check that at least one camera is visible to libcamera before opening
        available = Picamera2.global_camera_info()
        if not available:
            raise RuntimeError(
                "No cameras detected by libcamera. "
                "Run: rpicam-hello --list-cameras\n"
                "If empty, enable the camera overlay:\n"
                "  sudo raspi-config → Interface Options → Camera\n"
                "  OR add 'camera_auto_detect=1' to /boot/firmware/config.txt "
                "then reboot."
            )
        logger.info("CameraReader: libcamera detected %d camera(s): %s",
                    len(available), [c.get('Model', '?') for c in available])

        requested_idx = int(getattr(config, "CAMERA_LIBCAMERA_INDEX", 0))
        if 0 <= requested_idx < len(available):
            cam = Picamera2(requested_idx)
            logger.info("CameraReader: using libcamera index %d", requested_idx)
        else:
            cam = Picamera2(0)
            logger.warning(
                "CameraReader: CAMERA_LIBCAMERA_INDEX=%d invalid, fallback to index 0",
                requested_idx,
            )

        # ── Dual-stream config ────────────────────────────────────────
        # main  = full 1920×1080 — used for /camera/snapshot (high quality)
        # lores = small stream  — used for MJPEG live view (ISP hardware scale)
        # The ISP downscales lores for FREE — no extra CPU cost.
        sw = getattr(config, "CAMERA_STREAM_WIDTH",  640)
        sh = getattr(config, "CAMERA_STREAM_HEIGHT", 360)

        frame_us = int(1_000_000 / max(1, config.CAMERA_FRAMERATE))

        # PiSP (Pi 5) silently adjusts unsupported formats ("configuration has
        # been adjusted" in logs). Use RGB888 for both streams — this is what
        # PiSP actually delivers, and CAMERA_SWAP_RB=True corrects the channel
        # order (OV64A40 via PiSP sends BGR data in RGB888 containers).
        lores_fmt = "RGB888"

        # FrameDurationLimits = (min_duration_us, max_duration_us).
        # Setting both to frame_us LOCKS the ISP to exactly CAMERA_FRAMERATE fps.
        # The AE algorithm can no longer slow the frame rate to gain extra
        # exposure; it must compensate using analogue gain only.
        # Trade-off: consistent fps for all lighting vs. slightly more grain in
        # dim conditions — acceptable for temporal micro-expression datasets.
        # Force the sensor into a matching raw readout mode.
        # OV64A40 only has one mode near high-fps: 1920x1080 @ 45.65fps max.
        # FrameDurationLimits locks the ISP to exactly CAMERA_FRAMERATE
        # (currently configured as 30fps in config.py).
        video_cfg = cam.create_video_configuration(
            main={
                "size":   (config.CAMERA_WIDTH, config.CAMERA_HEIGHT),
                "format": "RGB888",
            },
            lores={
                "size":   (sw, sh),
                "format": lores_fmt,
            },
            controls={
                "FrameDurationLimits": (frame_us, frame_us),
                "AwbEnable":  True,
                "AeEnable":   True,
                "Sharpness":  getattr(config, "CAMERA_SHARPNESS", 2.0),
                "Brightness": getattr(config, "CAMERA_BRIGHTNESS", 0.0),
            },
            buffer_count=8,
        )

        # Apply rotation if configured
        rotation = getattr(config, "CAMERA_ROTATION", 0)
        if rotation != 0:
            try:
                from libcamera import Transform  # type: ignore
                transform_map = {
                    90:  Transform(rotation=90),
                    180: Transform(hflip=1, vflip=1),
                    270: Transform(rotation=270),
                }
                if rotation in transform_map:
                    video_cfg["transform"] = transform_map[rotation]
            except ImportError:
                pass

        cam.configure(video_cfg)

        cam.start()
        self._cam = cam   # expose to capture_snapshot()

        # Re-apply FrameDurationLimits post-start to ensure it sticks.
        try:
            cam.set_controls({"FrameDurationLimits": (frame_us, frame_us)})
        except Exception as fdl_exc:
            logger.warning("CameraReader: FrameDurationLimits post-start failed: %s", fdl_exc)

        # ── Autofocus (Arducam 64MP AF / OV64A40) ────────────────────────
        af_periodic_trigger = False
        af_trigger_start = 0
        af_refocus_interval_s = float(
            getattr(config, "CAMERA_AF_REFOCUS_INTERVAL_S", 2.0)
        )
        if af_refocus_interval_s < 0.0:
            af_refocus_interval_s = 0.0
        af_next_trigger_t = 0.0

        if getattr(config, "CAMERA_AUTOFOCUS", False):
            control_names = set()
            try:
                controls = getattr(cam, "camera_controls", None)
                if isinstance(controls, dict):
                    control_names = set(controls.keys())
            except Exception:
                pass

            def _set_control_if_supported(key: str, value) -> bool:
                if control_names and key not in control_names:
                    return False
                try:
                    cam.set_controls({key: value})
                    return True
                except Exception:
                    return False

            af_mode_continuous = 2
            af_mode_auto = 1
            af_speed_normal = 1
            try:
                from libcamera import controls as _controls  # type: ignore
                af_mode_continuous = _controls.AfModeEnum.Continuous
                af_mode_auto = _controls.AfModeEnum.Auto
                af_speed_normal = _controls.AfSpeedEnum.Normal
                af_trigger_start = _controls.AfTriggerEnum.Start
            except Exception:
                pass

            try:
                af_mode_name = None
                if _set_control_if_supported("AfMode", af_mode_continuous):
                    af_mode_name = "continuous"
                elif _set_control_if_supported("AfMode", af_mode_auto):
                    af_mode_name = "auto"

                if not _set_control_if_supported("AfSpeed", af_speed_normal):
                    logger.debug("CameraReader: AfSpeed not applied")

                af_trigger_supported = _set_control_if_supported(
                    "AfTrigger", af_trigger_start
                )

                if af_mode_name is None:
                    lens_position = getattr(config, "CAMERA_LENS_POSITION", None)
                    if lens_position is not None:
                        if _set_control_if_supported("LensPosition", float(lens_position)):
                            logger.info(
                                "CameraReader: manual LensPosition applied (%.2f)",
                                float(lens_position),
                            )
                    logger.warning("CameraReader: AF mode unsupported by camera driver")
                else:
                    logger.info("CameraReader: autofocus enabled (%s)", af_mode_name)

                af_periodic_trigger = (
                    af_mode_name == "auto"
                    and af_trigger_supported
                    and af_refocus_interval_s > 0.0
                )
                if af_periodic_trigger:
                    af_next_trigger_t = time.monotonic() + af_refocus_interval_s
            except Exception as af_exc:
                logger.warning("CameraReader: could not enable AF mode: %s", af_exc)

        # ── Dataset / fixed-exposure mode ─────────────────────────────────
        _fixed_exp  = getattr(config, "CAMERA_FIXED_EXPOSURE_US", 0)
        _fixed_gain = getattr(config, "CAMERA_ANALOGUE_GAIN", 0.0)
        if _fixed_exp > 0:
            try:
                _exp_controls = {"AeEnable": False, "ExposureTime": int(_fixed_exp)}
                if _fixed_gain > 0:
                    _exp_controls["AnalogueGain"] = float(_fixed_gain)
                cam.set_controls(_exp_controls)
                logger.info(
                    "CameraReader: fixed exposure mode — ExposureTime=%d µs, AnalogueGain=%s",
                    int(_fixed_exp), _fixed_gain if _fixed_gain > 0 else "auto",
                )
            except Exception as exp_exc:
                logger.warning("CameraReader: could not set fixed exposure: %s", exp_exc)
        else:
            logger.info("CameraReader: AE enabled (CAMERA_FIXED_EXPOSURE_US=0) — fps may drift")

        self._backend = "picamera2"
        self._error = None
        logger.info(
            "CameraReader: picamera2 started (%dx%d @ %d fps)",
            config.CAMERA_WIDTH, config.CAMERA_HEIGHT, config.CAMERA_FRAMERATE,
        )

        try:
            # Import cv2 — faster JPEG encoding. PIL+numpy used as fallback.
            try:
                import cv2 as _cv2
                _use_cv2 = True
                logger.info("CameraReader: using cv2 for JPEG encoding")
            except ImportError:
                _use_cv2 = False
                logger.info("CameraReader: cv2 not found, using PIL+numpy fallback")

            import numpy as _np
            from PIL import Image as _Image

            rotation = getattr(config, "CAMERA_ROTATION", 0)
            jpeg_q   = config.CAMERA_JPEG_QUALITY

            def _encode_frame(raw: "_np.ndarray") -> bytes:
                """Convert lores array to JPEG bytes with minimal copying."""
                arr = raw
                if arr.ndim == 3 and arr.shape[2] == 4:
                    arr = arr[:, :, :3]  # strip alpha

                swap_rb = getattr(config, "CAMERA_SWAP_RB", False)

                if _use_cv2:
                    # OV64A40 + PiSP delivers BGR data inside RGB888 container.
                    # cv2 also expects BGR input.
                    # When SWAP_RB=True: the two channel-flips (correct→RGB, then→BGR
                    # for cv2) cancel each other — raw array IS already BGR for cv2.
                    # Skip both flips and just make the array contiguous (one copy).
                    # When SWAP_RB=False: ISP delivers true RGB, flip once for cv2.
                    if swap_rb:
                        bgr = _np.ascontiguousarray(arr)          # already BGR
                    else:
                        bgr = _np.ascontiguousarray(arr[:, :, ::-1])  # RGB→BGR
                    if rotation == 90:
                        bgr = _cv2.rotate(bgr, _cv2.ROTATE_90_CLOCKWISE)
                    elif rotation == 180:
                        bgr = _cv2.rotate(bgr, _cv2.ROTATE_180)
                    elif rotation == 270:
                        bgr = _cv2.rotate(bgr, _cv2.ROTATE_90_COUNTERCLOCKWISE)
                    _, buf = _cv2.imencode(
                        ".jpg", bgr, [_cv2.IMWRITE_JPEG_QUALITY, jpeg_q]
                    )
                    return buf.tobytes()
                else:
                    # PIL expects RGB
                    if swap_rb:
                        img_arr = _np.ascontiguousarray(arr[:, :, ::-1])  # BGR→RGB
                    else:
                        img_arr = _np.ascontiguousarray(arr)               # already RGB
                    img = _Image.fromarray(img_arr, mode="RGB")
                    if rotation == 90:
                        img = img.rotate(-90, expand=True)
                    elif rotation == 180:
                        img = img.rotate(180, expand=True)
                    elif rotation == 270:
                        img = img.rotate(90, expand=True)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=jpeg_q)
                    return buf.getvalue()

            # ── Encode thread ───────────────────────────────────────────
            # Separating capture (ISP-bound) from encode (CPU-bound) lets the
            # ISP keep running full-speed even when encode takes >1 frame.
            # queue maxsize=2: absorbs brief encode spikes (e.g. GC pause)
            # without dropping. Capture thread always discards oldest when full
            # so live-view latency stays bounded at 2 frames (~44ms at 45fps).
            _enc_q: queue.Queue = queue.Queue(maxsize=2)

            def _encode_worker():
                while True:
                    raw = _enc_q.get()
                    if raw is None:   # poison pill — stop signal
                        break
                    try:
                        jpeg_bytes = _encode_frame(raw)
                        with self._cond:
                            self._frame = jpeg_bytes
                            self._frame_seq += 1
                            self._fps_timestamps.append(time.monotonic())
                            self._cond.notify_all()
                        self._new_frame_event.set()
                    except Exception as enc_exc:
                        logger.debug("CameraReader: encode error: %s", enc_exc)

            _enc_thread = threading.Thread(
                target=_encode_worker, name="camera-encode", daemon=True
            )
            _enc_thread.start()

            # FPS counters — logged every 5 seconds
            _cap_count  = 0
            _drop_count = 0
            _fps_t0     = time.monotonic()

            while self._running:
                if af_periodic_trigger and time.monotonic() >= af_next_trigger_t:
                    try:
                        cam.set_controls({"AfTrigger": af_trigger_start})
                    except Exception as af_tick_exc:
                        af_periodic_trigger = False
                        logger.debug("CameraReader: periodic AfTrigger failed: %s", af_tick_exc)
                    else:
                        af_next_trigger_t = time.monotonic() + af_refocus_interval_s

                raw = cam.capture_array("lores")   # BGR888 from ISP
                _cap_count += 1

                # Non-blocking put: drop stale frame if encode can't keep up
                try:
                    _enc_q.put_nowait(raw)
                except queue.Full:
                    try:
                        _enc_q.get_nowait()   # discard old frame
                    except queue.Empty:
                        pass
                    _enc_q.put_nowait(raw)
                    _drop_count += 1

                # Log actual FPS every 5 seconds
                _elapsed = time.monotonic() - _fps_t0
                if _elapsed >= 5.0:
                    _fps = _cap_count / _elapsed
                    logger.info(
                        "CameraReader: capture %.1f fps | dropped %d frames in %.0fs",
                        _fps, _drop_count, _elapsed,
                    )
                    _cap_count  = 0
                    _drop_count = 0
                    _fps_t0     = time.monotonic()

        finally:
            # Drain pending frames so the sentinel always fits (maxsize=2).
            # Discarding frames on shutdown is safe — they will never be displayed.
            while True:
                try:
                    _enc_q.get_nowait()
                except queue.Empty:
                    break
            try:
                _enc_q.put(None, timeout=2.0)  # blocking — sentinel must reach thread
            except queue.Full:
                logger.error("CameraReader: could not send encode-thread sentinel — thread may linger")
            self._cam = None
            cam.stop()
            cam.close()

    # ── OpenCV backend ────────────────────────────────────────────────────

    def _loop_opencv(self):
        worker_path = Path(__file__).resolve().parent.parent / "camera_worker.py"
        if not worker_path.exists():
            raise RuntimeError(f"Camera worker file not found: {worker_path}")

        worker_python_cfg = getattr(config, "CAMERA_WORKER_PYTHON", None)

        def _python_can_import_camera_stack(python_bin: str) -> bool:
            """True when interpreter can import mandatory camera worker deps."""
            try:
                probe = subprocess.run(
                    [python_bin, "-c", "import picamera2, cv2"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                )
                return probe.returncode == 0
            except Exception:
                return False

        worker_python: Optional[str] = None
        if worker_python_cfg:
            worker_python = str(worker_python_cfg).strip()
            if not worker_python:
                worker_python = None

        if worker_python is None:
            candidates = []
            if sys.executable:
                candidates.append(sys.executable)
            if "/usr/bin/python3" not in candidates:
                candidates.append("/usr/bin/python3")

            for candidate in candidates:
                if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
                    continue
                if _python_can_import_camera_stack(candidate):
                    worker_python = candidate
                    break

        if worker_python is None:
            worker_python = sys.executable
            logger.warning(
                "CameraReader: no interpreter with picamera2+cv2 detected; "
                "fallback to current interpreter (%s)",
                worker_python,
            )
        elif worker_python != sys.executable and not worker_python_cfg:
            logger.warning(
                "CameraReader: current interpreter (%s) lacks camera deps; "
                "using worker interpreter %s",
                sys.executable,
                worker_python,
            )

        proc = subprocess.Popen(
            [worker_python, "camera_worker.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(worker_path.parent),
            bufsize=0,
        )
        if proc.stdout is None:
            proc.terminate()
            raise RuntimeError("Camera worker started without stdout pipe")

        self._worker_proc = proc
        self._backend = "worker_subprocess"
        self._error = None
        logger.info(
            "CameraReader: worker subprocess started (%s via %s)",
            worker_path,
            worker_python,
        )

        frame_timeout_s = max(
            2.0,
            float(getattr(config, "CAMERA_WORKER_FRAME_TIMEOUT_S", 12.0)),
        )

        def _read_stderr_preview(limit: int = 2048, wait_s: float = 0.05) -> str:
            if proc.stderr is None:
                return ""
            try:
                ready, _, _ = select.select([proc.stderr], [], [], wait_s)
                if not ready:
                    return ""
                raw = proc.stderr.read(limit)
                return raw.decode("utf-8", errors="replace").strip()
            except Exception:
                return ""

        def _read_exact(size: int) -> Optional[bytes]:
            """Read exactly size bytes from worker stdout, or None on EOF."""
            chunks = []
            remaining = size
            deadline = time.monotonic() + frame_timeout_s
            while remaining > 0:
                wait_s = max(0.0, deadline - time.monotonic())
                if wait_s <= 0.0:
                    raise TimeoutError(f"CAMERA_WORKER_FRAME_TIMEOUT:{frame_timeout_s}s")
                ready, _, _ = select.select([proc.stdout], [], [], wait_s)
                if not ready:
                    raise TimeoutError(f"CAMERA_WORKER_FRAME_TIMEOUT:{frame_timeout_s}s")
                chunk = proc.stdout.read(remaining)
                if not chunk:
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        try:
            while self._running:
                size_bytes = _read_exact(4)
                if not size_bytes:
                    if self._running and proc.poll() is not None:
                        stderr_preview = _read_stderr_preview(wait_s=0.2)
                        code = proc.returncode
                        if stderr_preview:
                            self._error = (
                                f"CAMERA_WORKER_EXITED(code={code}):"
                                f"{stderr_preview[:1200]}"
                            )
                        else:
                            self._error = f"CAMERA_WORKER_EXITED(code={code})"
                        logger.error("CameraReader: %s", self._error)
                    break

                size = struct.unpack(">I", size_bytes)[0]
                if size <= 0 or size > 10_000_000:
                    logger.warning("CameraReader: invalid frame size from worker: %d", size)
                    continue

                jpg = _read_exact(size)
                if not jpg:
                    break

                with self._cond:
                    self._frame = jpg
                    self._frame_seq += 1
                    self._fps_timestamps.append(time.monotonic())
                    self._cond.notify_all()
                self._new_frame_event.set()
        except TimeoutError as exc:
            self._error = str(exc)
            logger.error("CameraReader: %s", self._error)
            stderr_preview = _read_stderr_preview()
            if stderr_preview:
                logger.error("CameraReader worker stderr: %s", stderr_preview[:240])
            raise
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._worker_proc = None
            self._backend = None
