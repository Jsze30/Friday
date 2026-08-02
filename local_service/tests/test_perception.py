from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from src import perception


class VisualPerceptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_uses_one_image_and_disables_response_storage(self) -> None:
        captured: dict = {}

        def fake_post(payload: dict, api_key: str) -> dict:
            captured["payload"] = payload
            captured["api_key"] = api_key
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The editor shows a missing import error.",
                            }
                        ],
                    }
                ]
            }

        original_key = perception.settings.openai_api_key
        original_model = perception.settings.vision_model
        original_enabled = perception.settings.cloud_visual_analysis
        try:
            perception.settings.openai_api_key = "test-key"
            perception.settings.vision_model = "test-vision-model"
            perception.settings.cloud_visual_analysis = True
            with patch.object(perception, "_post_openai", side_effect=fake_post):
                result = await perception.analyze(
                    {
                        "query": "What is wrong here?",
                        "imageBase64": base64.b64encode(b"jpeg-bytes").decode(),
                        "mimeType": "image/jpeg",
                        "ocrText": "Cannot find symbol Widget",
                        "metadata": {"app": "VS Code"},
                    }
                )
        finally:
            perception.settings.openai_api_key = original_key
            perception.settings.vision_model = original_model
            perception.settings.cloud_visual_analysis = original_enabled

        self.assertTrue(result["ok"])
        self.assertEqual(result["analysis"], "The editor shows a missing import error.")
        self.assertEqual(captured["api_key"], "test-key")
        self.assertFalse(captured["payload"]["store"])
        user_content = captured["payload"]["input"][1]["content"]
        self.assertEqual(user_content[1]["type"], "input_image")
        self.assertTrue(user_content[1]["image_url"].startswith("data:image/jpeg;base64,"))

    async def test_missing_key_keeps_local_perception_available(self) -> None:
        original_key = perception.settings.openai_api_key
        original_enabled = perception.settings.cloud_visual_analysis
        try:
            perception.settings.openai_api_key = None
            perception.settings.cloud_visual_analysis = True
            result = await perception.analyze(
                {
                    "query": "What is this?",
                    "imageBase64": base64.b64encode(b"image").decode(),
                }
            )
        finally:
            perception.settings.openai_api_key = original_key
            perception.settings.cloud_visual_analysis = original_enabled

        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertIn("OPENAI_API_KEY", result["error"])

    async def test_oversized_image_is_rejected_before_network_use(self) -> None:
        original_key = perception.settings.openai_api_key
        original_enabled = perception.settings.cloud_visual_analysis
        try:
            perception.settings.openai_api_key = "test-key"
            perception.settings.cloud_visual_analysis = True
            with self.assertRaises(perception.VisualAnalysisError):
                await perception.analyze(
                    {
                        "query": "Inspect this",
                        "imageBase64": "A" * (((perception.MAX_IMAGE_BYTES * 4) // 3) + 20),
                    }
                )
        finally:
            perception.settings.openai_api_key = original_key
            perception.settings.cloud_visual_analysis = original_enabled

    def test_output_text_parser_rejects_empty_responses(self) -> None:
        with self.assertRaises(perception.VisualAnalysisError):
            perception._extract_output_text({"output": []})


if __name__ == "__main__":
    unittest.main()
