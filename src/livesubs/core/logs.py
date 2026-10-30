"""Per-process log files in ``paths.log_dir()`` (ui.log, engine.log, device.log)."""

from __future__ import annotations

import logging
import logging.handlers

from livesubs.core import paths


def setup_file_logging(file_name: str) -> None:
    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / file_name, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
