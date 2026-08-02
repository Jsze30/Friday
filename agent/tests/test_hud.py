from __future__ import annotations

import json
import unittest

from hud import HUD_TOPIC, HudPublisher


class FakeParticipant:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, text: str, *, topic: str, **_kwargs) -> None:
        self.sent.append((topic, text))


class HudPublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_ordered_structured_turn_events(self) -> None:
        participant = FakeParticipant()
        publisher = HudPublisher(participant)  # type: ignore[arg-type]
        publisher.start()

        turn_id = publisher.begin_turn()
        publisher.emit(
            "transcript",
            role="user",
            text="Open that project",
            isFinal=True,
        )
        await publisher.aclose()

        self.assertEqual(
            [topic for topic, _ in participant.sent], [HUD_TOPIC, HUD_TOPIC]
        )
        events = [json.loads(value) for _, value in participant.sent]
        self.assertEqual(
            [event["type"] for event in events], ["turn_started", "transcript"]
        )
        self.assertEqual(events[0]["turnId"], turn_id)
        self.assertEqual(events[0]["sessionId"], events[1]["sessionId"])
        self.assertEqual(events[1]["text"], "Open that project")
        self.assertIn("elapsedMs", events[1])


if __name__ == "__main__":
    unittest.main()
