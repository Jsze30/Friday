from __future__ import annotations

import asyncio
import json
import unittest

from action_catalog import ActionCatalog
from action_tool import build_action_tool


class FakeRunContext:
    def __init__(self) -> None:
        self.interruptions_disallowed = False

    def disallow_interruptions(self) -> None:
        self.interruptions_disallowed = True


class ActionToolTests(unittest.TestCase):
    def test_runs_primitive_action_immediately(self) -> None:
        primitive_calls: list[tuple[str, dict]] = []
        events: list[tuple[str, dict]] = []

        async def rpc_call(_method: str, _payload: str) -> str:
            raise AssertionError("primitive action should not call capability RPC")

        async def call_primitive(name: str, arguments: dict) -> dict:
            primitive_calls.append((name, arguments))
            return {
                "ok": True,
                "spoken": "Opened Spotify.",
                "data": {"app": "Spotify"},
            }

        catalog = ActionCatalog(
            [
                {
                    "id": "system.open_app",
                    "target": {"kind": "primitive", "tool": "open_app"},
                    "permission": "low_risk_write",
                    "parameters": [
                        {
                            "name": "app",
                            "type": "string",
                            "required": True,
                        }
                    ],
                    "routes": [],
                }
            ]
        )
        context = FakeRunContext()
        tool = build_action_tool(
            rpc_call,
            call_primitive,
            catalog,
            event_sink=lambda event, payload: events.append((event, payload)),
        )

        raw = asyncio.run(
            tool(
                context=context,
                action="system.open_app",
                arguments_json='{"app": "Spotify"}',
            )
        )

        self.assertEqual(primitive_calls, [("open_app", {"app": "Spotify"})])
        self.assertTrue(context.interruptions_disallowed)
        self.assertEqual(json.loads(raw)["message"], "Opened Spotify.")
        self.assertEqual(
            [event for event, _ in events],
            ["action_started", "action_completed"],
        )
        self.assertTrue(events[1][1]["ok"])

    def test_runs_provider_action_through_one_shared_rpc(self) -> None:
        rpc_calls: list[tuple[str, dict]] = []

        async def rpc_call(method: str, payload: str) -> str:
            decoded = json.loads(payload)
            rpc_calls.append((method, decoded))
            return json.dumps(
                {
                    "ok": True,
                    "status": "succeeded",
                    "provider": "spotify-web-api",
                    "result": {"summary": "Paused Spotify."},
                    "attempts": [],
                }
            )

        async def call_primitive(_name: str, _arguments: dict) -> dict:
            raise AssertionError("provider action should not call a primitive")

        catalog = ActionCatalog(
            [
                {
                    "id": "music.pause",
                    "target": {
                        "kind": "capability",
                        "action": "music.pause",
                    },
                    "permission": "low_risk_write",
                    "parameters": [],
                    "routes": [],
                }
            ]
        )
        context = FakeRunContext()
        tool = build_action_tool(rpc_call, call_primitive, catalog)

        raw = asyncio.run(
            tool(
                context=context,
                action="music.pause",
                arguments_json="{}",
            )
        )

        self.assertEqual(rpc_calls[0][0], "capability_call")
        self.assertEqual(rpc_calls[0][1]["operation"], "action")
        self.assertEqual(rpc_calls[0][1]["action"], "music.pause")
        self.assertTrue(context.interruptions_disallowed)
        self.assertEqual(json.loads(raw)["provider"], "spotify-web-api")


if __name__ == "__main__":
    unittest.main()
