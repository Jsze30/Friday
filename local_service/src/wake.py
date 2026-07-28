from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

from .config import settings
from .events import bus

log = logging.getLogger("friday.wake")

SAMPLE_RATE = 16000
BLOCKSIZE = 1280  # 80 ms — openWakeWord's expected frame size


class WakeDetector:
    """Runs openWakeWord on a background thread reading from sounddevice.

    Paused state stops scoring audio (mic stream stays open to avoid
    re-acquiring the device) — Swift pauses while LiveKit owns the mic.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._last_fire_ms = 0.0

        model_ref = settings.resolved_wake_model()
        if ("/" in model_ref) and not Path(model_ref).exists():
            raise FileNotFoundError(f"Wake model not found: {model_ref}")
        log.info("loading openwakeword model: %s", model_ref)
        # Explicit onnx: tflite-runtime has no Apple Silicon wheels, and letting
        # openWakeWord discover that itself logs a warning on every start.
        self._model = Model(wakeword_models=[model_ref], inference_framework="onnx")

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        # Clear buffered audio features before unpausing so speech from the
        # just-ended turn can't score against the wake model.
        self._model.reset()
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def _run(self) -> None:
        try:
            with sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCKSIZE,
                dtype="int16",
                channels=1,
            ) as stream:
                log.info("wake detector listening")
                while not self._stop.is_set():
                    data, _ = stream.read(BLOCKSIZE)
                    if self._paused.is_set():
                        continue
                    frame = np.frombuffer(bytes(data), dtype=np.int16)
                    self._maybe_fire(self._model.predict(frame))
        except Exception:
            log.exception("wake detector crashed")

    def _maybe_fire(self, scores: dict[str, float]) -> None:
        if not scores:
            return
        name, score = max(scores.items(), key=lambda kv: kv[1])
        if score < settings.wake_threshold:
            return

        now_ms = time.monotonic() * 1000
        if now_ms - self._last_fire_ms < settings.wake_debounce_ms:
            return
        self._last_fire_ms = now_ms

        event = {
            "type": "wake_detected",
            "phrase": name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "confidence": float(score),
        }
        log.info("wake fired: model=%s score=%.3f", name, score)
        self._loop.call_soon_threadsafe(bus.publish, event)
