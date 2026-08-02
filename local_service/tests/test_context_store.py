from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
