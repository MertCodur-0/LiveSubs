"""Download the optional PP-OCR line recogniser into ``src/livesubs/assets/ocr/``.

The model (PaddleOCR PP-OCRv5 English mobile recogniser, Apache-2.0, converted to ONNX by the
RapidOCR project) is not kept in the repository; the build and developers fetch it once. Without
it (or without ``onnxruntime``) LiveSubs reads with Windows OCR only.

    .venv\\Scripts\\python.exe scripts\\get_ocr_model.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

from livesubs.engine.ocr.ppocr import MODEL_NAME, MODEL_SHA256, MODEL_URL

TARGET = Path(__file__).resolve().parent.parent / "src" / "livesubs" / "assets" / "ocr"


def main() -> None:
    path = TARGET / MODEL_NAME
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256:
        print(f"{path} is up to date")
        return
    TARGET.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != MODEL_SHA256:
        sys.exit(f"checksum mismatch for {MODEL_URL}: {digest}")
    path.write_bytes(data)
    print(f"saved {path} ({len(data) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
