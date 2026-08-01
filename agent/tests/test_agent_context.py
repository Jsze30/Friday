from __future__ import annotations

import unittest

from livekit.agents import function_tool

from agent import (
    BASE_INSTRUCTIONS,
    FridayAgent,
    matching_route_tools,
    render_instructions,
)


class AgentContextTests(unittest.TestCase):
    def test_identity_is_friday(self) -> None:
        self.assertIn("Your name is Friday.", BASE_INSTRUCTIONS)
        self.assertNotIn("Jarvis", BASE_INSTRUCTIONS)
        self.assertNotIn("confirm_action", BASE_INSTRUCTIONS)
        self.assertEqual(FridayAgent.__name__, "FridayAgent")

    def test_deterministic_route_exposes_only_the_selected_tool(self) -> None:
        async def run_action() -> str:
            return "action"

        async def run_capability() -> str:
            return "capability"

        selected = matching_route_tools(
            [
                function_tool(run_action),
                function_tool(run_capability),
            ],
            "run_action",
        )

        self.assertEqual([tool.info.name for tool in selected], ["run_action"])

    def test_live_location_replaces_saved_location_memory(self) -> None:
        instructions = render_instructions(
            {
                "facts": {
                    "home_city": "Champaign, Illinois",
                    "name": "Jason",
                }
            },
            {
                "status": "available",
                "place": "Chicago, Illinois, United States",
                "city": "Chicago",
                "latitude": 41.8781,
                "longitude": -87.6298,
                "horizontalAccuracyMeters": 100,
                "timestamp": "2026-07-29T22:00:00Z",
            },
        )

        self.assertNotIn("Champaign", instructions)
        self.assertIn("- name: Jason", instructions)
        self.assertIn("- place: Chicago, Illinois, United States", instructions)
        self.assertIn("- latitude: 41.8781", instructions)


if __name__ == "__main__":
    unittest.main()
