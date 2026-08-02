from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents import function_tool

CancelWork = Callable[[], Awaitable[dict[str, Any]]]
EventSink = Callable[[str, dict[str, Any]], None]


def build_stop_tool(
    cancel_work: CancelWork,
    event_sink: EventSink | None = None,
):
    @function_tool(
        name="stop_current_work",
        description=(
            "Immediately stop Friday's active actions and background capabilities. "
            "Use only when the user asks Friday to stop, cancel, abort, or never mind."
        ),
    )
    async def stop_current_work() -> str:
        result = await cancel_work()
        if event_sink:
            event_sink(
                "work_stopped",
                {
                    "cancelledCount": result.get("cancelledCount", 0),
                    "ok": bool(result.get("ok")),
                },
            )
        if not result.get("ok"):
            return json.dumps(
                {"status": "stop_failed", "error": result.get("error")}
            )
        return json.dumps(
            {
                "status": "stopped",
                "cancelledCount": result.get("cancelledCount", 0),
                "message": "Stopped.",
            }
        )

    return stop_current_work


__all__ = ["build_stop_tool"]
