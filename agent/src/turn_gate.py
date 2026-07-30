"""Pre-roll audio plumbing for wake-word turns.

The Mac detects the wake word locally on an AEC-processed stream and keeps a
rolling window of recent audio. On wake it unmutes the mic immediately, sends
that window as a byte stream (PREROLL_TOPIC), and calls activate_turn. The
pieces here make sure the full sentence - including words spoken before
activation completed - reaches STT, in order:

- PreRollReceiver collects the byte stream into AudioFrames.
- PreRollAudioInput wraps RoomIO's audio input and yields pre-roll frames
  ahead of the live stream when a turn activates.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any

from livekit import rtc
from livekit.agents import utils
from livekit.agents.voice import io

logger = logging.getLogger("friday-agent")

PREROLL_TOPIC = "friday.wake-preroll"

# A pre-roll older than this is from an earlier wake and must not be
# prepended to the current turn.
_MAX_PREROLL_AGE_S = 3.0


class PreRollAudioInput(io.AudioInput):
    """Wraps RoomIO's audio input so a wake-word pre-roll can be prepended.

    Between turns the session disables audio input, which normally detaches
    the RoomIO stream and makes it drop incoming frames. This wrapper
    swallows the detach so frames published after the Mac unmutes (but
    before activate_turn lands) buffer in the source channel instead of
    being dropped. On activation the pre-roll is yielded first, then the
    buffered frames, then live audio - a seamless stream for STT.
    """

    def __init__(self, source: io.AudioInput, *, sample_rate: int) -> None:
        super().__init__(label="PreRollGate", source=source)
        self._sample_rate = sample_rate
        self._pending: deque[rtc.AudioFrame] = deque()

    def queue_preroll(self, frames: list[rtc.AudioFrame]) -> None:
        # STT requires a consistent sample rate across the stream, so the
        # 16kHz pre-roll is resampled to match the live RoomIO frames.
        resampler: rtc.AudioResampler | None = None
        for frame in frames:
            if frame.sample_rate != self._sample_rate:
                if resampler is None:
                    resampler = rtc.AudioResampler(frame.sample_rate, self._sample_rate)
                self._pending.extend(resampler.push(frame))
            else:
                self._pending.append(frame)
        if resampler is not None:
            self._pending.extend(resampler.flush())

    async def drain_stale(self) -> None:
        """Discard buffered frames. Called a beat after the turn ends: the
        Mac mutes right after return_to_sleep, so anything still in the
        channel is the tail of the previous turn and must not leak into the
        start of the next one."""
        self._pending.clear()
        while True:
            try:
                await asyncio.wait_for(self.source.__anext__(), timeout=0.05)
            except (TimeoutError, StopAsyncIteration):
                return

    async def __anext__(self) -> rtc.AudioFrame:
        if self._pending:
            return self._pending.popleft()
        return await self.source.__anext__()

    def on_detached(self) -> None:
        # Deliberately not propagated: the source must stay attached so
        # post-unmute frames buffer instead of being dropped.
        pass


class PreRollReceiver:
    """Receives pre-roll PCM byte streams sent by the Mac on wake."""

    def __init__(self, room: rtc.Room) -> None:
        self._room = room
        self._result: tuple[float, list[rtc.AudioFrame]] | None = None
        self._event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()

    def register(self) -> None:
        def _handler(reader: rtc.ByteStreamReader, participant_id: str) -> None:
            task = asyncio.create_task(self._read(reader, participant_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        self._room.register_byte_stream_handler(PREROLL_TOPIC, _handler)

    async def aclose(self) -> None:
        with contextlib.suppress(ValueError):
            self._room.unregister_byte_stream_handler(PREROLL_TOPIC)
        for task in self._tasks:
            task.cancel()

    async def take(self, timeout: float = 1.0) -> list[rtc.AudioFrame]:
        """Return the most recent fresh pre-roll, waiting briefly for one
        that is still in flight. Returns [] on timeout - the turn proceeds
        without pre-roll rather than failing."""
        if self._result is None:
            self._event.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._event.wait(), timeout)
        result, self._result = self._result, None
        if result is None:
            logger.warning("no pre-roll received before timeout")
            return []
        received_at, frames = result
        if time.monotonic() - received_at > _MAX_PREROLL_AGE_S:
            logger.warning("discarding stale pre-roll")
            return []
        return frames

    @utils.log_exceptions(logger=logger)
    async def _read(self, reader: rtc.ByteStreamReader, participant_id: str) -> None:
        attrs = reader.info.attributes or {}
        sample_rate = int(attrs.get("sampleRate", "16000"))
        channels = int(attrs.get("channels", "1"))

        bstream = utils.audio.AudioByteStream(sample_rate, channels)
        frames: list[rtc.AudioFrame] = []
        async for chunk in reader:
            frames.extend(bstream.push(chunk))
        frames.extend(bstream.flush())

        duration = sum(f.duration for f in frames)
        logger.debug(
            "pre-roll received",
            extra={"participant": participant_id, "duration": duration},
        )
        self._result = (time.monotonic(), frames)
        self._event.set()
