from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from livekit import rtc

logger = logging.getLogger("friday-agent.hud")

HUD_TOPIC = "friday.hud"


class HudPublisher:
    """Best-effort structured event stream from the agent to the Mac HUD."""

    def __init__(self, participant: rtc.LocalParticipant) -> None:
        self._participant = participant
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        self._task: asyncio.Task[None] | None = None
        self._turn_id: str | None = None
        self._turn_started_at: float | None = None
        self._destination_identity: str | None = None

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def set_destination(self, identity: str | None) -> None:
        self._destination_identity = identity

    def begin_turn(self) -> str:
        self._turn_id = uuid.uuid4().hex
        self._turn_started_at = time.monotonic()
        self.emit("turn_started")
        return self._turn_id

    def emit(self, event_type: str, **payload: Any) -> None:
        event = {
            "version": 1,
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "turnId": self._turn_id,
            **payload,
        }
        if self._turn_started_at is not None:
            event["elapsedMs"] = round(
                (time.monotonic() - self._turn_started_at) * 1000,
                1,
            )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(
                "dropping HUD event because the queue is full: %s", event_type
            )

    async def aclose(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            try:
                arguments: dict[str, Any] = {"topic": HUD_TOPIC}
                if self._destination_identity:
                    arguments["destination_identities"] = [self._destination_identity]
                await self._participant.send_text(
                    json.dumps(event, separators=(",", ":")),
                    **arguments,
                )
            except Exception as error:  # noqa: BLE001
                logger.warning("failed to publish HUD event: %s", error)
