from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from src.capabilities.base import CapabilityRequest
from src.capabilities.computer import (
    ComputerControlProvider,
    _find_control,
    _parse_open_and_press,
    _parse_press_control,
    _screen_changed,
)
from src.config import settings
from src.events import bus
from src.native_bridge import NativeToolBridge


def envelope(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "spoken": "done", "data": data or {}, "error": None}


def observation(*, changed: bool = False) -> dict[str, Any]:
    return envelope(
        {
            "app": "Minecraft Launcher",
            "snapshotId": "snapshot-2" if changed else "snapshot-1",
            "elements": [
                {
                    "id": "snapshot-1-ui-4",
                    "role": "AXButton",
                    "title": "Launching" if changed else "Play",
                    "enabled": True,
                    "actions": ["AXPress"],
                    "bounds": {"x": 10, "y": 20, "width": 80, "height": 30},
                }
            ],
        }
    )


class FakeBridge:
    connected = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inspect_count = 0
        self.clicked = False

    async def wait_until_connected(self, timeout: float = 0.75) -> bool:
        del timeout
        return self.connected

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = 10,
    ) -> dict[str, Any]:
        del timeout
        payload = arguments or {}
        self.calls.append((tool, payload))
        if tool == "inspect_ui":
            self.inspect_count += 1
            return observation(changed=self.inspect_count > 1)
        if tool == "locate_ui":
            return envelope(
                {
                    "found": True,
                    "x": 200.0,
                    "y": 300.0,
                    "confidence": 0.9,
                    "visualFingerprint": "0000000000000000",
                    "visualToken": "visual-token",
                }
            )
        if tool == "input_control":
            self.clicked = True
            return envelope({"success": True})
        if tool == "observe_screen":
            fingerprint = "000000000000000f" if self.clicked else "0000000000000000"
            return envelope(
                {
                    "available": True,
                    "processId": 42,
                    "visualFingerprint": fingerprint,
                }
            )
        if tool == "verify_ui":
            return envelope(
                {
                    "succeeded": True,
                    "confidence": 0.95,
                    "reason": "The requested control visibly activated.",
                }
            )
        return envelope({"success": True})


async def no_progress(_phase: str, _message: str) -> None:
    return None


class ComputerControlTests(unittest.IsolatedAsyncioTestCase):
    def test_parses_generic_open_and_press_goal(self) -> None:
        self.assertEqual(
            _parse_open_and_press("Open Minecraft and press Play"),
            ("Minecraft", "Play"),
        )
        self.assertEqual(
            _parse_open_and_press("Launch System Settings then click Displays"),
            ("System Settings", "Displays"),
        )
        self.assertEqual(
            _parse_open_and_press("Open Minecraft and press press Play"),
            ("Minecraft", "Play"),
        )
        self.assertEqual(_parse_press_control("Press Play"), "Play")

    def test_finds_exact_accessible_control_before_visual_fallback(self) -> None:
        found = _find_control(observation(), "Play")

        self.assertIsNotNone(found)
        element, action = found or ({}, "")
        self.assertEqual(element["id"], "snapshot-1-ui-4")
        self.assertEqual(action, "AXPress")

    def test_screen_change_uses_perceptual_distance(self) -> None:
        changed, distance = _screen_changed(
            {"processId": 42, "visualFingerprint": "0000000000000000"},
            {"processId": 42, "visualFingerprint": "000000000000000f"},
        )

        self.assertTrue(changed)
        self.assertEqual(distance, 4)

    async def test_open_and_press_uses_native_observe_act_verify_loop(self) -> None:
        bridge = FakeBridge()
        provider = ComputerControlProvider(bridge=bridge)

        result = await provider.execute(
            CapabilityRequest(
                capability="computer",
                goal="Open Minecraft and press Play",
                permission="low_risk_write",
            ),
            no_progress,
        )

        self.assertEqual(result.data["path"], "accessibility")
        self.assertTrue(result.data["verifiedChange"])
        self.assertEqual(
            [tool for tool, _arguments in bridge.calls],
            [
                "open_app",
                "inspect_ui",
                "observe_screen",
                "interact_ui",
                "inspect_ui",
                "observe_screen",
            ],
        )

    async def test_unfamiliar_goal_uses_planner_until_verified_finish(self) -> None:
        bridge = FakeBridge()
        decisions = iter(
            [
                {"action": "open_app", "arguments": {"app": "Notes"}},
                {
                    "action": "finish",
                    "arguments": {"summary": "Opened Notes."},
                },
            ]
        )

        async def planner(
            _goal: str,
            _observation: dict[str, Any],
            _history: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return next(decisions)

        provider = ComputerControlProvider(bridge=bridge, planner=planner)
        result = await provider.execute(
            CapabilityRequest(
                capability="computer",
                goal="Bring up the notes application",
                permission="low_risk_write",
            ),
            no_progress,
        )

        self.assertEqual(result.summary, "Opened Notes")
        self.assertEqual(
            [tool for tool, _arguments in bridge.calls],
            [
                "inspect_ui",
                "observe_screen",
                "open_app",
                "inspect_ui",
                "observe_screen",
            ],
        )

    async def test_visual_fallback_clicks_only_grounded_coordinates(self) -> None:
        bridge = FakeBridge()

        async def no_control_call(
            tool: str,
            arguments: dict[str, Any] | None = None,
            *,
            timeout: float = 10,
        ) -> dict[str, Any]:
            if tool == "inspect_ui":
                bridge.calls.append((tool, arguments or {}))
                return envelope({"app": "Canvas App", "elements": []})
            return await FakeBridge.call(
                bridge,
                tool,
                arguments,
                timeout=timeout,
            )

        bridge.call = no_control_call  # type: ignore[method-assign]
        provider = ComputerControlProvider(bridge=bridge)
        with patch(
            "src.capabilities.computer.APP_LAUNCH_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await provider.execute(
                CapabilityRequest(
                    capability="computer",
                    goal="unused",
                    inputs={"action": "press_control", "label": "Play"},
                    permission="low_risk_write",
                ),
                no_progress,
            )

        self.assertEqual(result.data["path"], "visual")
        self.assertTrue(result.data["verifiedChange"])
        self.assertIn("locate_ui", [tool for tool, _arguments in bridge.calls])
        self.assertIn("input_control", [tool for tool, _arguments in bridge.calls])

    async def test_planner_uses_fast_model_for_confident_first_attempt(self) -> None:
        payloads: list[dict[str, Any]] = []

        def fake_post(payload: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict:
            payloads.append(payload)
            return {
                "output_text": (
                    '{"action":"open_app","arguments":{"app":"Notes"},'
                    '"expected":"Notes opens","confidence":0.91}'
                )
            }

        originals = (
            settings.openai_api_key,
            settings.computer_model,
            settings.computer_reasoning_effort,
            settings.computer_escalation_model,
            settings.computer_escalation_reasoning_effort,
        )
        try:
            settings.openai_api_key = "test-key"
            settings.computer_model = "gpt-5.4-mini"
            settings.computer_reasoning_effort = "low"
            settings.computer_escalation_model = "gpt-5.6-terra"
            settings.computer_escalation_reasoning_effort = "low"
            provider = ComputerControlProvider(bridge=FakeBridge())
            with patch("src.capabilities.computer.post", side_effect=fake_post):
                decision = await provider._plan_with_openai("Open Notes", {}, [])
        finally:
            (
                settings.openai_api_key,
                settings.computer_model,
                settings.computer_reasoning_effort,
                settings.computer_escalation_model,
                settings.computer_escalation_reasoning_effort,
            ) = originals

        self.assertEqual([payload["model"] for payload in payloads], ["gpt-5.4-mini"])
        self.assertEqual(payloads[0]["reasoning"], {"effort": "low"})
        self.assertEqual(decision["_plannerModel"], "gpt-5.4-mini")
        self.assertFalse(decision["_plannerEscalated"])

    async def test_planner_escalates_an_ambiguous_decision_to_terra(self) -> None:
        payloads: list[dict[str, Any]] = []

        def fake_post(payload: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict:
            payloads.append(payload)
            confidence = 0.4 if len(payloads) == 1 else 0.95
            return {
                "output_text": (
                    '{"action":"visual_click","arguments":{"target":"Play"},'
                    f'"expected":"Play activates","confidence":{confidence}}}'
                )
            }

        originals = (
            settings.openai_api_key,
            settings.computer_model,
            settings.computer_reasoning_effort,
            settings.computer_escalation_model,
            settings.computer_escalation_reasoning_effort,
        )
        try:
            settings.openai_api_key = "test-key"
            settings.computer_model = "gpt-5.4-mini"
            settings.computer_reasoning_effort = "low"
            settings.computer_escalation_model = "gpt-5.6-terra"
            settings.computer_escalation_reasoning_effort = "low"
            provider = ComputerControlProvider(bridge=FakeBridge())
            with patch("src.capabilities.computer.post", side_effect=fake_post):
                decision = await provider._plan_with_openai("Press Play", {}, [])
        finally:
            (
                settings.openai_api_key,
                settings.computer_model,
                settings.computer_reasoning_effort,
                settings.computer_escalation_model,
                settings.computer_escalation_reasoning_effort,
            ) = originals

        self.assertEqual(
            [payload["model"] for payload in payloads],
            ["gpt-5.4-mini", "gpt-5.6-terra"],
        )
        self.assertEqual(payloads[1]["reasoning"], {"effort": "low"})
        self.assertEqual(decision["_plannerModel"], "gpt-5.6-terra")
        self.assertTrue(decision["_plannerEscalated"])

    async def test_planner_starts_with_terra_after_a_failed_step(self) -> None:
        payloads: list[dict[str, Any]] = []

        def fake_post(payload: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict:
            payloads.append(payload)
            return {
                "output_text": (
                    '{"action":"wait","arguments":{"seconds":0.5},'
                    '"expected":"UI settles","confidence":0.9}'
                )
            }

        originals = (
            settings.openai_api_key,
            settings.computer_model,
            settings.computer_reasoning_effort,
            settings.computer_escalation_model,
            settings.computer_escalation_reasoning_effort,
        )
        try:
            settings.openai_api_key = "test-key"
            settings.computer_model = "gpt-5.4-mini"
            settings.computer_reasoning_effort = "low"
            settings.computer_escalation_model = "gpt-5.6-terra"
            settings.computer_escalation_reasoning_effort = "low"
            provider = ComputerControlProvider(bridge=FakeBridge())
            history = [{"action": "interact_ui", "ok": False, "error": "stale"}]
            with patch("src.capabilities.computer.post", side_effect=fake_post):
                decision = await provider._plan_with_openai("Continue", {}, history)
        finally:
            (
                settings.openai_api_key,
                settings.computer_model,
                settings.computer_reasoning_effort,
                settings.computer_escalation_model,
                settings.computer_escalation_reasoning_effort,
            ) = originals

        self.assertEqual([payload["model"] for payload in payloads], ["gpt-5.6-terra"])
        self.assertEqual(decision["_plannerModel"], "gpt-5.6-terra")


class NativeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_correlates_native_request_and_response(self) -> None:
        bridge = NativeToolBridge()
        queue = await bus.subscribe()
        bridge.connect()
        try:
            task = asyncio.create_task(bridge.call("open_app", {"app": "Notes"}))
            request = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(request["type"], "native_tool_request")
            bridge.handle_response(
                {
                    "type": "native_tool_response",
                    "requestId": request["requestId"],
                    "result": envelope({"name": "Notes"}),
                }
            )
            result = await task
            self.assertEqual(result["data"]["name"], "Notes")
        finally:
            bridge.disconnect()
            await bus.unsubscribe(queue)


if __name__ == "__main__":
    unittest.main()
