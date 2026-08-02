from __future__ import annotations

import asyncio
import unittest

from livekit.agents import function_tool, llm

from action_catalog import ActionCatalog
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

    def test_turn_context_is_retrieved_and_added_only_to_current_turn(self) -> None:
        events: list[tuple[str, dict]] = []

        async def context_provider(_query: str) -> dict:
            return {
                "workingContext": {"currentWindow": "vision.md - friday"},
                "resolutions": [
                    {
                        "phrase": "this file",
                        "target": "/tmp/vision.md",
                        "source": "active document",
                    }
                ],
            }

        agent = object.__new__(FridayAgent)
        agent._turn_context_provider = context_provider
        agent._hud_event_sink = lambda event, payload: events.append((event, payload))
        agent._current_turn_context = {}
        turn_context = llm.ChatContext()
        message = llm.ChatMessage(role="user", content=["Explain this file"])

        asyncio.run(agent.on_user_turn_completed(turn_context, message))

        self.assertEqual(len(turn_context.items), 1)
        self.assertIn("<working_context>", turn_context.items[0].text_content)
        self.assertIn("vision.md", turn_context.items[0].text_content)
        self.assertLess(turn_context.items[0].created_at, message.created_at)
        turn_context.insert(message)
        self.assertTrue(agent._is_initial_user_inference(turn_context))
        self.assertEqual(events[0][0], "context")
        self.assertEqual(
            agent._current_turn_context["resolutions"][0]["phrase"],
            "this file",
        )

    def test_irrelevant_working_context_keeps_the_preemptive_fast_path(self) -> None:
        async def context_provider(_query: str) -> dict:
            return {
                "workingContext": {
                    "currentApplication": {"name": "Visual Studio Code"}
                },
                "resolutions": [],
            }

        agent = object.__new__(FridayAgent)
        agent._turn_context_provider = context_provider
        agent._hud_event_sink = None
        agent._current_turn_context = {}
        turn_context = llm.ChatContext()
        message = llm.ChatMessage(role="user", content=["Pause Spotify"])

        asyncio.run(agent.on_user_turn_completed(turn_context, message))

        self.assertEqual(turn_context.items, [])
        self.assertEqual(
            agent._current_turn_context["workingContext"]["currentApplication"]["name"],
            "Visual Studio Code",
        )

    def test_resolved_reference_bypasses_the_raw_deterministic_route(self) -> None:
        agent = object.__new__(FridayAgent)
        agent._action_catalog = ActionCatalog(
            [
                {
                    "id": "system.open_app",
                    "target": {"kind": "primitive", "tool": "open_app"},
                    "parameters": [{"name": "app", "type": "string", "required": True}],
                    "routes": [
                        {
                            "pattern": (
                                r"(?:open|launch|focus|activate)\s+"
                                r"(?:the\s+)?(?P<app>[\w .'-]+)"
                            )
                        }
                    ],
                }
            ]
        )
        chat_context = llm.ChatContext()
        chat_context.add_message(role="user", content="Open the project")

        agent._current_turn_context = {}
        raw_route = agent._deterministic_route_for_turn(chat_context)
        self.assertEqual(raw_route.arguments["action"], "system.open_app")

        agent._current_turn_context = {
            "resolutions": [
                {
                    "phrase": "the project",
                    "target": "/tmp/friday",
                    "kind": "project",
                }
            ]
        }
        self.assertIsNone(agent._deterministic_route_for_turn(chat_context))

    def test_reference_memory_action_remains_deterministic(self) -> None:
        agent = object.__new__(FridayAgent)
        agent._action_catalog = ActionCatalog(
            [
                {
                    "id": "context.remember_reference",
                    "target": {
                        "kind": "primitive",
                        "tool": "remember_reference",
                    },
                    "parameters": [
                        {"name": "alias", "type": "string", "required": True},
                        {"name": "target", "type": "string", "required": True},
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"when\s+i\s+say\s+(?P<alias>.+?),?\s+"
                                r"i\s+mean\s+(?P<target>.+?)"
                            )
                        }
                    ],
                }
            ]
        )
        agent._current_turn_context = {
            "resolutions": [
                {
                    "phrase": "the project",
                    "target": "/tmp/friday",
                    "kind": "project",
                }
            ]
        }
        chat_context = llm.ChatContext()
        chat_context.add_message(
            role="user",
            content="When I say the project, I mean Friday",
        )

        route = agent._deterministic_route_for_turn(chat_context)

        self.assertEqual(route.arguments["action"], "context.remember_reference")


if __name__ == "__main__":
    unittest.main()
