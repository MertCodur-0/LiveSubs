"""LiveSubs — real-time, fully local game subtitle translation for Windows."""

import os

# OpenCV reads this once, when cv2 is imported: with hardware transforms on, opening a capture
# card through Media Foundation took ~26 s instead of ~0.3 s (measured on a USB UVC card).
# Set here, before any module imports cv2; child processes inherit it.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

__version__ = "0.6.0"
APP_NAME = "LiveSubs"
