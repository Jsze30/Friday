from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import profile
from .base import ToolParam, ToolResult, tool


@tool(
    name="get_time",
    description=(
        "Get the current time. If a location/timezone is given, return the time "
        "there; otherwise use the user's saved timezone, or the system local time."
    ),
    parameters=[
        ToolParam(
            name="location",
            type="string",
            description=(
                "Optional IANA timezone (e.g. 'Europe/London') or city name. "
                "Omit to use the user's default."
            ),
            required=False,
        ),
    ],
    permission="read_only",
)
async def get_time(location: str | None = None) -> ToolResult:
    tz_name = _resolve_tz(location)
    try:
        tz = ZoneInfo(tz_name) if tz_name else None
    except ZoneInfoNotFoundError:
        return ToolResult(
            spoken=f"I don't know the timezone for {location}.",
            data={"error": "unknown_timezone", "location": location},
        )

    now = datetime.now(tz)
    spoken_time = now.strftime("%-I:%M %p").lower()
    where = _pretty_place(location, tz_name)
    spoken = f"It's {spoken_time}{f' in {where}' if where else ''}."

    return ToolResult(
        spoken=spoken,
        data={
            "iso": now.isoformat(),
            "timezone": tz_name or "system",
            "hour": now.hour,
            "minute": now.minute,
        },
    )


_CITY_TZ = {
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "tokyo": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
}


def _resolve_tz(location: str | None) -> str | None:
    if location:
        if "/" in location:
            return location
        return _CITY_TZ.get(location.strip().lower(), location)
    facts = profile.load().get("facts", {})
    return facts.get("timezone")


def _pretty_place(location: str | None, tz_name: str | None) -> str:
    if location:
        return location
    if tz_name and "/" in tz_name:
        return tz_name.split("/")[-1].replace("_", " ")
    return ""
