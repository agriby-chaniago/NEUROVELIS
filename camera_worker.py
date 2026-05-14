import sys
import time
import traceback

import config


def _import_camera_stack():
    """Import picamera2/cv2 with recovery for broken /usr/local libcamera shadowing."""
    try:
        from picamera2 import Picamera2 as _Picamera2
        import cv2 as _cv2
        return _Picamera2, _cv2
    except ModuleNotFoundError as exc:
        # Common on Raspberry Pi when stale /usr/local Python package shadows
        # apt-managed python3-libcamera from /usr/lib/python3/dist-packages.
        if exc.name != "libcamera._libcamera":
            raise

        bad_paths = [
            p for p in list(sys.path)
            if p.startswith("/usr/local/lib/python")
            and (p.endswith("site-packages") or p.endswith("dist-packages"))
        ]
        if not bad_paths:
            raise

        for p in bad_paths:
            try:
                sys.path.remove(p)
            except ValueError:
                pass

        # Clear possibly half-imported modules before retrying.
        for mod in ("libcamera", "picamera2"):
            sys.modules.pop(mod, None)

        from picamera2 import Picamera2 as _Picamera2
        import cv2 as _cv2
        return _Picamera2, _cv2


Picamera2, cv2 = _import_camera_stack()


capture_width = int(max(1, getattr(config, "CAMERA_WIDTH", 1920)))
capture_height = int(max(1, getattr(config, "CAMERA_HEIGHT", 1080)))
stream_width = int(max(1, getattr(config, "CAMERA_STREAM_WIDTH", capture_width)))
stream_height = int(max(1, getattr(config, "CAMERA_STREAM_HEIGHT", capture_height)))
fps = int(max(1, getattr(config, "CAMERA_FRAMERATE", 30)))
frame_us = int(1_000_000 / fps)
jpeg_quality = int(getattr(config, "CAMERA_JPEG_QUALITY", 80))
swap_rb = bool(getattr(config, "CAMERA_SWAP_RB", False))
rotation = int(getattr(config, "CAMERA_ROTATION", 0))
autofocus = bool(getattr(config, "CAMERA_AUTOFOCUS", False))
sharpness = float(getattr(config, "CAMERA_SHARPNESS", 1.0))
brightness = float(getattr(config, "CAMERA_BRIGHTNESS", 0.0))
mirror_horizontal = bool(getattr(config, "CAMERA_MIRROR_HORIZONTAL", True))
af_refocus_interval_s = float(getattr(config, "CAMERA_AF_REFOCUS_INTERVAL_S", 2.0))
if af_refocus_interval_s < 0.0:
    af_refocus_interval_s = 0.0
fixed_exposure_us = int(getattr(config, "CAMERA_FIXED_EXPOSURE_US", 0))
analogue_gain     = float(getattr(config, "CAMERA_ANALOGUE_GAIN", 2.0))
noise_reduction   = int(getattr(config, "CAMERA_NOISE_REDUCTION_MODE", 1))


def _camera_control_names(cam):
    """Return camera control names if available (best-effort)."""
    try:
        controls = getattr(cam, "camera_controls", None)
        if isinstance(controls, dict):
            return set(controls.keys())
    except Exception:
        pass
    return set()


def _set_control_if_supported(cam, control_names, key, value):
    """Set one control key if supported; return True when applied."""
    if control_names and key not in control_names:
        return False
    try:
        cam.set_controls({key: value})
        return True
    except Exception:
        return False

requested_idx = int(getattr(config, "CAMERA_LIBCAMERA_INDEX", 0))
available = Picamera2.global_camera_info()
if not available:
    raise RuntimeError(
        "No camera detected by libcamera. Run 'rpicam-hello --list-cameras' and "
        "check CSI ribbon/power/config first."
    )
if requested_idx < 0 or requested_idx >= len(available):
    raise RuntimeError(
        f"CAMERA_LIBCAMERA_INDEX={requested_idx} invalid; "
        f"detected cameras={len(available)}"
    )

picam2 = Picamera2(requested_idx)
_init_controls = {
    "FrameDurationLimits": (frame_us, frame_us),
    "AwbEnable":           True,
    "AeEnable":            fixed_exposure_us <= 0,
    "Sharpness":           sharpness,
    "Brightness":          brightness,
    "NoiseReductionMode":  noise_reduction,
}
if fixed_exposure_us > 0:
    _init_controls["ExposureTime"] = fixed_exposure_us
    _init_controls["AnalogueGain"] = analogue_gain
video_config = picam2.create_video_configuration(
    # Sensor mode is locked by main stream resolution.
    # Keep this at 1920x1080 for OV64A40 high-speed 60fps mode.
    main={"size": (capture_width, capture_height), "format": "RGB888"},
    controls=_init_controls,
)
picam2.configure(video_config)
picam2.start()

# Some camera pipelines ignore FrameDurationLimits from initial config.
try:
    picam2.set_controls({"FrameDurationLimits": (frame_us, frame_us)})
except Exception:
    pass

if autofocus:
    try:
        # Layered AF strategy:
        # 1) Prefer continuous AF.
        # 2) Fallback to auto AF + trigger if continuous mode unsupported.
        # 3) Periodically retrigger in auto mode to keep focus responsive.
        control_names = _camera_control_names(picam2)
        if control_names:
            af_caps = [
                key for key in ("AfMode", "AfTrigger", "AfSpeed", "LensPosition")
                if key in control_names
            ]
            sys.stderr.write(
                f"camera_worker: AF control capability={','.join(af_caps) or 'none'}\n"
            )
        af_mode_continuous = 2
        af_mode_auto = 1
        af_speed_normal = 1
        af_trigger_start = 0
        try:
            from libcamera import controls as _controls  # type: ignore
            af_mode_continuous = _controls.AfModeEnum.Continuous
            af_mode_auto = _controls.AfModeEnum.Auto
            af_speed_normal = _controls.AfSpeedEnum.Normal
            af_trigger_start = _controls.AfTriggerEnum.Start
        except Exception:
            pass

        af_mode_name = None
        if _set_control_if_supported(picam2, control_names, "AfMode", af_mode_continuous):
            af_mode_name = "continuous"
        elif _set_control_if_supported(picam2, control_names, "AfMode", af_mode_auto):
            af_mode_name = "auto"

        _set_control_if_supported(picam2, control_names, "AfSpeed", af_speed_normal)

        af_trigger_supported = _set_control_if_supported(
            picam2, control_names, "AfTrigger", af_trigger_start
        )

        if af_mode_name is None:
            # Last-resort fallback for modules exposing only manual lens control.
            lens_position = getattr(config, "CAMERA_LENS_POSITION", None)
            if lens_position is not None:
                manual_ok = _set_control_if_supported(
                    picam2,
                    control_names,
                    "LensPosition",
                    float(lens_position),
                )
                if manual_ok:
                    sys.stderr.write(
                        f"camera_worker: manual LensPosition={float(lens_position):.2f}\n"
                    )
            else:
                sys.stderr.write(
                    "camera_worker: autofocus unavailable (no AfMode support)\n"
                )
        else:
            sys.stderr.write(
                f"camera_worker: autofocus mode active ({af_mode_name})\n"
            )

        af_periodic_trigger = (
            af_mode_name == "auto"
            and af_trigger_supported
            and af_refocus_interval_s > 0.0
        )
        if af_periodic_trigger:
            sys.stderr.write(
                f"camera_worker: periodic AfTrigger every {af_refocus_interval_s:.1f}s\n"
            )
        next_af_trigger_t = time.monotonic() + af_refocus_interval_s
    except Exception:
        af_periodic_trigger = False
        next_af_trigger_t = 0.0
else:
    af_periodic_trigger = False
    next_af_trigger_t = 0.0

try:
    while True:
        if af_periodic_trigger and time.monotonic() >= next_af_trigger_t:
            try:
                picam2.set_controls({"AfTrigger": af_trigger_start})
            except Exception:
                af_periodic_trigger = False
            else:
                next_af_trigger_t = time.monotonic() + af_refocus_interval_s

        frame = picam2.capture_array()
        # cv2.imencode expects BGR input.
        if not swap_rb:
            frame = frame[:, :, ::-1]

        if (stream_width, stream_height) != (capture_width, capture_height):
            interpolation = cv2.INTER_AREA
            if stream_width > capture_width or stream_height > capture_height:
                interpolation = cv2.INTER_LINEAR
            frame = cv2.resize(
                frame,
                (stream_width, stream_height),
                interpolation=interpolation,
            )

        if rotation == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif rotation == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

        if mirror_horizontal:
            frame = cv2.flip(frame, 1)

        ok, jpg = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
        )
        if not ok:
            continue
        sys.stdout.buffer.write(len(jpg).to_bytes(4, "big"))
        sys.stdout.buffer.write(jpg.tobytes())
        sys.stdout.flush()
except KeyboardInterrupt:
    pass
except Exception:
    traceback.print_exc(file=sys.stderr)
    raise
finally:
    try:
        picam2.stop()
    finally:
        picam2.close()
