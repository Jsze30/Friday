from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone

import sounddevice as sd
from vosk import KaldiRecognizer, Model, SetLogLevel

from .config import settings
from .events import bus

log = logging.getLogger("friday.wake")
SetLogLevel(-1)

SAMPLE_RATE = 16000
BLOCKSIZE = 4000  # 250 ms


class WakeDetector:
    """Runs Vosk on a background thread reading from sounddevice.

    Paused state stops processing audio (mic stream stays open to avoid
    re-acquiring the device) — Swift pauses while LiveKit owns the mic.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._last_fire_ms = 0.0

        model_path = settings.resolved_vosk_model_path()
        if not model_path.exists():
            raise FileNotFoundError(f"Vosk model not found: {model_path}")
        log.info("loading vosk model: %s", model_path)
        self._model = Model(str(model_path))

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
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def _run(self) -> None:
        grammar = json.dumps([settings.wake_phrase, "[unk]"])
        recognizer = KaldiRecognizer(self._model, SAMPLE_RATE, grammar)
        recognizer.SetWords(True)

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
                        recognizer.Reset()
                        continue
                    if recognizer.AcceptWaveform(bytes(data)):
                        result = json.loads(recognizer.Result())
                        self._maybe_fire(result, final=True)
                    else:
                        partial = json.loads(recognizer.PartialResult())
                        self._maybe_fire(partial, final=False)
        except Exception:
            log.exception("wake detector crashed")

    def _maybe_fire(self, result: dict, *, final: bool) -> None:
        text = (result.get("text") or result.get("partial") or "").strip().lower()
        if not text or settings.wake_phrase not in text.split():
            return

        now_ms = time.monotonic() * 1000
        if now_ms - self._last_fire_ms < settings.wake_debounce_ms:
            return
        self._last_fire_ms = now_ms

        confidence: float | None = None
        for w in result.get("result", []):
            if w.get("word") == settings.wake_phrase:
                confidence = float(w.get("conf", 0.0))
                break

        event = {
            "type": "wake_detected",
            "phrase": settings.wake_phrase,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "confidence": confidence,
        }
        log.info("wake fired: final=%s conf=%s text=%r", final, confidence, text)
        self._loop.call_soon_threadsafe(bus.publish, event)
