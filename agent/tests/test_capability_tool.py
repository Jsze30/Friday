from __future__ import annotations

import asyncio
import inspect
import json
import unittest

from livekit.agents import function_tool
from livekit.agents.beta.toolsets import ToolProxyToolset

from capability_tool import _decode, _terminal_result, build_capability_tool


class CapabilityToolTests(unittest.TestCase):
    def test_builds_one_cancellable_function_tool(self) -> None:
        async def rpc_call(_method: str, _payload: str) -> str:
            return "{}"

        tool = build_capability_tool(rpc_call, ["files", "browser"])

        self.assertEqual(tool.info.name, "run_capability")
        self.assertIn("inputs_json", inspect.signature(tool).parameters)
        self.assertIn("browser", tool.info.description)

    def test_decodes_only_json_objects(self) -> None:
        self.assertFalse(_decode(None)["ok"])
        self.assertFalse(_decode("[]")["ok"])
        self.assertEqual(_decode('{"ok": true}'), {"ok": True})

    def test_terminal_result_keeps_provider_trace(self) -> None:
        result = json.loads(
            _terminal_result(
                {
                    "status": "succeeded",
                    "provider": "research-direct",
                    "result": {"summary": "done"},
                    "attempts": [{"provider": "research-direct"}],
                }
            )
        )

        self.assertEqual(result["provider"], "research-direct")
        self.assertEqual(result["result"]["summary"], "done")

    def test_proxy_toolset_exposes_only_search_and_call(self) -> None:
        async def primitive(value: str) -> str:
            return value

        proxy = ToolProxyToolset(
            id="test-primitives",
            tools=[function_tool(primitive, description="test primitive")],
        )

        names = {tool.info.name for tool in proxy.tools}
        self.assertEqual(names, {"tool_search", "call_tool"})

    def test_failed_start_closes_the_hud_activity(self) -> None:
        events: list[tuple[str, dict]] = []

        async def rpc_call(_method: str, _payload: str) -> str:
            return '{"ok": true}'

        tool = build_capability_tool(
            rpc_call,
            ["files"],
            event_sink=lambda event, payload: events.append((event, payload)),
        )

        result = asyncio.run(
            tool(
                context=object(),
                capability="files",
                goal="Read a file",
                inputs_json="{}",
            )
        )

        self.assertIn("did not return a task ID", result)
        self.assertEqual(
            [event for event, _ in events],
            ["capability_started", "capability_completed"],
        )
        self.assertFalse(events[-1][1]["ok"])


if __name__ == "__main__":
    unittest.main()
