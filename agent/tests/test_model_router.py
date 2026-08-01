from __future__ import annotations

import json
import unittest

from action_catalog import ActionCatalog
from model_router import deterministic_tool_route, route_request


class RouteRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        capability_target = {"kind": "capability", "action": "catalog"}
        primitive_target = {"kind": "primitive", "tool": "native"}
        self.catalog = ActionCatalog(
            [
                {
                    "id": "music.pause",
                    "target": capability_target,
                    "parameters": [],
                    "routes": [{"pattern": r"pause\s+the\s+music"}],
                    "priority": 120,
                },
                {
                    "id": "music.resume",
                    "target": capability_target,
                    "parameters": [],
                    "routes": [{"pattern": r"play\s+this\s+song"}],
                    "priority": 130,
                },
                {
                    "id": "music.play",
                    "target": capability_target,
                    "parameters": [
                        {"name": "query", "type": "string", "required": True}
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"play\s+(?P<query>.+?)"
                                r"(?:\s+on\s+spotify)?"
                            )
                        }
                    ],
                    "priority": 100,
                },
                {
                    "id": "music.play_playlist",
                    "target": capability_target,
                    "parameters": [
                        {"name": "playlist", "type": "string", "required": True}
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"play\s+(?:my\s+)?(?P<playlist>.+?)"
                                r"\s+playlist"
                            )
                        }
                    ],
                    "priority": 150,
                },
                {
                    "id": "system.open_app",
                    "target": primitive_target,
                    "parameters": [{"name": "app", "type": "string", "required": True}],
                    "routes": [{"pattern": (r"(?:open|launch)\s+(?P<app>[\w .'-]+)")}],
                    "priority": 100,
                },
                {
                    "id": "system.open_url",
                    "target": primitive_target,
                    "parameters": [
                        {"name": "url", "type": "string", "required": True},
                        {
                            "name": "browser",
                            "type": "string",
                            "required": False,
                        },
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"open\s+(?P<url>https?://\S+?)"
                                r"(?:\s+in\s+(?P<browser>[\w .'-]+))?"
                            )
                        }
                    ],
                    "priority": 140,
                },
                {
                    "id": "system.quit_app",
                    "target": primitive_target,
                    "parameters": [{"name": "app", "type": "string", "required": True}],
                    "routes": [{"pattern": r"close\s+(?P<app>[\w .'-]+)"}],
                    "priority": 110,
                },
                {
                    "id": "system.set_volume",
                    "target": primitive_target,
                    "parameters": [
                        {
                            "name": "volume",
                            "type": "integer",
                            "required": True,
                            "minimum": 0,
                            "maximum": 100,
                        }
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"set\s+the\s+volume\s+to\s+"
                                r"(?P<volume>\d{1,3})"
                            )
                        }
                    ],
                    "priority": 120,
                },
            ]
        )

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
        decision = route_request("Open Spotify.", self.catalog)

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "deterministic catalog action")

    def test_routes_open_url_to_fast_model(self) -> None:
        decision = route_request("Open https://example.com in Arc.", self.catalog)

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "deterministic catalog action")

    def test_routes_volume_change_to_fast_model(self) -> None:
        decision = route_request("Set the volume to 40.", self.catalog)

        self.assertEqual(decision.route, "fast")

    def test_routes_named_spotify_song_to_fast_model(self) -> None:
        decision = route_request(
            "Play Pink and White by Frank Ocean.",
            self.catalog,
        )

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "deterministic catalog action")

    def test_routes_open_playlist_to_fast_model(self) -> None:
        decision = route_request("Play my road trip playlist.", self.catalog)

        self.assertEqual(decision.route, "fast")
        self.assertEqual(decision.reason, "deterministic catalog action")

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

    def test_forces_pause_through_spotify(self) -> None:
        route = deterministic_tool_route("Pause the music.", self.catalog)

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.tool_name, "run_action")
        self.assertEqual(route.arguments["action"], "music.pause")
        self.assertEqual(
            json.loads(route.arguments["arguments_json"]),
            {},
        )

    def test_forces_named_song_through_spotify(self) -> None:
        route = deterministic_tool_route(
            "Play Pink and White by Frank Ocean on Spotify.",
            self.catalog,
        )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(
            json.loads(route.arguments["arguments_json"]),
            {
                "query": "Pink and White by Frank Ocean",
            },
        )

    def test_play_this_song_resumes_spotify_instead_of_searching(self) -> None:
        route = deterministic_tool_route("Play this song.", self.catalog)

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(
            json.loads(route.arguments["arguments_json"]),
            {},
        )

    def test_forces_named_playlist_through_spotify(self) -> None:
        route = deterministic_tool_route(
            "Play my road trip playlist.",
            self.catalog,
        )

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(
            json.loads(route.arguments["arguments_json"]),
            {
                "playlist": "road trip",
            },
        )

    def test_forces_quit_app_through_native_control(self) -> None:
        route = deterministic_tool_route("Close Spotify.", self.catalog)

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.tool_name, "run_action")
        self.assertEqual(
            route.arguments,
            {
                "action": "system.quit_app",
                "arguments_json": '{"app": "Spotify"}',
            },
        )

    def test_does_not_force_multi_step_analysis(self) -> None:
        route = deterministic_tool_route(
            "Open VS Code and analyze the wake detector.",
            self.catalog,
        )

        self.assertIsNone(route)


if __name__ == "__main__":
    unittest.main()
