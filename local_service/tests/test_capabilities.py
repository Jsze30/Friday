from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from src.capabilities.base import (
    CapabilityProvider,
    CapabilityRequest,
    CapabilityResult,
    ProgressCallback,
    ProviderFailed,
    ProviderInfo,
)
from src.capabilities.broker import CapabilityBroker
from src.capabilities.providers import FileProvider
from src.capabilities.runtime import CapabilityRuntime


class FakeProvider(CapabilityProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        priority: int,
        result: str | None = None,
        fail: bool = False,
        delay: float = 0,
        permission: str = "read_only",
    ) -> None:
        self.info = ProviderInfo(
            provider_id=provider_id,
            name=provider_id,
            description="test provider",
            capabilities=("test",),
            permission=permission,
            priority=priority,
        )
        self.result = result
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ProviderFailed("intentional failure")
        return CapabilityResult(summary=self.result or "done")


async def wait_for_terminal(
    runtime: CapabilityRuntime,
    task_id: str,
) -> dict:
    for _ in range(100):
        status = await runtime.status(task_id)
        if status.get("status") in {"succeeded", "failed", "cancelled"}:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("capability task did not finish")


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_falls_back_after_provider_failure(self) -> None:
        preferred = FakeProvider("preferred", priority=100, fail=True)
        fallback = FakeProvider("fallback", priority=50, result="fallback result")
        broker = CapabilityBroker([fallback, preferred])

        async def progress(_phase: str, _message: str) -> None:
            return None

        result, attempts, selected = await broker.execute(
            CapabilityRequest(capability="test", goal="test"),
            progress,
        )

        self.assertEqual(result.summary, "fallback result")
        self.assertEqual(selected, "fallback")
        self.assertEqual([attempt.status for attempt in attempts], ["failed", "succeeded"])
        self.assertEqual(preferred.calls, 1)
        self.assertEqual(fallback.calls, 1)

    async def test_runtime_starts_and_returns_a_background_result(self) -> None:
        provider = FakeProvider("fast", priority=100, result="complete")
        runtime = CapabilityRuntime(CapabilityBroker([provider]))
        started = await runtime.start("test", "finish this")

        self.assertEqual(started["status"], "queued")
        status = await wait_for_terminal(runtime, started["taskId"])

        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["provider"], "fast")
        self.assertEqual(status["result"]["summary"], "complete")
        await runtime.shutdown()

    async def test_music_uses_the_local_low_risk_permission_policy(self) -> None:
        provider = FakeProvider(
            "music-provider",
            priority=100,
            result="playing",
            permission="low_risk_write",
        )
        provider.info = ProviderInfo(
            provider_id="music-provider",
            name="music-provider",
            description="test provider",
            capabilities=("music",),
            permission="low_risk_write",
            priority=100,
        )
        runtime = CapabilityRuntime(CapabilityBroker([provider]))

        started = await runtime.start(
            "music",
            "play something",
            {"action": "play"},
        )
        status = await wait_for_terminal(runtime, started["taskId"])

        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(provider.calls, 1)
        await runtime.shutdown()

    async def test_file_provider_reads_an_allowed_file(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            source = Path(temporary) / "notes.txt"
            source.write_text("Friday capability content")
            provider = FileProvider()

            async def progress(_phase: str, _message: str) -> None:
                return None

            result = await provider.execute(
                CapabilityRequest(
                    capability="files",
                    goal="read notes",
                    inputs={"operation": "read", "path": str(source)},
                ),
                progress,
            )

            self.assertEqual(
                result.data["content"],
                "Friday capability content",
            )

    async def test_runtime_cancels_background_work(self) -> None:
        provider = FakeProvider(
            "slow",
            priority=100,
            result="too late",
            delay=10,
        )
        runtime = CapabilityRuntime(CapabilityBroker([provider]))
        started = await runtime.start("test", "wait")
        await asyncio.sleep(0)

        status = await runtime.cancel(started["taskId"])

        self.assertEqual(status["status"], "cancelled")
        await runtime.shutdown()

    async def test_failed_task_keeps_all_provider_attempts(self) -> None:
        first = FakeProvider("first", priority=100, fail=True)
        second = FakeProvider("second", priority=50, fail=True)
        runtime = CapabilityRuntime(CapabilityBroker([first, second]))
        started = await runtime.start("test", "fail")

        status = await wait_for_terminal(runtime, started["taskId"])

        self.assertEqual(status["status"], "failed")
        self.assertEqual(
            [attempt["provider"] for attempt in status["attempts"]],
            ["first", "second"],
        )
        await runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
