from __future__ import annotations

import secrets
import time
from datetime import timedelta

from livekit import api

from .config import settings


def mint_token() -> dict[str, str | int]:
    room_name = f"{settings.room_prefix}-{int(time.time())}-{secrets.token_hex(3)}"
    identity = f"mac-{secrets.token_hex(6)}"

    grants = api.VideoGrants(
        room=room_name,
        room_join=True,
        room_create=True,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )

    room_config = api.RoomConfiguration(
        agents=[api.RoomAgentDispatch(agent_name=settings.agent_name)],
    )

    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .with_room_config(room_config)
        .with_ttl(timedelta(seconds=settings.token_ttl_seconds))
        .to_jwt()
    )

    return {
        "url": settings.livekit_url,
        "token": token,
        "roomName": room_name,
        "participantIdentity": identity,
        "agentName": settings.agent_name,
    }
