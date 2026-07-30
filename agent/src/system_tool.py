from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents import function_tool

PrimitiveCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
SYSTEM_PRIMITIVE_NAMES = {
    "list_apps",
    "open_app",
    "open_url",
    "quit_app",
    "get_volume",
    "set_volume",
    "mute_audio",
}


def _result_json(envelope: dict[str, Any]) -> str:
    if not envelope.get("ok"):
        return json.dumps(
            {
                "error": envelope.get("error") or "The Mac action failed.",
                "message": envelope.get("spoken"),
                "data": envelope.get("data"),
            }
        )
    result = {
        "message": envelope.get("spoken"),
        "data": envelope.get("data"),
        "needsConfirmation": envelope.get("needsConfirmation", False),
        "confirmationId": envelope.get("confirmationId"),
    }
    return json.dumps(
        {key: value for key, value in result.items() if value is not None}
    )


def build_system_tool(call_primitive: PrimitiveCall):
    @function_tool(
        name="control_mac",
        description=(
            "Fast native Mac controls. Use this directly for listing apps, "
            "opening or focusing an app, opening an HTTP or HTTPS URL in a "
            "browser, gracefully quitting an app, reading volume, setting exact "
            "volume, and muting or unmuting. action must be list_apps, open_app, "
            "open_url, quit_app, get_volume, set_volume, or mute_audio. "
            "open_url defaults to Arc. quit_app requires user confirmation."
        ),
    )
    async def control_mac(
        action: str,
        app: str | None = None,
        url: str | None = None,
        browser: str | None = None,
        volume: int | None = None,
        muted: bool | None = None,
        running_only: bool | None = None,
    ) -> str:
        """Control common Mac system functions."""
        if action not in SYSTEM_PRIMITIVE_NAMES:
            return (
                "Unknown Mac action. Use list_apps, open_app, open_url, "
                "quit_app, get_volume, set_volume, or mute_audio."
            )

        arguments: dict[str, Any] = {}
        if action in {"open_app", "quit_app"}:
            if not app:
                return f"app is required for {action}."
            arguments["app"] = app
        elif action == "open_url":
            if not url:
                return "url is required for open_url."
            arguments["url"] = url
            if browser:
                arguments["browser"] = browser
        elif action == "set_volume":
            if volume is None:
                return "volume is required for set_volume."
            if not 0 <= volume <= 100:
                return "volume must be from 0 to 100."
            arguments["volume"] = volume
        elif action == "mute_audio":
            if muted is None:
                return "muted is required for mute_audio."
            arguments["muted"] = muted
        elif action == "list_apps" and running_only is not None:
            arguments["running_only"] = running_only

        return _result_json(await call_primitive(action, arguments))

    return control_mac


__all__ = [
    "SYSTEM_PRIMITIVE_NAMES",
    "build_system_tool",
]
