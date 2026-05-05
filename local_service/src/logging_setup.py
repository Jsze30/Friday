from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / "Library" / "Logs" / "Friday"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_DIR / "local_service.log",
        maxBytes=2_000_000,
        backupCount=3,
    )
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s :: %(message)s"
    )
    handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    if os.isatty(2):
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
