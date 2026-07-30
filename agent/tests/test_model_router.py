from __future__ import annotations

import unittest

from model_router import route_request


class RouteRequestTests(unittest.TestCase):
    def test_routes_short_conversation_to_fast_model(self) -> None:
        decision = route_request("How are you?")

        self.assertEqual(decision.route, "fast")

    def test_routes_ambient_time_request_to_fast_model(self) -> None:
        decision = route_request("What time is it?")

        self.assertEqual(decision.route, "fast")

    def test_routes_computer_action_to_complex_model(self) -> None:
        decision = route_request("Show me what is in Downloads.")

        self.assertEqual(decision.route, "complex")
        self.assertEqual(decision.reason, "computer or web action")

    def test_routes_open_app_to_fast_model(self) -> None:
        decision = route_request("Open Spotify.")

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "simple Mac control")

    def test_routes_open_url_to_fast_model(self) -> None:
        decision = route_request("Open https://example.com in Arc.")

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "simple Mac control")

    def test_routes_volume_change_to_fast_model(self) -> None:
        decision = route_request("Set the volume to 40.")

        self.assertEqual(decision.route, "fast")

    def test_routes_named_spotify_song_to_fast_model(self) -> None:
        decision = route_request("Play Pink and White by Frank Ocean.")

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "simple music control")

    def test_routes_open_playlist_to_fast_model(self) -> None:
        decision = route_request("Open my road trip playlist.")

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "simple music control")

    def test_routes_app_action_with_analysis_to_complex_model(self) -> None:
        decision = route_request("Open VS Code and analyze the wake detector.")

        self.assertEqual(decision.route, "complex")
        self.assertEqual(decision.reason, "analysis request")

    def test_routes_weather_to_complex_model(self) -> None:
        decision = route_request("What is the weather here?")

        self.assertEqual(decision.route, "complex")

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
