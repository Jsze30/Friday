from __future__ import annotations

from .. import profile
from .base import ToolParam, ToolResult, tool


@tool(
    name="remember",
    description=(
        "Save a stable fact about the user (name, preferences, recurring people or "
        "places, long-term goals). Do not use for transient context. The fact persists "
        "across sessions."
    ),
    parameters=[
        ToolParam(
            name="key",
            type="string",
            description="Short identifier for the fact, e.g. 'name' or 'home_city'.",
        ),
        ToolParam(
            name="value",
            type="string",
            description="The fact to remember.",
        ),
    ],
    permission="low_risk_write",
)
async def remember(key: str, value: str) -> ToolResult:
    updated = profile.set_fact(key, value)
    return ToolResult(spoken="saved", data={"profile": updated})
