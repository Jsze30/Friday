from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from .broker import CapabilityBroker
from .computer import ComputerControlProvider
from .providers import (
    CodexProvider,
    FileProvider,
    ResearchProvider,
    configured_command_providers,
)
from .runtime import CapabilityRuntime
from .spotify import SpotifyProvider

log = logging.getLogger("friday.capabilities")
_runtime: CapabilityRuntime | None = None


def load_all() -> CapabilityRuntime:
    global _runtime
    if _runtime is None:
        providers = [
            FileProvider(),
            ResearchProvider(),
            CodexProvider(),
            ComputerControlProvider(),
            SpotifyProvider(
                client_id=settings.spotify_client_id,
                redirect_uri=settings.spotify_redirect_uri,
            ),
            *configured_command_providers(),
        ]
        _runtime = CapabilityRuntime(CapabilityBroker(providers))
        log.info(
            "loaded %d capability providers: %s",
            len(providers),
            [provider.info.provider_id for provider in providers],
        )
    return _runtime


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = load_all()
    operation = payload.get("operation")
    if operation == "list":
        return await runtime.catalog()
    if operation == "start":
        inputs = payload.get("inputs") or {}
        if not isinstance(inputs, dict):
            return {"ok": False, "error": "inputs must be an object"}
        return await runtime.start(
            str(payload.get("capability") or ""),
            str(payload.get("goal") or ""),
            inputs,
        )
    if operation == "action":
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {"ok": False, "error": "arguments must be an object"}
        return await runtime.action(
            str(payload.get("action") or ""),
            str(payload.get("goal") or ""),
            arguments,
        )
    if operation == "status":
        return await runtime.status(
            str(payload.get("taskId") or ""),
            int(payload.get("since") or 0),
        )
    if operation == "cancel":
        return await runtime.cancel(str(payload.get("taskId") or ""))
    if operation == "cancel_all":
        return await runtime.cancel_all()
    return {"ok": False, "error": "unknown capability operation"}


async def shutdown() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.shutdown()
        _runtime = None


__all__ = ["execute", "load_all", "shutdown"]
