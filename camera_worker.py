from picamera2 import Picamera2
import cv2
import sys

import config


width = int(getattr(config, "CAMERA_STREAM_WIDTH", 640))
height = int(getattr(config, "CAMERA_STREAM_HEIGHT", 360))
fps = int(max(1, getattr(config, "CAMERA_FRAMERATE", 30)))
frame_us = int(1_000_000 / fps)
jpeg_quality = int(getattr(config, "CAMERA_JPEG_QUALITY", 80))
swap_rb = bool(getattr(config, "CAMERA_SWAP_RB", False))
rotation = int(getattr(config, "CAMERA_ROTATION", 0))

picam2 = Picamera2(int(getattr(config, "CAMERA_LIBCAMERA_INDEX", 0)))
video_config = picam2.create_video_configuration(
    main={"size": (width, height), "format": "RGB888"},
    controls={
        "FrameDurationLimits": (frame_us, frame_us),
        "AwbEnable": True,
        "AeEnable": True,
    },
)
picam2.configure(video_config)
picam2.start()

# Some camera pipelines ignore FrameDurationLimits from initial config.
try:
    picam2.set_controls({"FrameDurationLimits": (frame_us, frame_us)})
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
finally:
    try:
        picam2.stop()
    finally:
        picam2.close()
