from __future__ import annotations

import json
import unittest

from stop_tool import build_stop_tool


class StopToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_tool_calls_the_shared_cancellation_control(self) -> None:
        calls = 0

        async def cancel_work() -> dict:
            nonlocal calls
            calls += 1
            return {"ok": True, "cancelledCount": 3}

        tool = build_stop_tool(cancel_work)
        result = json.loads(await tool._func())

        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["cancelledCount"], 3)


if __name__ == "__main__":
    unittest.main()
