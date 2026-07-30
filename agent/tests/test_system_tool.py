from __future__ import annotations

import asyncio
import json
import unittest

from system_tool import build_system_tool


class SystemToolTests(unittest.TestCase):
    def test_routes_volume_to_the_native_primitive(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def call_primitive(name: str, arguments: dict) -> dict:
            calls.append((name, arguments))
            return {
                "ok": True,
                "spoken": "Set the Mac volume to 40 percent.",
                "data": {"volume": 40, "muted": False},
            }

        tool = build_system_tool(call_primitive)
        raw = asyncio.run(
            tool(
                action="set_volume",
                volume=40,
            )
        )

        self.assertEqual(calls, [("set_volume", {"volume": 40})])
        self.assertEqual(json.loads(raw)["data"]["volume"], 40)

    def test_quit_preserves_confirmation_fields(self) -> None:
        async def call_primitive(_name: str, _arguments: dict) -> dict:
            return {
                "ok": True,
                "spoken": "I need confirmation before I quit Spotify.",
                "needsConfirmation": True,
                "confirmationId": "mac:123",
            }

        tool = build_system_tool(call_primitive)
        raw = asyncio.run(tool(action="quit_app", app="Spotify"))
        result = json.loads(raw)

        self.assertTrue(result["needsConfirmation"])
        self.assertEqual(result["confirmationId"], "mac:123")

    def test_routes_url_to_requested_browser(self) -> None:
        calls: list[tuple[str, dict]] = []

        async def call_primitive(name: str, arguments: dict) -> dict:
            calls.append((name, arguments))
            return {
                "ok": True,
                "spoken": "Opened example.com in Arc.",
                "data": {
                    "url": "https://example.com",
                    "browser": "Arc",
                },
            }

        tool = build_system_tool(call_primitive)
        raw = asyncio.run(
            tool(
                action="open_url",
                url="https://example.com",
                browser="Arc",
            )
        )

        self.assertEqual(
            calls,
            [
                (
                    "open_url",
                    {"url": "https://example.com", "browser": "Arc"},
                )
            ],
        )
        self.assertEqual(json.loads(raw)["data"]["browser"], "Arc")

    def test_open_url_requires_a_url(self) -> None:
        called = False

        async def call_primitive(_name: str, _arguments: dict) -> dict:
            nonlocal called
            called = True
            return {"ok": True}

        tool = build_system_tool(call_primitive)
        result = asyncio.run(tool(action="open_url"))

        self.assertFalse(called)
        self.assertIn("url is required", result)

    def test_rejects_invalid_volume_without_calling_primitive(self) -> None:
        called = False

        async def call_primitive(_name: str, _arguments: dict) -> dict:
            nonlocal called
            called = True
            return {"ok": True}

        tool = build_system_tool(call_primitive)
        result = asyncio.run(tool(action="set_volume", volume=101))

        self.assertFalse(called)
        self.assertIn("0 to 100", result)


if __name__ == "__main__":
    unittest.main()
