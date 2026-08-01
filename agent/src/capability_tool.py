from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents import RunContext, function_tool, llm

RpcCall = Callable[[str, str], Awaitable[str | None]]
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
FAST_PATH_SECONDS = 0.8
FAST_POLL_SECONDS = 0.2
BACKGROUND_POLL_SECONDS = 0.75


def _decode(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {"ok": False, "error": "the Mac did not respond"}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "the Mac returned invalid JSON"}
    if not isinstance(value, dict):
        return {"ok": False, "error": "the Mac returned an invalid response"}
    return value


def _terminal_result(status: dict[str, Any]) -> str:
    state = status.get("status")
    if state == "succeeded":
        return json.dumps(
            {
                "status": state,
                "provider": status.get("provider"),
                "result": status.get("result"),
                "attempts": status.get("attempts"),
            }
        )
    return json.dumps(
        {
            "status": state or "failed",
            "error": status.get("error") or "The capability task did not finish.",
            "attempts": status.get("attempts"),
        }
    )


def build_capability_tool(
    rpc_call: RpcCall,
    capabilities: list[str] | None = None,
):
    supported = ", ".join(
        capabilities or ["files", "research", "web", "coding", "music"]
    )

    @function_tool(
        name="run_capability",
        description=(
            "Run one intelligent or multi-step capability through Friday's "
            "fastest available provider. Use a registered action instead when "
            "the request has one clear deterministic operation. Available "
            f"capabilities: {supported}. Describe the complete desired outcome "
            "in goal. inputs_json is an optional JSON object containing useful "
            "structured hints. Slow work continues in the background."
        ),
        flags=llm.ToolFlag.CANCELLABLE,
    )
    async def run_capability(
        context: RunContext,
        capability: str,
        goal: str,
        inputs_json: str | None = None,
    ) -> str:
        """Run a high-level capability."""
        try:
            inputs = json.loads(inputs_json) if inputs_json else {}
        except json.JSONDecodeError:
            return "inputs_json must be valid JSON."
        if not isinstance(inputs, dict):
            return "inputs_json must contain a JSON object."

        started = _decode(
            await rpc_call(
                "capability_call",
                json.dumps(
                    {
                        "operation": "start",
                        "capability": capability,
                        "goal": goal,
                        "inputs": inputs,
                    }
                ),
            )
        )
        if not started.get("ok"):
            return str(started.get("error") or "Could not start the capability.")
        task_id = str(started.get("taskId") or "")
        if not task_id:
            return "The local capability runner did not return a task ID."

        last_sequence = 0

        async def poll() -> dict[str, Any]:
            return _decode(
                await rpc_call(
                    "capability_call",
                    json.dumps(
                        {
                            "operation": "status",
                            "taskId": task_id,
                            "since": last_sequence,
                        }
                    ),
                )
            )

        try:
            fast_deadline = time.monotonic() + FAST_PATH_SECONDS
            while time.monotonic() < fast_deadline:
                status = await poll()
                if status.get("status") in TERMINAL_STATUSES:
                    return _terminal_result(status)
                await asyncio.sleep(FAST_POLL_SECONDS)

            await context.update(
                f"I started the {capability} task and will keep working."
            )
            async with context.with_filler(
                "I am still working on that.",
                delay=8,
                interval=15,
                max_steps=2,
            ):
                while True:
                    status = await poll()
                    if not status.get("ok") and not status.get("status"):
                        return str(
                            status.get("error")
                            or "The capability runner stopped responding."
                        )
                    events = status.get("events") or []
                    if events:
                        last_sequence = max(
                            int(event.get("sequence") or 0)
                            for event in events
                            if isinstance(event, dict)
                        )
                        for event in events:
                            if (
                                isinstance(event, dict)
                                and event.get("phase") == "fallback"
                            ):
                                await context.update(str(event.get("message")))
                    if status.get("status") in TERMINAL_STATUSES:
                        return _terminal_result(status)
                    await asyncio.sleep(BACKGROUND_POLL_SECONDS)
        except asyncio.CancelledError:
            await rpc_call(
                "capability_call",
                json.dumps({"operation": "cancel", "taskId": task_id}),
            )
            raise

    return run_capability
