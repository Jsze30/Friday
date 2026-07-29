from __future__ import annotations

import unittest

from model_router import route_request


class RouteRequestTests(unittest.TestCase):
    def test_routes_short_conversation_to_fast_model(self) -> None:
        decision = route_request("How are you?")

        self.assertEqual(decision.route, "fast")

    def test_routes_simple_tool_request_to_fast_model(self) -> None:
        decision = route_request("What time is it in Chicago?")

        self.assertEqual(decision.route, "fast")

    def test_routes_explicit_complex_request_to_complex_model(self) -> None:
        decision = route_request("Use the smarter model and help me solve this.")

        self.assertEqual(decision.route, "complex")
        self.assertEqual(decision.reason, "explicit complex-model request")

    def test_routes_analysis_request_to_complex_model(self) -> None:
        decision = route_request("Analyze why the wake detector sometimes misses me.")

        self.assertEqual(decision.route, "complex")

    def test_routes_multi_step_request_to_complex_model(self) -> None:
        decision = route_request(
            "Check my calendar and then find an open hour for a workout."
        )

        self.assertEqual(decision.route, "complex")

    def test_routes_long_request_to_complex_model(self) -> None:
        decision = route_request("word " * 45)

        self.assertEqual(decision.route, "complex")
        self.assertIn("45 words", decision.reason)

    def test_routes_tool_followup_text_consistently(self) -> None:
        text = "Compare both options and explain the tradeoffs."

        self.assertEqual(route_request(text), route_request(text))


if __name__ == "__main__":
    unittest.main()
