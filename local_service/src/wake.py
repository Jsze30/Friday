from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from openwakeword.model import Model

from .config import settings

log = logging.getLogger("friday.wake")

SAMPLE_RATE = 16_000
BLOCK_SAMPLES = 1_280
BLOCK_BYTES = BLOCK_SAMPLES * np.dtype(np.int16).itemsize


class WakeDetector:
    """Score echo-cancelled PCM supplied by the Mac app with openWakeWord."""

    def __init__(self) -> None:
        model_ref = settings.resolved_wake_model()
        if "/" in model_ref and not Path(model_ref).exists():
            raise FileNotFoundError(f"Wake model not found: {model_ref}")

        log.info("loading openWakeWord model: %s", model_ref)
        self._model = Model(
            wakeword_models=[model_ref],
            inference_framework="onnx",
        )
        self._lock = threading.Lock()
        self._paused = False
        self._last_fire_ms = 0.0

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def resume(self) -> None:
        with self._lock:
            self._model.reset()
            self._paused = False

    def process_pcm(self, data: bytes) -> dict[str, Any] | None:
        if len(data) != BLOCK_BYTES:
            log.warning(
                "ignoring wake PCM block with %d bytes; expected %d",
                len(data),
                BLOCK_BYTES,
            )
            return None

        with self._lock:
            if self._paused:
                return None
            frame = np.frombuffer(data, dtype=np.int16)
            scores = self._model.predict(frame)
            if not scores:
                return None

            name, score = max(scores.items(), key=lambda item: item[1])
            if score < settings.wake_threshold:
                return None

            now_ms = time.monotonic() * 1_000
            if now_ms - self._last_fire_ms < settings.wake_debounce_ms:
                return None

            self._last_fire_ms = now_ms
            self._paused = True

        log.info("wake fired: model=%s score=%.3f", name, score)
        return {
            "type": "wake_detected",
            "phrase": name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "confidence": float(score),
        }
