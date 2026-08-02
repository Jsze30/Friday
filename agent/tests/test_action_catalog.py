from __future__ import annotations

import unittest

from action_catalog import (
    ActionCatalog,
    action_arguments_need_resolution,
    merge_action_manifests,
)


class ActionCatalogTests(unittest.TestCase):
    def test_merges_provider_and_primitive_actions(self) -> None:
        actions = merge_action_manifests(
            {
                "actions": [
                    {
                        "id": "music.pause",
                        "target": {
                            "kind": "capability",
                            "action": "music.pause",
                        },
                        "parameters": [],
                        "routes": [{"pattern": "pause"}],
                    }
                ]
            },
            [
                {
                    "name": "open_app",
                    "permission": "low_risk_write",
                    "parameters": [
                        {
                            "name": "app",
                            "type": "string",
                            "required": True,
                        }
                    ],
                    "actions": [
                        {
                            "id": "system.open_app",
                            "routes": [{"pattern": (r"open\s+(?P<app>[\w .'-]+)")}],
                        }
                    ],
                }
            ],
        )

        catalog = ActionCatalog(actions)

        self.assertEqual(
            catalog.action_ids,
            ["music.pause", "system.open_app"],
        )
        self.assertEqual(
            catalog.get("system.open_app")["target"],
            {"kind": "primitive", "tool": "open_app"},
        )
        self.assertEqual(
            catalog.get("system.open_app")["permission"],
            "low_risk_write",
        )

    def test_extracts_and_converts_declared_parameters(self) -> None:
        catalog = ActionCatalog(
            [
                {
                    "id": "system.set_volume",
                    "target": {"kind": "primitive", "tool": "set_volume"},
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
                                r"set\s+volume\s+to\s+"
                                r"(?P<volume>\d{1,3})"
                            )
                        }
                    ],
                }
            ]
        )

        match = catalog.match("Friday, please set volume to 42.")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.action_id, "system.set_volume")
        self.assertEqual(match.arguments, {"volume": 42})

    def test_rejects_out_of_range_and_multi_step_commands(self) -> None:
        catalog = ActionCatalog(
            [
                {
                    "id": "system.set_volume",
                    "target": {"kind": "primitive", "tool": "set_volume"},
                    "parameters": [
                        {
                            "name": "volume",
                            "type": "integer",
                            "required": True,
                            "maximum": 100,
                        }
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"set\s+volume\s+to\s+"
                                r"(?P<volume>\d{1,3})"
                            )
                        }
                    ],
                },
                {
                    "id": "system.open_app",
                    "target": {"kind": "primitive", "tool": "open_app"},
                    "parameters": [
                        {
                            "name": "app",
                            "type": "string",
                            "required": True,
                        }
                    ],
                    "routes": [{"pattern": r"open\s+(?P<app>[\w .'-]+)"}],
                },
            ]
        )

        self.assertIsNone(catalog.match("Set volume to 101."))
        self.assertIsNone(catalog.match("Open VS Code and analyze the project."))
        self.assertIsNone(catalog.match("Open Minecraft and press Play."))
        self.assertIsNone(catalog.match("Open Arc and click the first video."))

    def test_priority_resolves_overlapping_integration_routes(self) -> None:
        catalog = ActionCatalog(
            [
                {
                    "id": "music.play",
                    "target": {
                        "kind": "capability",
                        "action": "music.play",
                    },
                    "parameters": [
                        {
                            "name": "query",
                            "type": "string",
                            "required": True,
                        }
                    ],
                    "routes": [{"pattern": r"play\s+(?P<query>.+)"}],
                    "priority": 100,
                },
                {
                    "id": "music.play_playlist",
                    "target": {
                        "kind": "capability",
                        "action": "music.play_playlist",
                    },
                    "parameters": [
                        {
                            "name": "playlist",
                            "type": "string",
                            "required": True,
                        }
                    ],
                    "routes": [{"pattern": (r"play\s+(?P<playlist>.+?)\s+playlist")}],
                    "priority": 150,
                },
            ]
        )

        match = catalog.match("Play road trip playlist.")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.action_id, "music.play_playlist")
        self.assertEqual(match.arguments, {"playlist": "road trip"})

    def test_known_website_route_wins_over_generic_open_app(self) -> None:
        catalog = ActionCatalog(
            [
                {
                    "id": "system.open_application",
                    "target": {"kind": "capability", "action": "open_application"},
                    "parameters": [{"name": "app", "type": "string", "required": True}],
                    "routes": [{"pattern": (r"(?:open|launch)\s+(?P<app>[\w .'-]+)")}],
                    "priority": 190,
                },
                {
                    "id": "browser.open_website",
                    "target": {"kind": "capability", "action": "open_website"},
                    "parameters": [
                        {
                            "name": "destination",
                            "type": "string",
                            "required": True,
                        },
                        {
                            "name": "browser",
                            "type": "string",
                            "required": False,
                        },
                    ],
                    "routes": [
                        {
                            "pattern": (
                                r"open\s+(?:youtube|you\s+tube)"
                                r"(?:\s+in\s+(?P<browser>[\w .'-]+))?"
                            ),
                            "arguments": {"destination": "YouTube"},
                        }
                    ],
                    "priority": 250,
                },
            ]
        )

        match = catalog.match("Open YouTube.")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.action_id, "browser.open_website")
        self.assertEqual(match.arguments, {"destination": "YouTube"})

    def test_fixed_boolean_arguments_are_preserved(self) -> None:
        catalog = ActionCatalog(
            [
                {
                    "id": "music.shuffle",
                    "target": {
                        "kind": "capability",
                        "action": "music.shuffle",
                    },
                    "parameters": [
                        {
                            "name": "enabled",
                            "type": "boolean",
                            "required": True,
                        }
                    ],
                    "routes": [
                        {
                            "pattern": r"(?:turn\s+)?shuffle\s+off",
                            "arguments": {"enabled": False},
                        }
                    ],
                }
            ]
        )

        match = catalog.match("Turn shuffle off.")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.arguments, {"enabled": False})

    def test_only_referential_action_arguments_require_context_resolution(self) -> None:
        self.assertFalse(action_arguments_need_resolution({"app": "Minecraft"}))
        self.assertFalse(action_arguments_need_resolution({"destination": "YouTube"}))
        self.assertTrue(action_arguments_need_resolution({"app": "the app"}))
        self.assertTrue(action_arguments_need_resolution({"query": "that"}))


if __name__ == "__main__":
    unittest.main()
