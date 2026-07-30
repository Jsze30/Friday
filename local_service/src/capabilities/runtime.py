from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from .base import CapabilityRequest
from .broker import AllProvidersFailed, CapabilityBroker

log = logging.getLogger("friday.capabilities.runtime")

TaskStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
MAX_TASKS = 100
TASK_TTL_SECONDS = 15 * 60
MAX_EVENTS = 30
CAPABILITY_PERMISSIONS = {
    "music": "low_risk_write",
}


@dataclass
class TaskEvent:
    sequence: int
    phase: str
    message: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "phase": self.phase,
            "message": self.message,
            "timestamp": self.timestamp,
        }


@dataclass
class CapabilityTask:
    task_id: str
    request: CapabilityRequest
    status: TaskStatus = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    events: list[TaskEvent] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    provider: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    runner: asyncio.Task[None] | None = None

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        return {
            "ok": self.status not in {"failed"},
            "taskId": self.task_id,
            "capability": self.request.capability,
            "status": self.status,
            "provider": self.provider,
            "events": [
                event.to_dict()
                for event in self.events
                if event.sequence > since
            ],
            "lastSequence": self.events[-1].sequence if self.events else 0,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


class CapabilityRuntime:
    def __init__(self, broker: CapabilityBroker) -> None:
        self._broker = broker
        self._tasks: dict[str, CapabilityTask] = {}

    async def catalog(self) -> dict[str, Any]:
        return {"ok": True, **(await self._broker.catalog())}

    async def start(
        self,
        capability: str,
        goal: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        capability = capability.strip().casefold()
        goal = goal.strip()
        if not capability:
            return {"ok": False, "error": "capability is required"}
        if not goal:
            return {"ok": False, "error": "goal is required"}
        await self._discard_expired()
        request = CapabilityRequest(
            capability=capability,
            goal=goal,
            inputs=inputs or {},
            permission=CAPABILITY_PERMISSIONS.get(capability, "read_only"),
        )
        task_id = str(uuid.uuid4())
        record = CapabilityTask(task_id=task_id, request=request)
        self._tasks[task_id] = record
        record.runner = asyncio.create_task(
            self._run(record),
            name=f"friday-capability-{task_id}",
        )
        log.info(
            "capability started task=%s capability=%s",
            task_id,
            capability,
        )
        return record.snapshot()

    async def status(self, task_id: str, since: int = 0) -> dict[str, Any]:
        record = self._tasks.get(task_id)
        if record is None:
            return {"ok": False, "error": "unknown capability task"}
        return record.snapshot(max(0, since))

    async def cancel(self, task_id: str) -> dict[str, Any]:
        record = self._tasks.get(task_id)
        if record is None:
            return {"ok": False, "error": "unknown capability task"}
        if record.runner and not record.runner.done():
            record.runner.cancel()
            try:
                await record.runner
            except asyncio.CancelledError:
                pass
        return record.snapshot()

    async def shutdown(self) -> None:
        running = [
            record.runner
            for record in self._tasks.values()
            if record.runner and not record.runner.done()
        ]
        for runner in running:
            runner.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await self._broker.shutdown()

    async def _run(self, record: CapabilityTask) -> None:
        record.status = "running"
        record.updated_at = time.time()

        async def progress(phase: str, message: str) -> None:
            record.events.append(
                TaskEvent(
                    sequence=(
                        record.events[-1].sequence + 1
                        if record.events
                        else 1
                    ),
                    phase=phase,
                    message=message[:500],
                    timestamp=time.time(),
                )
            )
            record.events = record.events[-MAX_EVENTS:]
            record.updated_at = time.time()

        try:
            result, attempts, provider = await self._broker.execute(
                record.request,
                progress,
            )
            record.status = "succeeded"
            record.result = result.to_dict()
            record.provider = provider
            record.attempts = [attempt.to_dict() for attempt in attempts]
            await progress("complete", result.summary)
        except asyncio.CancelledError:
            record.status = "cancelled"
            await progress("cancelled", "Capability task cancelled.")
            raise
        except AllProvidersFailed as error:
            record.status = "failed"
            record.error = str(error)[:2_000]
            record.attempts = [
                attempt.to_dict() for attempt in error.attempts
            ]
            await progress("failed", "Capability task failed.")
            log.warning(
                "capability failed task=%s capability=%s error=%s",
                record.task_id,
                record.request.capability,
                error,
            )
        # A background task must always become terminal even if a provider
        # violates the provider contract with an unexpected exception.
        except Exception as error:  # noqa: BLE001
            record.status = "failed"
            record.error = str(error)[:2_000]
            await progress("failed", "Capability task failed.")
            log.warning(
                "capability failed task=%s capability=%s error=%s",
                record.task_id,
                record.request.capability,
                error,
            )
        finally:
            record.updated_at = time.time()

    async def _discard_expired(self) -> None:
        cutoff = time.time() - TASK_TTL_SECONDS
        expired = [
            task_id
            for task_id, record in self._tasks.items()
            if record.status in TERMINAL_STATUSES and record.updated_at < cutoff
        ]
        for task_id in expired:
            self._tasks.pop(task_id, None)
        if len(self._tasks) <= MAX_TASKS:
            return
        removable = sorted(
            (
                record
                for record in self._tasks.values()
                if record.status in TERMINAL_STATUSES
            ),
            key=lambda record: record.updated_at,
        )
        for record in removable[: len(self._tasks) - MAX_TASKS]:
            self._tasks.pop(record.task_id, None)
