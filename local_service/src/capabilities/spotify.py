from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from typing import Any

import keyring

from .base import (
    ActionDefinition,
    ActionParameter,
    ActionRoute,
    CapabilityProvider,
    CapabilityRequest,
    CapabilityResult,
    ProgressCallback,
    ProviderFailed,
    ProviderInfo,
)

SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_ACCOUNTS_BASE = "https://accounts.spotify.com"
SPOTIFY_SCOPES = (
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative",
)
KEYCHAIN_SERVICE = "com.friday.spotify.oauth"
KEYCHAIN_ACCOUNT = "spotify"
AUTHORIZATION_TIMEOUT_SECONDS = 5 * 60
HTTP_TIMEOUT_SECONDS = 10


class SpotifyAuthorizationRequired(ProviderFailed):
    pass


class SpotifyHTTPError(ProviderFailed):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class SpotifyTokenStore:
    async def load(self) -> dict[str, Any] | None:
        try:
            raw = await asyncio.to_thread(
                keyring.get_password,
                KEYCHAIN_SERVICE,
                KEYCHAIN_ACCOUNT,
            )
        except Exception as error:
            raise ProviderFailed(
                "Could not read Spotify credentials from macOS Keychain."
            ) from error
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    async def save(self, token: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(
                keyring.set_password,
                KEYCHAIN_SERVICE,
                KEYCHAIN_ACCOUNT,
                json.dumps(token, separators=(",", ":")),
            )
        except Exception as error:
            raise ProviderFailed(
                "Could not save Spotify credentials in macOS Keychain."
            ) from error


class SpotifyClient:
    def __init__(
        self,
        client_id: str | None,
        redirect_uri: str,
        *,
        token_store: SpotifyTokenStore | None = None,
        browser_opener: Callable[[str], bool] | None = None,
    ) -> None:
        self.client_id = (client_id or "").strip()
        self.redirect_uri = redirect_uri.strip()
        self._token_store = token_store or SpotifyTokenStore()
        self._browser_opener = browser_opener or (
            lambda url: webbrowser.open(url, new=2)
        )
        self._authorization_lock = asyncio.Lock()
        self._callback_server: asyncio.Server | None = None
        self._authorization_url: str | None = None
        self._authorization_state: str | None = None
        self._code_verifier: str | None = None
        self._authorization_timeout: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    async def connected(self) -> bool:
        token = await self._token_store.load()
        return bool(
            token
            and (
                str(token.get("refresh_token") or "").strip()
                or str(token.get("access_token") or "").strip()
            )
            and _has_required_scopes(token)
        )

    async def begin_authorization(self) -> dict[str, Any]:
        if not self.configured:
            raise ProviderFailed("Spotify Client ID is not configured.")
        async with self._authorization_lock:
            if self._callback_server and self._authorization_url:
                authorization_url = self._authorization_url
            else:
                parsed = urllib.parse.urlsplit(self.redirect_uri)
                if (
                    parsed.scheme != "http"
                    or parsed.hostname not in {"127.0.0.1", "::1"}
                    or parsed.port is None
                    or not parsed.path
                ):
                    raise ProviderFailed(
                        "Spotify redirect URI must use an explicit loopback port."
                    )
                self._authorization_state = secrets.token_urlsafe(24)
                self._code_verifier = secrets.token_urlsafe(64)
                challenge = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(self._code_verifier.encode()).digest()
                    )
                    .rstrip(b"=")
                    .decode()
                )
                try:
                    self._callback_server = await asyncio.start_server(
                        self._handle_callback,
                        host=parsed.hostname,
                        port=parsed.port,
                    )
                except OSError as error:
                    raise ProviderFailed(
                        f"Spotify login callback port {parsed.port} is unavailable."
                    ) from error
                parameters = urllib.parse.urlencode(
                    {
                        "client_id": self.client_id,
                        "response_type": "code",
                        "redirect_uri": self.redirect_uri,
                        "scope": " ".join(SPOTIFY_SCOPES),
                        "code_challenge_method": "S256",
                        "code_challenge": challenge,
                        "state": self._authorization_state,
                    }
                )
                authorization_url = f"{SPOTIFY_ACCOUNTS_BASE}/authorize?{parameters}"
                self._authorization_url = authorization_url
                self._authorization_timeout = asyncio.create_task(
                    self._expire_authorization(),
                    name="friday-spotify-auth-timeout",
                )

        opened = await asyncio.to_thread(
            self._browser_opener,
            authorization_url,
        )
        return {
            "authorizationRequired": True,
            "authorizationUrl": authorization_url,
            "browserOpened": bool(opened),
            "expiresInSeconds": AUTHORIZATION_TIMEOUT_SECONDS,
        }

    async def shutdown(self) -> None:
        await self._close_callback_server()

    async def playback(self) -> dict[str, Any] | None:
        value = await self._api_request("GET", "/me/player")
        return value if isinstance(value, dict) else None

    async def play(self, query: str | None = None) -> dict[str, Any]:
        device_id = await self._ensure_device()
        if query:
            track = await self.search_track(query)
            await self._api_request(
                "PUT",
                "/me/player/play",
                query={"device_id": device_id},
                body={"uris": [track["uri"]]},
            )
            return track
        await self._api_request(
            "PUT",
            "/me/player/play",
            query={"device_id": device_id},
        )
        return {}

    async def pause(self) -> None:
        device_id = await self._ensure_device()
        await self._api_request(
            "PUT",
            "/me/player/pause",
            query={"device_id": device_id},
        )

    async def next(self) -> None:
        device_id = await self._ensure_device()
        await self._api_request(
            "POST",
            "/me/player/next",
            query={"device_id": device_id},
        )

    async def previous(self) -> None:
        device_id = await self._ensure_device()
        await self._api_request(
            "POST",
            "/me/player/previous",
            query={"device_id": device_id},
        )

    async def queue(self, query: str) -> dict[str, Any]:
        track = await self.search_track(query)
        device_id = await self._ensure_device()
        await self._api_request(
            "POST",
            "/me/player/queue",
            query={"uri": track["uri"], "device_id": device_id},
        )
        return track

    async def set_shuffle(self, enabled: bool) -> None:
        device_id = await self._ensure_device()
        await self._api_request(
            "PUT",
            "/me/player/shuffle",
            query={
                "state": str(enabled).lower(),
                "device_id": device_id,
            },
        )

    async def set_repeat(self, mode: str) -> None:
        if mode not in {"off", "context", "track"}:
            raise ProviderFailed("repeat mode must be off, context, or track")
        device_id = await self._ensure_device()
        await self._api_request(
            "PUT",
            "/me/player/repeat",
            query={"state": mode, "device_id": device_id},
        )

    async def set_volume(self, volume: int) -> None:
        if not 0 <= volume <= 100:
            raise ProviderFailed("Spotify volume must be from 0 to 100.")
        device_id = await self._ensure_device()
        await self._api_request(
            "PUT",
            "/me/player/volume",
            query={
                "volume_percent": str(volume),
                "device_id": device_id,
            },
        )

    async def search_track(self, query: str) -> dict[str, Any]:
        value = await self._api_request(
            "GET",
            "/search",
            query={"q": query, "type": "track", "limit": "5"},
        )
        tracks = (
            value.get("tracks", {}).get("items", []) if isinstance(value, dict) else []
        )
        track = next(
            (item for item in tracks if isinstance(item, dict)),
            None,
        )
        if not track:
            raise ProviderFailed(f"Spotify could not find a track for {query}.")
        artists = [
            str(artist.get("name"))
            for artist in track.get("artists", [])
            if isinstance(artist, dict) and artist.get("name")
        ]
        return {
            "name": str(track.get("name") or query),
            "artists": artists,
            "artist": ", ".join(artists),
            "uri": str(track.get("uri") or ""),
            "url": str((track.get("external_urls") or {}).get("spotify") or ""),
        }

    async def list_playlists(
        self,
        *,
        query: str | None = None,
        max_results: int = 40,
    ) -> list[dict[str, Any]]:
        playlists = await self._playlist_catalog(max_items=300)
        if query:
            needle = _normalized_name(query)
            playlists = [
                playlist
                for playlist in playlists
                if needle in _normalized_name(str(playlist.get("name") or ""))
            ]
        return playlists[: max(1, min(max_results, 50))]

    async def playlist_items(
        self,
        query: str,
        *,
        max_items: int = 40,
    ) -> dict[str, Any]:
        playlist = await self.find_playlist(query)
        limit = max(1, min(max_items, 50))
        value = await self._api_request(
            "GET",
            f"/playlists/{playlist['id']}/items",
            query={
                "limit": str(limit),
                "offset": "0",
                "additional_types": "track,episode",
            },
        )
        raw_items = value.get("items", []) if isinstance(value, dict) else []
        items: list[dict[str, Any]] = []
        for row in raw_items:
            if not isinstance(row, dict):
                continue
            item = row.get("item")
            if not isinstance(item, dict):
                continue
            artists = [
                str(artist.get("name"))
                for artist in item.get("artists", [])
                if isinstance(artist, dict) and artist.get("name")
            ]
            items.append(
                {
                    "name": str(item.get("name") or "Unknown item"),
                    "artist": ", ".join(artists),
                    "artists": artists,
                    "type": str(item.get("type") or "track"),
                    "uri": str(item.get("uri") or ""),
                }
            )
        total = (
            int(value.get("total") or len(items))
            if isinstance(value, dict)
            else len(items)
        )
        return {
            "playlist": playlist,
            "items": items,
            "total": total,
            "truncated": total > len(items),
        }

    async def find_playlist(self, query: str) -> dict[str, Any]:
        needle = _normalized_name(query)
        if not needle:
            raise ProviderFailed("A playlist name is required.")
        playlists = await self._playlist_catalog(max_items=300)
        if not playlists:
            raise ProviderFailed("Spotify returned no playlists.")

        exact = [
            playlist
            for playlist in playlists
            if _normalized_name(str(playlist.get("name") or "")) == needle
        ]
        if len(exact) == 1:
            return exact[0]

        containing = [
            playlist
            for playlist in playlists
            if needle in _normalized_name(str(playlist.get("name") or ""))
            or _normalized_name(str(playlist.get("name") or "")) in needle
        ]
        if len(containing) == 1:
            return containing[0]

        ranked = sorted(
            (
                (
                    difflib.SequenceMatcher(
                        None,
                        needle,
                        _normalized_name(str(playlist.get("name") or "")),
                    ).ratio(),
                    playlist,
                )
                for playlist in playlists
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        best_score, best = ranked[0]
        next_score = ranked[1][0] if len(ranked) > 1 else 0
        if best_score >= 0.65 and best_score - next_score >= 0.08:
            return best

        suggestions = [
            str(playlist.get("name"))
            for _, playlist in ranked[:3]
            if playlist.get("name")
        ]
        detail = ", ".join(suggestions)
        raise ProviderFailed(
            f"I could not confidently match the playlist {query}."
            + (f" Closest matches: {detail}." if detail else "")
        )

    async def open_playlist(self, query: str) -> dict[str, Any]:
        playlist = await self.find_playlist(query)
        await self._open_spotify_uri(str(playlist["uri"]))
        return playlist

    async def play_playlist(self, query: str) -> dict[str, Any]:
        playlist = await self.find_playlist(query)
        device_id = await self._ensure_device()
        await self._api_request(
            "PUT",
            "/me/player/play",
            query={"device_id": device_id},
            body={"context_uri": playlist["uri"]},
        )
        return playlist

    async def _playlist_catalog(
        self,
        *,
        max_items: int,
    ) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        offset = 0
        while len(playlists) < max_items:
            page_limit = min(50, max_items - len(playlists))
            value = await self._api_request(
                "GET",
                "/me/playlists",
                query={"limit": str(page_limit), "offset": str(offset)},
            )
            raw_items = value.get("items", []) if isinstance(value, dict) else []
            if not raw_items:
                break
            for item in raw_items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                item_summary = item.get("items") or item.get("tracks") or {}
                owner = item.get("owner") or {}
                playlists.append(
                    {
                        "id": str(item["id"]),
                        "name": str(item.get("name") or "Untitled playlist"),
                        "uri": str(item.get("uri") or ""),
                        "url": str(
                            (item.get("external_urls") or {}).get("spotify") or ""
                        ),
                        "owner": str(owner.get("display_name") or ""),
                        "public": item.get("public"),
                        "collaborative": bool(item.get("collaborative")),
                        "itemCount": int(item_summary.get("total") or 0),
                    }
                )
            if not isinstance(value, dict) or not value.get("next"):
                break
            offset += len(raw_items)
        return playlists

    async def _ensure_device(self) -> str:
        devices = await self._available_devices()
        if not devices:
            await self._launch_spotify()
            for _ in range(8):
                await asyncio.sleep(0.5)
                devices = await self._available_devices()
                if devices:
                    break
        usable = [
            device
            for device in devices
            if isinstance(device, dict)
            and device.get("id")
            and not device.get("is_restricted")
        ]
        if not usable:
            raise ProviderFailed(
                "Spotify has no controllable playback device. Open Spotify "
                "and start playback once, then try again."
            )
        device = next(
            (item for item in usable if item.get("is_active")),
            usable[0],
        )
        device_id = str(device["id"])
        if not device.get("is_active"):
            await self._api_request(
                "PUT",
                "/me/player",
                body={"device_ids": [device_id], "play": False},
            )
            await asyncio.sleep(0.2)
        return device_id

    async def _available_devices(self) -> list[dict[str, Any]]:
        value = await self._api_request("GET", "/me/player/devices")
        if not isinstance(value, dict):
            return []
        return [item for item in value.get("devices", []) if isinstance(item, dict)]

    async def _launch_spotify(self) -> None:
        await self._open_spotify_uri()

    async def _open_spotify_uri(self, uri: str | None = None) -> None:
        try:
            command = ["/usr/bin/open", "-b", "com.spotify.client"]
            if uri:
                command.append(uri)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=5)
            if process.returncode != 0:
                raise ProviderFailed("Could not open the playlist in Spotify.")
        except (OSError, TimeoutError):
            raise ProviderFailed("Could not open Spotify.")

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._access_token()
        try:
            return await self._request_json(
                method,
                f"{SPOTIFY_API_BASE}{path}",
                token=token,
                query=query,
                body=body,
            )
        except SpotifyHTTPError as error:
            if error.status != 401:
                raise
        token = await self._access_token(force_refresh=True)
        return await self._request_json(
            method,
            f"{SPOTIFY_API_BASE}{path}",
            token=token,
            query=query,
            body=body,
        )

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        token = await self._token_store.load()
        if not token:
            raise SpotifyAuthorizationRequired("Spotify is not connected.")
        if not _has_required_scopes(token):
            raise SpotifyAuthorizationRequired(
                "Spotify needs approval for the new playlist permissions."
            )
        access_token = str(token.get("access_token") or "")
        expires_at = float(token.get("expires_at") or 0)
        if access_token and not force_refresh and expires_at > time.time() + 30:
            return access_token
        refresh_token = str(token.get("refresh_token") or "")
        if not refresh_token:
            raise SpotifyAuthorizationRequired("Spotify needs to reconnect.")
        refreshed = await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            }
        )
        if not refreshed.get("refresh_token"):
            refreshed["refresh_token"] = refresh_token
        if not refreshed.get("scope"):
            refreshed["scope"] = token.get("scope")
        await self._save_token(refreshed)
        return str(refreshed["access_token"])

    async def _handle_callback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = "400 Bad Request"
        title = "Spotify connection failed"
        message = "Friday could not complete the Spotify connection."
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=5,
            )
            request_line = raw.split(b"\r\n", 1)[0].decode(
                "ascii",
                errors="replace",
            )
            parts = request_line.split(" ")
            if len(parts) < 2:
                raise ValueError("invalid callback request")
            target = urllib.parse.urlsplit(parts[1])
            expected_path = urllib.parse.urlsplit(self.redirect_uri).path
            if target.path != expected_path:
                status = "404 Not Found"
                message = "This is not Friday's Spotify callback."
            else:
                parameters = urllib.parse.parse_qs(target.query)
                returned_state = (parameters.get("state") or [""])[0]
                error_value = (parameters.get("error") or [""])[0]
                code = (parameters.get("code") or [""])[0]
                if returned_state != self._authorization_state:
                    message = "The Spotify login state did not match."
                elif error_value:
                    message = "Spotify access was not approved."
                elif not code or not self._code_verifier:
                    message = "Spotify did not return an authorization code."
                else:
                    token = await self._token_request(
                        {
                            "client_id": self.client_id,
                            "grant_type": "authorization_code",
                            "code": code,
                            "redirect_uri": self.redirect_uri,
                            "code_verifier": self._code_verifier,
                        }
                    )
                    await self._save_token(token)
                    status = "200 OK"
                    title = "Spotify connected"
                    message = "Spotify is connected to Friday. You can close this tab."
        except Exception:  # noqa: BLE001
            message = "Friday could not complete the Spotify connection."

        html = (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width">'
            f"<title>{title}</title></head>"
            '<body style="font-family:-apple-system;padding:48px;'
            'max-width:560px;margin:auto">'
            f"<h1>{title}</h1><p>{message}</p></body></html>"
        ).encode()
        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(html)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + html
        writer.write(response)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            asyncio.create_task(
                self._close_callback_server(),
                name="friday-spotify-auth-close",
            )

    async def _expire_authorization(self) -> None:
        try:
            await asyncio.sleep(AUTHORIZATION_TIMEOUT_SECONDS)
            await self._close_callback_server()
        except asyncio.CancelledError:
            return

    async def _close_callback_server(self) -> None:
        server = self._callback_server
        self._callback_server = None
        self._authorization_url = None
        self._authorization_state = None
        self._code_verifier = None
        timeout_task = self._authorization_timeout
        self._authorization_timeout = None
        if timeout_task and timeout_task is not asyncio.current_task():
            timeout_task.cancel()
        if server:
            server.close()
            await server.wait_closed()

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        value = await self._request_json(
            "POST",
            f"{SPOTIFY_ACCOUNTS_BASE}/api/token",
            form=form,
        )
        if not isinstance(value, dict) or not value.get("access_token"):
            raise ProviderFailed("Spotify did not return an access token.")
        return value

    async def _save_token(self, token: dict[str, Any]) -> None:
        stored = dict(token)
        stored["expires_at"] = time.time() + max(
            0, int(token.get("expires_in") or 3600)
        )
        await self._token_store.save(stored)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
    ) -> Any:
        def request() -> Any:
            target = url
            if query:
                target = f"{target}?{urllib.parse.urlencode(query)}"
            headers = {"Accept": "application/json"}
            data: bytes | None = None
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            elif form is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                data = urllib.parse.urlencode(form).encode()
            http_request = urllib.request.Request(
                target,
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    http_request,
                    timeout=HTTP_TIMEOUT_SECONDS,
                ) as response:
                    payload = response.read()
            except urllib.error.HTTPError as error:
                payload = error.read()
                message = _spotify_error_message(payload) or (
                    f"Spotify request failed with status {error.code}."
                )
                raise SpotifyHTTPError(error.code, message) from error
            except urllib.error.URLError as error:
                raise ProviderFailed("Could not reach Spotify.") from error
            # Spotify playback endpoints normally return an empty 204 body,
            # but some HTTP paths can preserve harmless response whitespace.
            if not payload.strip():
                return None
            try:
                return json.loads(payload)
            except json.JSONDecodeError as error:
                raise ProviderFailed("Spotify returned invalid data.") from error

        return await asyncio.to_thread(request)


SPOTIFY_ACTIONS = (
    ActionDefinition(
        action_id="music.connect",
        capability="music",
        operation="connect",
        description="Connect or reconnect the user's Spotify account.",
        routes=(
            ActionRoute(r"(?:connect|reconnect|authorize|log\s+in(?:to)?)\s+spotify"),
        ),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.pause",
        capability="music",
        operation="pause",
        description="Pause Spotify playback.",
        routes=(
            ActionRoute(
                r"(?:pause)(?:\s+(?:the\s+|my\s+)?"
                r"(?:music|song|track|spotify|playback))?"
                r"(?:\s+(?:on|in)\s+spotify)?"
            ),
            ActionRoute(
                r"stop\s+(?:the\s+|my\s+)?"
                r"(?:music|song|track|spotify|playback)"
            ),
        ),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.resume",
        capability="music",
        operation="play",
        description="Resume the current Spotify playback.",
        routes=(
            ActionRoute(
                r"(?:resume|continue)(?:\s+(?:the\s+|my\s+)?"
                r"(?:music|song|track|spotify|playback))?"
                r"(?:\s+(?:on|in)\s+spotify)?"
            ),
            ActionRoute(
                r"play(?:\s+(?:it|music|my\s+music|the\s+music|spotify|"
                r"this\s+(?:song|track)|that\s+(?:song|track)))?"
                r"(?:\s+(?:on|in)\s+spotify)?"
            ),
        ),
        permission="low_risk_write",
        latency_ms=500,
        priority=140,
    ),
    ActionDefinition(
        action_id="music.next",
        capability="music",
        operation="next",
        description="Skip to the next Spotify track.",
        routes=(ActionRoute(r"(?:skip|next)(?:\s+(?:song|track))?"),),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.previous",
        capability="music",
        operation="previous",
        description="Return to the previous Spotify track.",
        routes=(ActionRoute(r"(?:previous|back|go\s+back)(?:\s+(?:song|track))?"),),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.status",
        capability="music",
        operation="status",
        description="Read the current Spotify playback status.",
        routes=(
            ActionRoute(
                r"(?:what(?:'s|\s+is)\s+(?:currently\s+)?playing|"
                r"what\s+(?:song|track)\s+is\s+(?:this|playing)|"
                r"spotify\s+status)"
            ),
        ),
        permission="read_only",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.list_playlists",
        capability="music",
        operation="list_playlists",
        description="List the user's Spotify playlists.",
        routes=(
            ActionRoute(
                r"(?:open|show|list|browse)\s+(?:me\s+)?"
                r"(?:my\s+|the\s+)?(?:spotify\s+)?playlists?"
            ),
        ),
        permission="read_only",
        latency_ms=700,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.playlist_tracks",
        capability="music",
        operation="playlist_tracks",
        description="List the tracks in a named Spotify playlist.",
        parameters=(
            ActionParameter(
                "playlist",
                "string",
                "The spoken playlist name.",
            ),
        ),
        routes=(
            ActionRoute(
                r"(?:(?:what|which)\s+(?:songs|tracks)\s+(?:are\s+)?"
                r"(?:in|on)|(?:show|list)\s+(?:me\s+)?(?:the\s+)?"
                r"(?:songs|tracks)\s+(?:in|on)|look\s+(?:through|in))\s+"
                r"(?:my\s+|the\s+)?(?P<playlist>.+?)\s+playlist"
            ),
        ),
        permission="read_only",
        latency_ms=900,
        priority=150,
    ),
    ActionDefinition(
        action_id="music.open_playlist",
        capability="music",
        operation="open_playlist",
        description="Open a named playlist in the Spotify app.",
        parameters=(
            ActionParameter(
                "playlist",
                "string",
                "The spoken playlist name.",
            ),
        ),
        routes=(
            ActionRoute(r"open\s+(?:my\s+|the\s+)?(?P<playlist>.+?)\s+playlist"),
            ActionRoute(r"open\s+(?:my\s+|the\s+)?playlist\s+(?P<playlist>.+)"),
        ),
        permission="low_risk_write",
        latency_ms=900,
        priority=150,
    ),
    ActionDefinition(
        action_id="music.play_playlist",
        capability="music",
        operation="play_playlist",
        description="Start playing a named Spotify playlist.",
        parameters=(
            ActionParameter(
                "playlist",
                "string",
                "The spoken playlist name.",
            ),
        ),
        routes=(
            ActionRoute(r"play\s+(?:my\s+|the\s+)?(?P<playlist>.+?)\s+playlist"),
            ActionRoute(r"play\s+(?:my\s+|the\s+)?playlist\s+(?P<playlist>.+)"),
        ),
        permission="low_risk_write",
        latency_ms=900,
        priority=150,
    ),
    ActionDefinition(
        action_id="music.queue",
        capability="music",
        operation="queue",
        description="Add a named song to the Spotify queue.",
        parameters=(
            ActionParameter("query", "string", "The song and optional artist."),
        ),
        routes=(
            ActionRoute(
                r"(?:queue|add)\s+(?P<query>.+?)"
                r"(?:\s+to\s+(?:the\s+)?queue)?"
                r"(?:\s+on\s+spotify)?"
            ),
        ),
        permission="low_risk_write",
        latency_ms=900,
        priority=120,
    ),
    ActionDefinition(
        action_id="music.shuffle",
        capability="music",
        operation="shuffle",
        description="Turn Spotify shuffle on or off.",
        parameters=(
            ActionParameter("enabled", "boolean", "Whether shuffle is enabled."),
        ),
        routes=(
            ActionRoute(
                r"(?:turn\s+)?shuffle\s+on",
                {"enabled": True},
            ),
            ActionRoute(
                r"(?:turn\s+)?shuffle\s+off",
                {"enabled": False},
            ),
        ),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.repeat",
        capability="music",
        operation="repeat",
        description="Set the Spotify repeat mode.",
        parameters=(
            ActionParameter(
                "mode",
                "string",
                "Spotify repeat mode.",
                choices=("off", "track", "context"),
            ),
        ),
        routes=(
            ActionRoute(r"(?:set\s+)?repeat\s+off", {"mode": "off"}),
            ActionRoute(
                r"(?:set\s+)?repeat\s+(?:track|song)",
                {"mode": "track"},
            ),
            ActionRoute(
                r"(?:set\s+)?repeat\s+(?:context|playlist)",
                {"mode": "context"},
            ),
        ),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.volume",
        capability="music",
        operation="volume",
        description="Set Spotify's playback volume.",
        parameters=(
            ActionParameter(
                "volume",
                "integer",
                "Exact Spotify volume from 0 to 100.",
                minimum=0,
                maximum=100,
            ),
        ),
        routes=(
            ActionRoute(
                r"(?:set\s+)?spotify\s+volume\s+(?:to\s+)?"
                r"(?P<volume>\d{1,3})(?:\s*%)?"
            ),
        ),
        permission="low_risk_write",
        latency_ms=500,
        priority=130,
    ),
    ActionDefinition(
        action_id="music.play",
        capability="music",
        operation="play",
        description="Find and play a named Spotify song.",
        parameters=(
            ActionParameter("query", "string", "The song and optional artist."),
        ),
        routes=(
            ActionRoute(
                r"play(?:\s+(?:the\s+)?song)?\s+(?P<query>.+?)"
                r"(?:\s+on\s+spotify)?"
            ),
        ),
        permission="low_risk_write",
        latency_ms=900,
        priority=100,
    ),
)


class SpotifyProvider(CapabilityProvider):
    info = ProviderInfo(
        provider_id="spotify-web-api",
        name="Spotify",
        description=(
            "Controls Spotify playback and finds exact tracks through Spotify's API."
        ),
        capabilities=("music",),
        actions=SPOTIFY_ACTIONS,
        permission="low_risk_write",
        priority=100,
        reliability=0.95,
        latency=1,
    )

    def __init__(
        self,
        client_id: str | None = None,
        redirect_uri: str = "http://127.0.0.1:43821/spotify/callback",
        *,
        client: SpotifyClient | None = None,
    ) -> None:
        self.client = client or SpotifyClient(client_id, redirect_uri)

    async def available(self) -> bool:
        return self.client.configured

    async def shutdown(self) -> None:
        await self.client.shutdown()

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        action = _music_action(request)
        await progress("spotify", f"Using Spotify to {action}.")
        if action == "connect":
            if await self.client.connected():
                return CapabilityResult(
                    summary="Spotify is already connected.",
                    data={"connected": True},
                )
            return await self._authorization_result()

        try:
            if action == "status":
                return _playback_result(await self.client.playback())
            if action == "list_playlists":
                query = str(
                    request.inputs.get("playlist") or request.inputs.get("query") or ""
                ).strip()
                playlists = await self.client.list_playlists(
                    query=query or None,
                    max_results=int(request.inputs.get("limit") or 40),
                )
                return CapabilityResult(
                    summary=(
                        f"Found {len(playlists)} Spotify playlists"
                        + (f" matching {query}." if query else ".")
                    ),
                    data={
                        "action": action,
                        "query": query or None,
                        "playlists": playlists,
                    },
                )
            if action == "playlist_tracks":
                query = _playlist_query(request)
                if not query:
                    raise ProviderFailed("A playlist name is required.")
                contents = await self.client.playlist_items(
                    query,
                    max_items=int(request.inputs.get("limit") or 40),
                )
                playlist = contents["playlist"]
                return CapabilityResult(
                    summary=(
                        f"Found {len(contents['items'])} items in {playlist['name']}."
                    ),
                    data={"action": action, **contents},
                )
            if action == "open_playlist":
                query = _playlist_query(request)
                if not query:
                    raise ProviderFailed("A playlist name is required.")
                playlist = await self.client.open_playlist(query)
                return CapabilityResult(
                    summary=f"Opened {playlist['name']} in Spotify.",
                    data={"action": action, "playlist": playlist},
                )
            if action == "play_playlist":
                query = _playlist_query(request)
                if not query:
                    raise ProviderFailed("A playlist name is required.")
                playlist = await self.client.play_playlist(query)
                return CapabilityResult(
                    summary=f"Playing the Spotify playlist {playlist['name']}.",
                    data={"action": action, "playlist": playlist},
                )
            if action == "play":
                query = _music_query(request)
                track = await self.client.play(query or None)
                if track:
                    return CapabilityResult(
                        summary=(f"Playing {track['name']} by {track['artist']}."),
                        data={"action": action, "track": track},
                    )
                return CapabilityResult(
                    summary="Resumed Spotify.",
                    data={"action": action},
                )
            if action == "pause":
                await self.client.pause()
                return CapabilityResult(
                    summary="Paused Spotify.",
                    data={"action": action},
                )
            if action == "next":
                await self.client.next()
                return CapabilityResult(
                    summary="Skipped to the next Spotify track.",
                    data={"action": action},
                )
            if action == "previous":
                await self.client.previous()
                return CapabilityResult(
                    summary="Went to the previous Spotify track.",
                    data={"action": action},
                )
            if action == "queue":
                query = _music_query(request)
                if not query:
                    raise ProviderFailed("A song is required for the Spotify queue.")
                track = await self.client.queue(query)
                return CapabilityResult(
                    summary=(
                        f"Added {track['name']} by {track['artist']} to the queue."
                    ),
                    data={"action": action, "track": track},
                )
            if action == "shuffle":
                enabled = _enabled_value(request, default=True)
                await self.client.set_shuffle(enabled)
                return CapabilityResult(
                    summary=f"Turned Spotify shuffle {'on' if enabled else 'off'}.",
                    data={"action": action, "enabled": enabled},
                )
            if action == "repeat":
                mode = str(request.inputs.get("mode") or "context").casefold()
                await self.client.set_repeat(mode)
                return CapabilityResult(
                    summary=f"Set Spotify repeat to {mode}.",
                    data={"action": action, "mode": mode},
                )
            if action == "volume":
                volume = int(request.inputs.get("volume"))
                await self.client.set_volume(volume)
                return CapabilityResult(
                    summary=f"Set Spotify volume to {volume} percent.",
                    data={"action": action, "volume": volume},
                )
        except SpotifyAuthorizationRequired:
            return await self._authorization_result()
        raise ProviderFailed(f"Unsupported Spotify action: {action}.")

    async def _authorization_result(self) -> CapabilityResult:
        authorization = await self.client.begin_authorization()
        return CapabilityResult(
            summary=(
                "I opened Spotify sign-in. Approve access in the browser, "
                "then ask me to use Spotify again."
            ),
            data=authorization,
        )


def _spotify_error_message(payload: bytes) -> str | None:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or "") or None
    if isinstance(error, str):
        description = value.get("error_description")
        return str(description or error)
    return None


def _music_action(request: CapabilityRequest) -> str:
    supplied = str(request.inputs.get("action") or "").strip().casefold()
    aliases = {
        "current": "status",
        "currently_playing": "status",
        "now_playing": "status",
        "resume": "play",
        "skip": "next",
        "back": "previous",
        "add_to_queue": "queue",
        "set_shuffle": "shuffle",
        "set_repeat": "repeat",
        "set_volume": "volume",
        "playlists": "list_playlists",
        "browse_playlists": "list_playlists",
        "playlist_contents": "playlist_tracks",
        "list_playlist_tracks": "playlist_tracks",
        "open": "open_playlist",
    }
    if supplied:
        return aliases.get(supplied, supplied)
    goal = request.goal.casefold()
    if any(word in goal for word in ("connect", "log in", "sign in", "authorize")):
        return "connect"
    if "playlist" in goal:
        if any(
            phrase in goal
            for phrase in (
                "what songs",
                "which songs",
                "what tracks",
                "which tracks",
                "look through",
                "look in",
                "contents",
            )
        ):
            return "playlist_tracks"
        if re.search(r"\bopen\b", goal):
            return "open_playlist"
        if re.search(r"\bplay\b", goal):
            return "play_playlist"
        if re.search(r"\b(?:list|browse|what|which|show)\b", goal):
            return "list_playlists"
    if "queue" in goal:
        return "queue"
    if "pause" in goal:
        return "pause"
    if "previous" in goal or "go back" in goal:
        return "previous"
    if "skip" in goal or re.search(r"\bnext\b", goal):
        return "next"
    if "shuffle" in goal:
        return "shuffle"
    if "repeat" in goal:
        return "repeat"
    if "volume" in goal:
        return "volume"
    if any(
        phrase in goal
        for phrase in ("what is playing", "what's playing", "current song")
    ):
        return "status"
    if "play" in goal or "resume" in goal:
        return "play"
    return "status"


def _music_query(request: CapabilityRequest) -> str:
    supplied = str(
        request.inputs.get("query") or request.inputs.get("track") or ""
    ).strip()
    artist = str(request.inputs.get("artist") or "").strip()
    if supplied:
        return f"{supplied} {artist}".strip()
    goal = request.goal.strip()
    cleaned = re.sub(
        r"^(?:please\s+)?(?:play|queue|add)\s+",
        "",
        goal,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(?:on\s+spotify|to\s+(?:the\s+)?queue)[.!?]*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if cleaned.casefold() in {"spotify", "music", "the music"}:
        return ""
    return cleaned


def _playlist_query(request: CapabilityRequest) -> str:
    supplied = str(
        request.inputs.get("playlist") or request.inputs.get("query") or ""
    ).strip()
    if supplied:
        return supplied
    goal = request.goal.strip()
    cleaned = re.sub(
        r"^(?:please\s+)?(?:(?:open|play|show|browse|list)\s+|"
        r"(?:what|which)\s+(?:songs|tracks)\s+(?:are\s+)?in\s+|"
        r"look\s+(?:through|in)\s+)",
        "",
        goal,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(?:on|in)\s+spotify[.!?]*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:my\s+|the\s+)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+playlist[.!?]*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if cleaned.casefold() in {"playlists", "playlist"}:
        return ""
    return cleaned.strip()


def _normalized_name(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())


def _has_required_scopes(token: dict[str, Any]) -> bool:
    granted = {scope for scope in str(token.get("scope") or "").split() if scope}
    return set(SPOTIFY_SCOPES).issubset(granted)


def _enabled_value(
    request: CapabilityRequest,
    *,
    default: bool,
) -> bool:
    value = request.inputs.get("enabled")
    if isinstance(value, bool):
        return value
    goal = request.goal.casefold()
    if re.search(r"\b(?:off|disable|disabled)\b", goal):
        return False
    if re.search(r"\b(?:on|enable|enabled)\b", goal):
        return True
    return default


def _playback_result(playback: dict[str, Any] | None) -> CapabilityResult:
    if not playback:
        return CapabilityResult(
            summary="Spotify is not currently playing.",
            data={"playing": False},
        )
    item = playback.get("item")
    if not isinstance(item, dict):
        return CapabilityResult(
            summary="Spotify has no current track.",
            data={"playing": bool(playback.get("is_playing"))},
        )
    artists = [
        str(artist.get("name"))
        for artist in item.get("artists", [])
        if isinstance(artist, dict) and artist.get("name")
    ]
    name = str(item.get("name") or "Unknown track")
    artist = ", ".join(artists)
    playing = bool(playback.get("is_playing"))
    return CapabilityResult(
        summary=(
            f"{'Playing' if playing else 'Paused on'} {name}"
            + (f" by {artist}." if artist else ".")
        ),
        data={
            "playing": playing,
            "track": {
                "name": name,
                "artist": artist,
                "artists": artists,
                "uri": str(item.get("uri") or ""),
            },
            "progressMs": playback.get("progress_ms"),
            "device": playback.get("device"),
            "shuffle": playback.get("shuffle_state"),
            "repeat": playback.get("repeat_state"),
        },
    )


__all__ = [
    "SpotifyClient",
    "SpotifyProvider",
    "SpotifyTokenStore",
]
