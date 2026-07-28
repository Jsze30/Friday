"""Live wake-word score monitor - for choosing FRIDAY_WAKE_THRESHOLD.

Prints every frame that scores above NOTABLE, so both real detections and
near-misses are visible. Quit the Friday app first; it owns the mic.

    cd local_service
    uv run python scripts/wake_monitor.py                 # uses settings from .env
    uv run python scripts/wake_monitor.py hey_jarvis      # override the model

Two things to measure:
  1. Say the wake phrase ~10x, varying distance and volume. Note the LOWEST
     score that still fired. Your threshold must sit below that.
  2. Then talk normally, play a video, have a call - for a few minutes.
     Note the HIGHEST score reached without you saying the phrase. Your
     threshold must sit above that.
Set FRIDAY_WAKE_THRESHOLD between those two numbers.
"""

from __future__ import annotations

import sys
import time
from collections import Counter

import numpy as np
import sounddevice as sd
from openwakeword.model import Model

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from src.config import settings  # noqa: E402

SAMPLE_RATE = 16000
BLOCKSIZE = 1280  # 80 ms, openWakeWord's expected frame size
NOTABLE = 0.05  # print anything above this, so near-misses are visible
HEARTBEAT_S = 15.0


def main() -> int:
    model_ref = sys.argv[1] if len(sys.argv) > 1 else settings.resolved_wake_model()
    threshold = settings.wake_threshold

    print(f"model     : {model_ref}")
    print(f"threshold : {threshold}   (FRIDAY_WAKE_THRESHOLD)")
    print(f"device    : {sd.query_devices(kind='input')['name']}")
    print("\nlistening - ctrl-C to stop\n")

    model = Model(wakeword_models=[model_ref], inference_framework="onnx")

    peak_overall = 0.0
    peak_since_hit = 0.0
    fires = Counter()
    last_beat = time.monotonic()
    started = time.monotonic()

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, blocksize=BLOCKSIZE, dtype="int16", channels=1
    ) as stream:
        try:
            while True:
                data, _ = stream.read(BLOCKSIZE)
                frame = np.frombuffer(bytes(data), dtype=np.int16)
                scores = model.predict(frame)
                name, score = max(scores.items(), key=lambda kv: kv[1])

                peak_overall = max(peak_overall, score)
                peak_since_hit = max(peak_since_hit, score)

                if score >= threshold:
                    fires[name] += 1
                    print(f"  FIRE     {name:<16} {score:.3f}   (#{fires[name]})")
                    peak_since_hit = 0.0
                elif score >= NOTABLE:
                    bar = "#" * int(score * 30)
                    print(f"  .        {name:<16} {score:.3f}   {bar}")

                now = time.monotonic()
                if now - last_beat >= HEARTBEAT_S:
                    last_beat = now
                    print(
                        f"  [{now - started:5.0f}s] alive - "
                        f"peak since last fire {peak_since_hit:.3f}, "
                        f"peak overall {peak_overall:.3f}"
                    )
        except KeyboardInterrupt:
            pass

    total = sum(fires.values())
    print(f"\n\nstopped after {time.monotonic() - started:.0f}s")
    print(f"  detections   : {total} {dict(fires) if total else ''}")
    print(f"  peak score   : {peak_overall:.3f}")
    print("\nIf real phrases scored ~0.9 and background noise stayed under ~0.1,")
    print("the default threshold of 0.5 is fine. Narrow the gap only if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
