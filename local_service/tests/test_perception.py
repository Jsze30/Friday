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
        self.assertTrue(
            user_content[1]["image_url"].startswith("data:image/jpeg;base64,")
        )

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

    async def test_locator_returns_normalized_coordinates_without_storage(self) -> None:
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
                                "text": (
                                    '{"found":true,"x":0.25,"y":0.75,'
                                    '"confidence":0.92,"description":"Play button"}'
                                ),
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
                result = await perception.locate(
                    {
                        "target": "Play button",
                        "imageBase64": base64.b64encode(b"jpeg-bytes").decode(),
                        "mimeType": "image/jpeg",
                        "ocrText": "Play",
                        "metadata": {"app": "Example"},
                    }
                )
        finally:
            perception.settings.openai_api_key = original_key
            perception.settings.vision_model = original_model
            perception.settings.cloud_visual_analysis = original_enabled

        self.assertTrue(result["ok"])
        self.assertTrue(result["found"])
        self.assertEqual(result["x"], 0.25)
        self.assertEqual(result["y"], 0.75)
        self.assertEqual(result["confidence"], 0.92)
        self.assertEqual(captured["api_key"], "test-key")
        self.assertFalse(captured["payload"]["store"])
        self.assertEqual(
            captured["payload"]["input"][1]["content"][1]["detail"], "high"
        )

    async def test_action_verifier_compares_two_images_without_storage(self) -> None:
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
                                "text": (
                                    '{"succeeded":true,"confidence":0.93,'
                                    '"reason":"The requested view opened."}'
                                ),
                            }
                        ],
                    }
                ]
            }

        original_key = perception.settings.openai_api_key
        original_enabled = perception.settings.cloud_visual_analysis
        try:
            perception.settings.openai_api_key = "test-key"
            perception.settings.cloud_visual_analysis = True
            with patch.object(perception, "_post_openai", side_effect=fake_post):
                result = await perception.verify_action(
                    {
                        "target": "Displays",
                        "beforeImageBase64": base64.b64encode(b"before").decode(),
                        "afterImageBase64": base64.b64encode(b"after").decode(),
                        "mimeType": "image/jpeg",
                    }
                )
        finally:
            perception.settings.openai_api_key = original_key
            perception.settings.cloud_visual_analysis = original_enabled

        self.assertTrue(result["succeeded"])
        self.assertEqual(result["confidence"], 0.93)
        self.assertFalse(captured["payload"]["store"])
        image_parts = [
            part
            for part in captured["payload"]["input"][1]["content"]
            if part["type"] == "input_image"
        ]
        self.assertEqual(len(image_parts), 2)

    async def test_locator_escalates_low_confidence_result_to_terra(self) -> None:
        payloads: list[dict] = []

        def fake_post(payload: dict, _api_key: str) -> dict:
            payloads.append(payload)
            confidence = 0.42 if len(payloads) == 1 else 0.94
            return {
                "output_text": (
                    '{"found":true,"x":0.4,"y":0.6,'
                    f'"confidence":{confidence},"description":"Play"}}'
                )
            }

        originals = (
            perception.settings.openai_api_key,
            perception.settings.vision_model,
            perception.settings.vision_reasoning_effort,
            perception.settings.vision_escalation_model,
            perception.settings.vision_escalation_reasoning_effort,
            perception.settings.cloud_visual_analysis,
        )
        try:
            perception.settings.openai_api_key = "test-key"
            perception.settings.vision_model = "gpt-5.4-mini"
            perception.settings.vision_reasoning_effort = "none"
            perception.settings.vision_escalation_model = "gpt-5.6-terra"
            perception.settings.vision_escalation_reasoning_effort = "low"
            perception.settings.cloud_visual_analysis = True
            with patch.object(perception, "_post_openai", side_effect=fake_post):
                result = await perception.locate(
                    {
                        "target": "Play",
                        "imageBase64": base64.b64encode(b"image").decode(),
                    }
                )
        finally:
            (
                perception.settings.openai_api_key,
                perception.settings.vision_model,
                perception.settings.vision_reasoning_effort,
                perception.settings.vision_escalation_model,
                perception.settings.vision_escalation_reasoning_effort,
                perception.settings.cloud_visual_analysis,
            ) = originals

        self.assertEqual(
            [payload["model"] for payload in payloads],
            ["gpt-5.4-mini", "gpt-5.6-terra"],
        )
        self.assertEqual(payloads[0]["reasoning"], {"effort": "none"})
        self.assertEqual(payloads[1]["reasoning"], {"effort": "low"})
        self.assertTrue(result["escalated"])
        self.assertEqual(result["model"], "gpt-5.6-terra")
        self.assertEqual(result["confidence"], 0.94)

    async def test_verifier_keeps_confident_failure_on_fast_model(self) -> None:
        payloads: list[dict] = []

        def fake_post(payload: dict, _api_key: str) -> dict:
            payloads.append(payload)
            return {
                "output_text": (
                    '{"succeeded":false,"confidence":0.92,'
                    '"reason":"A nearby control opened."}'
                )
            }

        originals = (
            perception.settings.openai_api_key,
            perception.settings.vision_model,
            perception.settings.vision_reasoning_effort,
            perception.settings.vision_escalation_model,
            perception.settings.cloud_visual_analysis,
        )
        try:
            perception.settings.openai_api_key = "test-key"
            perception.settings.vision_model = "gpt-5.4-mini"
            perception.settings.vision_reasoning_effort = "none"
            perception.settings.vision_escalation_model = "gpt-5.6-terra"
            perception.settings.cloud_visual_analysis = True
            with patch.object(perception, "_post_openai", side_effect=fake_post):
                result = await perception.verify_action(
                    {
                        "target": "Play",
                        "beforeImageBase64": base64.b64encode(b"before").decode(),
                        "afterImageBase64": base64.b64encode(b"after").decode(),
                    }
                )
        finally:
            (
                perception.settings.openai_api_key,
                perception.settings.vision_model,
                perception.settings.vision_reasoning_effort,
                perception.settings.vision_escalation_model,
                perception.settings.cloud_visual_analysis,
            ) = originals

        self.assertEqual([payload["model"] for payload in payloads], ["gpt-5.4-mini"])
        self.assertFalse(result["succeeded"])
        self.assertFalse(result["escalated"])

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
                        "imageBase64": "A"
                        * (((perception.MAX_IMAGE_BYTES * 4) // 3) + 20),
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
