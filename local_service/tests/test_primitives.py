from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import tools


class PrimitiveToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        tools.load_all()

    def test_registry_contains_only_primitive_kernel(self) -> None:
        self.assertEqual(
            set(tools.REGISTRY),
            {
                "create_directory",
                "fetch_url",
                "forget_latest_memory",
                "forget_memory",
                "forget_reference",
                "inspect_path",
                "list_reference_memories",
                "list_memories",
                "move_path",
                "remember_reference",
                "remember_fact",
                "remember_preference",
                "run_applescript",
                "run_process",
                "search_files",
                "trash_path",
                "web_search",
                "write_file",
            },
        )

    def test_write_executes_immediately(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            destination = Path(temporary) / "written.txt"
            result = asyncio.run(
                tools.execute(
                    "write_file",
                    {"path": str(destination), "content": "hello"},
                )
            )

            self.assertTrue(result["ok"])
            self.assertNotIn("needsConfirmation", result)
            self.assertEqual(destination.read_text(), "hello")

    def test_inspect_path_reads_text(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            source = Path(temporary) / "sample.txt"
            source.write_text("primitive content")

            result = asyncio.run(tools.execute("inspect_path", {"path": str(source)}))

            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["content"], "primitive content")

    def test_fetch_url_blocks_local_addresses(self) -> None:
        result = asyncio.run(
            tools.execute("fetch_url", {"url": "http://127.0.0.1/private"})
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["error"], "fetch_failed")

    def test_inspect_path_blocks_credential_files(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            source = Path(temporary) / ".env.local"
            source.write_text("SECRET=do-not-return")

            result = asyncio.run(tools.execute("inspect_path", {"path": str(source)}))

            self.assertEqual(result["data"]["error"], "sensitive_path")
            self.assertNotIn("do-not-return", str(result))

    def test_write_file_blocks_credential_paths(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            destination = Path(temporary) / ".env.local"
            result = asyncio.run(
                tools.execute(
                    "write_file",
                    {"path": str(destination), "content": "SECRET=no"},
                )
            )

            self.assertEqual(result["data"]["error"], "sensitive_path")
            self.assertFalse(destination.exists())

    def test_run_process_executes_immediately(self) -> None:
        result = asyncio.run(
            tools.execute(
                "run_process",
                {"executable": "false", "arguments": []},
            )
        )

        self.assertTrue(result["ok"])
        self.assertNotIn("needsConfirmation", result)
        self.assertEqual(result["data"]["exitCode"], 1)

    def test_human_downloads_alias_is_resolved(self) -> None:
        result = asyncio.run(tools.execute("inspect_path", {"path": "Downloads"}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["path"], str(Path.home() / "Downloads"))
        self.assertEqual(result["data"]["kind"], "directory")

    def test_search_files_finds_nested_name(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            match = nested / "Friday Notes.txt"
            match.write_text("hello")

            result = asyncio.run(
                tools.execute(
                    "search_files",
                    {"root": str(root), "query": "friday notes"},
                )
            )

            self.assertEqual(result["data"]["matches"][0]["path"], str(match))

    def test_move_path_executes_immediately(self) -> None:
        local_service_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=local_service_root) as temporary:
            source = Path(temporary) / "before.txt"
            destination = Path(temporary) / "after.txt"
            source.write_text("move me")

            result = asyncio.run(
                tools.execute(
                    "move_path",
                    {
                        "source": str(source),
                        "destination": str(destination),
                    },
                )
            )

            self.assertTrue(result["ok"])
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_text(), "move me")

    def test_web_search_returns_structured_results(self) -> None:
        fake = [
            {
                "title": "Friday",
                "url": "https://example.com/friday",
                "snippet": "A result.",
            }
        ]
        with patch("src.tools.primitives._web_search", return_value=fake):
            result = asyncio.run(
                tools.execute("web_search", {"query": "Friday assistant"})
            )

        self.assertEqual(result["data"]["results"], fake)


if __name__ == "__main__":
    unittest.main()
