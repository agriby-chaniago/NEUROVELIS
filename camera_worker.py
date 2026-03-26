from picamera2 import Picamera2
import cv2
import sys


picam2 = Picamera2()
config = picam2.create_video_configuration(
    main={"size": (1280, 720), "format": "RGB888"}
)
picam2.configure(config)
picam2.start()

try:
    while True:
        frame = picam2.capture_array()
        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
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
