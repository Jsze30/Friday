from __future__ import annotations

import unittest

from agent import BASE_INSTRUCTIONS, FridayAgent, render_instructions


class AgentContextTests(unittest.TestCase):
    def test_identity_is_friday(self) -> None:
        self.assertIn("Your name is Friday.", BASE_INSTRUCTIONS)
        self.assertNotIn("Jarvis", BASE_INSTRUCTIONS)
        self.assertEqual(FridayAgent.__name__, "FridayAgent")

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
