from __future__ import annotations

import asyncio
import socket
import unittest
import urllib.parse
from unittest.mock import MagicMock, patch

from src.capabilities.base import CapabilityRequest
from src.capabilities.spotify import (
    SPOTIFY_SCOPES,
    SpotifyClient,
    SpotifyProvider,
)


class MemoryTokenStore:
    def __init__(self, token: dict | None = None) -> None:
        self.token = token

    async def load(self) -> dict | None:
        return self.token

    async def save(self, token: dict) -> None:
        self.token = token


class FakeSpotifyClient:
    configured = True

    def __init__(self) -> None:
        self.play_queries: list[str | None] = []
        self.opened_playlists: list[str] = []
        self.authorization_calls = 0

    async def connected(self) -> bool:
        return True

    async def begin_authorization(self) -> dict:
        self.authorization_calls += 1
        return {
            "authorizationRequired": True,
            "authorizationUrl": "https://accounts.spotify.com/authorize",
        }

    async def shutdown(self) -> None:
        return None

    async def playback(self) -> dict:
        return {
            "is_playing": True,
            "progress_ms": 1_000,
            "item": {
                "name": "Pink + White",
                "artists": [{"name": "Frank Ocean"}],
                "uri": "spotify:track:123",
            },
        }

    async def play(self, query: str | None = None) -> dict:
        self.play_queries.append(query)
        return {
            "name": "Pink + White",
            "artist": "Frank Ocean",
            "artists": ["Frank Ocean"],
            "uri": "spotify:track:123",
            "url": "https://open.spotify.com/track/123",
        }

    async def pause(self) -> None:
        return None

    async def list_playlists(
        self,
        *,
        query: str | None = None,
        max_results: int = 40,
    ) -> list[dict]:
        playlists = [
            {
                "id": "playlist-1",
                "name": "Road Trip",
                "uri": "spotify:playlist:playlist-1",
                "itemCount": 12,
            },
            {
                "id": "playlist-2",
                "name": "Study",
                "uri": "spotify:playlist:playlist-2",
                "itemCount": 20,
            },
        ]
        if query:
            playlists = [
                playlist
                for playlist in playlists
                if query.casefold() in playlist["name"].casefold()
            ]
        return playlists[:max_results]

    async def playlist_items(
        self,
        query: str,
        *,
        max_items: int = 40,
    ) -> dict:
        return {
            "playlist": {
                "id": "playlist-1",
                "name": query,
                "uri": "spotify:playlist:playlist-1",
            },
            "items": [
                {
                    "name": "Song",
                    "artist": "Artist",
                    "uri": "spotify:track:1",
                }
            ][:max_items],
            "total": 1,
            "truncated": False,
        }

    async def open_playlist(self, query: str) -> dict:
        self.opened_playlists.append(query)
        return {
            "id": "playlist-1",
            "name": query,
            "uri": "spotify:playlist:playlist-1",
        }

    async def play_playlist(self, query: str) -> dict:
        return {
            "id": "playlist-1",
            "name": query,
            "uri": "spotify:playlist:playlist-1",
        }


def unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class SpotifyTests(unittest.IsolatedAsyncioTestCase):
    def test_declares_fast_actions_for_every_supported_operation(self) -> None:
        action_ids = {action.action_id for action in SpotifyProvider.info.actions}

        self.assertEqual(
            action_ids,
            {
                "music.connect",
                "music.pause",
                "music.resume",
                "music.next",
                "music.previous",
                "music.status",
                "music.list_playlists",
                "music.playlist_tracks",
                "music.open_playlist",
                "music.play_playlist",
                "music.queue",
                "music.shuffle",
                "music.repeat",
                "music.volume",
                "music.play",
            },
        )

    async def test_whitespace_only_api_response_is_empty(self) -> None:
        client = SpotifyClient(
            "client-id",
            "http://127.0.0.1:43821/spotify/callback",
            token_store=MemoryTokenStore(),  # type: ignore[arg-type]
        )
        response = MagicMock()
        response.read.return_value = b" \n"
        response_context = MagicMock()
        response_context.__enter__.return_value = response

        with patch(
            "src.capabilities.spotify.urllib.request.urlopen",
            return_value=response_context,
        ):
            value = await client._request_json(
                "PUT",
                "https://api.spotify.com/v1/me/player/play",
            )

        self.assertIsNone(value)

    async def test_pkce_authorization_contains_no_client_secret(self) -> None:
        opened: list[str] = []
        port = unused_port()
        client = SpotifyClient(
            "client-id",
            f"http://127.0.0.1:{port}/spotify/callback",
            token_store=MemoryTokenStore(),  # type: ignore[arg-type]
            browser_opener=lambda url: not opened.append(url),
        )

        result = await client.begin_authorization()
        parsed = urllib.parse.urlsplit(result["authorizationUrl"])
        parameters = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(parameters["client_id"], ["client-id"])
        self.assertEqual(parameters["code_challenge_method"], ["S256"])
        self.assertNotIn("client_secret", parameters)
        self.assertEqual(opened, [result["authorizationUrl"]])
        await client.shutdown()

    async def test_callback_server_closes_without_blocking_shutdown(self) -> None:
        port = unused_port()
        client = SpotifyClient(
            "client-id",
            f"http://127.0.0.1:{port}/spotify/callback",
            token_store=MemoryTokenStore(),  # type: ignore[arg-type]
            browser_opener=lambda _url: True,
        )
        await client.begin_authorization()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /spotify/callback?state=wrong HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()

        self.assertIn(b"400 Bad Request", response)
        await asyncio.wait_for(client.shutdown(), timeout=1)

    async def test_existing_token_requires_new_playlist_scopes(self) -> None:
        port = unused_port()
        incomplete = SpotifyClient(
            "client-id",
            f"http://127.0.0.1:{port}/spotify/callback",
            token_store=MemoryTokenStore(
                {
                    "access_token": "token",
                    "refresh_token": "refresh",
                    "scope": "user-read-playback-state",
                }
            ),  # type: ignore[arg-type]
            browser_opener=lambda _url: True,
        )
        complete = SpotifyClient(
            "client-id",
            f"http://127.0.0.1:{port}/spotify/callback",
            token_store=MemoryTokenStore(
                {
                    "access_token": "token",
                    "refresh_token": "refresh",
                    "scope": " ".join(SPOTIFY_SCOPES),
                }
            ),  # type: ignore[arg-type]
            browser_opener=lambda _url: True,
        )

        self.assertFalse(await incomplete.connected())
        self.assertTrue(await complete.connected())

    async def test_named_track_is_sent_to_spotify_search_and_play(self) -> None:
        client = FakeSpotifyClient()
        provider = SpotifyProvider(client=client)  # type: ignore[arg-type]

        async def progress(_phase: str, _message: str) -> None:
            return None

        result = await provider.execute(
            CapabilityRequest(
                capability="music",
                goal="Play Pink and White by Frank Ocean",
                inputs={
                    "action": "play",
                    "query": "Pink + White by Frank Ocean",
                },
                permission="low_risk_write",
            ),
            progress,
        )

        self.assertEqual(
            client.play_queries,
            ["Pink + White by Frank Ocean"],
        )
        self.assertIn("Frank Ocean", result.summary)
        self.assertTrue(result.data["verified"])

    async def test_play_retries_when_spotify_does_not_confirm_playback(self) -> None:
        class RetryClient(FakeSpotifyClient):
            async def playback(self) -> dict:
                value = await super().playback()
                value["is_playing"] = len(self.play_queries) > 1
                return value

        client = RetryClient()
        provider = SpotifyProvider(client=client)  # type: ignore[arg-type]
        phases: list[str] = []

        async def progress(phase: str, _message: str) -> None:
            phases.append(phase)

        with patch("src.capabilities.spotify.PLAYBACK_VERIFY_ATTEMPTS", 1):
            result = await provider.execute(
                CapabilityRequest(
                    capability="music",
                    goal="Play Pink and White by Frank Ocean",
                    inputs={"action": "play", "query": "Pink + White"},
                    permission="low_risk_write",
                ),
                progress,
            )

        self.assertEqual(client.play_queries, ["Pink + White", "Pink + White"])
        self.assertIn("retry", phases)
        self.assertTrue(result.data["verified"])

    async def test_pause_requires_spotify_to_report_paused(self) -> None:
        class PausedClient(FakeSpotifyClient):
            async def playback(self) -> dict:
                value = await super().playback()
                value["is_playing"] = False
                return value

        provider = SpotifyProvider(client=PausedClient())  # type: ignore[arg-type]

        async def progress(_phase: str, _message: str) -> None:
            return None

        result = await provider.execute(
            CapabilityRequest(
                capability="music",
                goal="Pause the music",
                inputs={"action": "pause"},
                permission="low_risk_write",
            ),
            progress,
        )

        self.assertTrue(result.data["verified"])
        self.assertFalse(result.data["playback"]["is_playing"])

    async def test_status_returns_current_track(self) -> None:
        provider = SpotifyProvider(
            client=FakeSpotifyClient(),  # type: ignore[arg-type]
        )

        async def progress(_phase: str, _message: str) -> None:
            return None

        result = await provider.execute(
            CapabilityRequest(
                capability="music",
                goal="What is playing?",
                inputs={"action": "status"},
                permission="low_risk_write",
            ),
            progress,
        )

        self.assertEqual(result.data["track"]["name"], "Pink + White")
        self.assertTrue(result.data["playing"])

    async def test_lists_playlists(self) -> None:
        provider = SpotifyProvider(
            client=FakeSpotifyClient(),  # type: ignore[arg-type]
        )

        async def progress(_phase: str, _message: str) -> None:
            return None

        result = await provider.execute(
            CapabilityRequest(
                capability="music",
                goal="List my Spotify playlists",
                inputs={"action": "list_playlists"},
                permission="low_risk_write",
            ),
            progress,
        )

        self.assertEqual(
            [playlist["name"] for playlist in result.data["playlists"]],
            ["Road Trip", "Study"],
        )

    async def test_opens_playlist_by_spoken_name(self) -> None:
        client = FakeSpotifyClient()
        provider = SpotifyProvider(client=client)  # type: ignore[arg-type]

        async def progress(_phase: str, _message: str) -> None:
            return None

        result = await provider.execute(
            CapabilityRequest(
                capability="music",
                goal="Open my road trip playlist",
                inputs={
                    "action": "open_playlist",
                    "playlist": "Road Trip",
                },
                permission="low_risk_write",
            ),
            progress,
        )

        self.assertEqual(client.opened_playlists, ["Road Trip"])
        self.assertIn("Road Trip", result.summary)


if __name__ == "__main__":
    unittest.main()
