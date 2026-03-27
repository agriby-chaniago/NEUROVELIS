from picamera2 import Picamera2
import cv2
import sys
import traceback

import config


width = int(getattr(config, "CAMERA_STREAM_WIDTH", 640))
height = int(getattr(config, "CAMERA_STREAM_HEIGHT", 360))
fps = int(max(1, getattr(config, "CAMERA_FRAMERATE", 30)))
frame_us = int(1_000_000 / fps)
jpeg_quality = int(getattr(config, "CAMERA_JPEG_QUALITY", 80))
swap_rb = bool(getattr(config, "CAMERA_SWAP_RB", False))
rotation = int(getattr(config, "CAMERA_ROTATION", 0))
autofocus = bool(getattr(config, "CAMERA_AUTOFOCUS", False))
sharpness = float(getattr(config, "CAMERA_SHARPNESS", 1.0))
brightness = float(getattr(config, "CAMERA_BRIGHTNESS", 0.0))
mirror_horizontal = bool(getattr(config, "CAMERA_MIRROR_HORIZONTAL", True))

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
video_config = picam2.create_video_configuration(
    main={"size": (width, height), "format": "RGB888"},
    controls={
        "FrameDurationLimits": (frame_us, frame_us),
        "AwbEnable": True,
        "AeEnable": True,
        "Sharpness": sharpness,
        "Brightness": brightness,
    },
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
        # Arducam 64MP AF: 2 = continuous autofocus, 1 = normal AF speed.
        picam2.set_controls({"AfMode": 2, "AfSpeed": 1})
    except Exception:
        pass

try:
    while True:
        frame = picam2.capture_array()
        # cv2.imencode expects BGR input.
        if not swap_rb:
            frame = frame[:, :, ::-1]

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
