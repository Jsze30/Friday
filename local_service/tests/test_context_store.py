from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.context_store import ContextStore


class ContextStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ContextStore(Path(self.temporary.name) / "context.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reference_memory_survives_store_recreation(self) -> None:
        self.store.remember_reference("the project", "Friday", kind="project")

        reopened = ContextStore(self.store.path)

        self.assertEqual(reopened.list_references()[0]["target"], "Friday")

    def test_saved_alias_is_resolved_from_natural_request(self) -> None:
        self.store.remember_reference("the project", "Friday", kind="project")

        result = self.store.resolve("Open the project")

        self.assertEqual(
            result["resolutions"][0],
            {
                "phrase": "the project",
                "target": "Friday",
                "kind": "project",
                "source": "saved memory",
                "confidence": 1.0,
            },
        )

    def test_current_document_resolves_this_file(self) -> None:
        result = self.store.resolve(
            "Explain this file",
            {"currentDocument": "file:///tmp/Friday%20Notes.md"},
        )

        self.assertEqual(result["resolutions"][0]["target"], "/tmp/Friday Notes.md")
        self.assertEqual(result["resolutions"][0]["source"], "active document")

    def test_saved_reference_overrides_live_project_guess(self) -> None:
        self.store.remember_reference("the project", "Friday", kind="project")

        result = self.store.resolve(
            "Open the project",
            {"project": {"name": "Other", "path": "/tmp/other"}},
        )

        self.assertEqual(len(result["resolutions"]), 1)
        self.assertEqual(result["resolutions"][0]["target"], "Friday")

    def test_project_memory_is_canonicalized_to_current_project_path(self) -> None:
        self.store.resolve(
            "When I say the project, I mean Friday",
            {"project": {"name": "friday", "path": "/tmp/friday"}},
        )

        memory = self.store.remember_reference("the project", "Friday")

        self.assertEqual(memory["kind"], "project")
        self.assertEqual(memory["target"], "/tmp/friday")
        self.assertEqual(memory["metadata"]["label"], "Friday")

    def test_forget_reference_removes_alias(self) -> None:
        self.store.remember_reference("the project", "Friday")

        self.assertTrue(self.store.forget_reference("THE PROJECT"))
        self.assertEqual(self.store.list_references(), [])
        self.assertFalse(
            any(
                item["name"] == "Friday"
                for item in self.store.list_memories(kind="entity")
            )
        )

    def test_graph_fact_keeps_provenance_and_is_searchable(self) -> None:
        relationship = self.store.remember_fact(
            "Jason",
            "works_on",
            "Friday",
            subject_kind="person",
            object_kind="project",
        )

        memories = self.store.search_memories("Jason Friday")

        self.assertEqual(relationship["source"], "user")
        self.assertEqual(relationship["confidence"], 1.0)
        self.assertTrue(any(item["id"] == relationship["id"] for item in memories))

    def test_profile_preferences_are_imported_and_retrieved(self) -> None:
        self.store.import_profile(
            {"facts": {"preferred_browser": "Arc", "temperature_unit": "Fahrenheit"}}
        )

        result = self.store.resolve("Which browser do I prefer?")

        self.assertEqual(result["preferences"][0]["value"], "Arc")

    def test_final_transcript_is_recorded_once_and_mentions_are_resolved(self) -> None:
        self.store.upsert_entity("person", "Sarah Chen")
        payload = {
            "type": "transcript",
            "role": "user",
            "text": "I just spoke with Sarah Chen about Friday",
            "isFinal": True,
            "sessionId": "session-1",
            "turnId": "turn-1",
        }

        first = self.store.record_client_event(payload)
        second = self.store.record_client_event(payload)
        result = self.store.resolve("Send her this file", session_id="session-1")

        self.assertEqual(first["id"], second["id"])
        person = next(item for item in result["resolutions"] if item["kind"] == "person")
        self.assertEqual(person["target"], "Sarah Chen")
        self.assertEqual(person["source"], "recent conversation")

    def test_working_context_builds_entities_timeline_and_deduplicates_snapshot(self) -> None:
        working = {
            "currentApplication": {"name": "Visual Studio Code", "bundleId": "com.microsoft.VSCode"},
            "currentDocument": "/tmp/friday/vision.md",
            "project": {"name": "Friday", "path": "/tmp/friday"},
        }

        self.store.resolve("Explain this file", working)
        self.store.resolve("Explain this file", working)

        memories = self.store.list_memories(kind="entity")
        timeline = self.store.search_timeline()
        self.assertTrue(any(item["kind"] == "file" for item in memories))
        self.assertTrue(any(item["kind"] == "project" for item in memories))
        changes = [item for item in timeline if item["type"] == "context.activity_changed"]
        self.assertEqual(len(changes), 1)

    def test_retrieved_context_has_a_hard_character_budget(self) -> None:
        for index in range(30):
            self.store.record_event(
                "conversation.turn",
                f"Friday architecture discussion {index} " + ("detail " * 100),
                fingerprint=f"event-{index}",
            )

        result = self.store.resolve(
            "What did we discuss about Friday architecture?",
            {"computerPerception": {"visibleText": "screen " * 2_000}},
            max_characters=2_500,
        )

        encoded = json.dumps(result, separators=(",", ":"))
        self.assertLessEqual(len(encoded), 2_500)
        self.assertLessEqual(len(result["timeline"]), 6)

    def test_retention_removes_expired_low_value_events(self) -> None:
        expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        event = self.store.record_event(
            "context.activity_changed",
            "Old activity",
            expires_at=expired,
            fingerprint="expired",
        )

        removed = self.store.run_retention()

        self.assertEqual(removed["expiredEvents"], 1)
        self.assertFalse(
            any(item["id"] == event["id"] for item in self.store.search_timeline())
        )

    def test_retention_compacts_old_conversation_turns(self) -> None:
        occurred_at = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        for index in range(4):
            self.store.record_event(
                "conversation.turn",
                f"User: discussed context topic {index}",
                data={"role": "user", "text": f"context topic {index}"},
                occurred_at=occurred_at,
                session_id="old-session",
                fingerprint=f"old-turn-{index}",
            )

        removed = self.store.run_retention()
        timeline = self.store.search_timeline("context topic")

        self.assertEqual(removed["compactedConversationTurns"], 4)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0]["type"], "conversation.summary")

    def test_forget_memory_soft_deletes_a_relationship(self) -> None:
        relationship = self.store.remember_fact("Jason", "uses", "Friday")

        self.assertTrue(self.store.forget_memory(relationship["id"]))
        self.assertFalse(
            any(
                item["id"] == relationship["id"]
                for item in self.store.list_memories(kind="relationship")
            )
        )


if __name__ == "__main__":
    unittest.main()
