from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from livekit.agents import RunContext, function_tool

from action_catalog import ActionCatalog
from capability_tool import RpcCall, _decode

PrimitiveCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def _primitive_result(envelope: dict[str, Any]) -> str:
    if not envelope.get("ok"):
        return json.dumps(
            {
                "error": envelope.get("error") or "The action failed.",
                "message": envelope.get("spoken"),
                "data": envelope.get("data"),
            }
        )
    return json.dumps(
        {
            key: value
            for key, value in {
                "message": envelope.get("spoken"),
                "data": envelope.get("data"),
            }.items()
            if value is not None
        }
    )


def build_action_tool(
    rpc_call: RpcCall,
    call_primitive: PrimitiveCall,
    catalog: ActionCatalog,
):
    available = catalog.tool_summary()

    @function_tool(
        name="run_action",
        description=(
            "Run one fast deterministic action from Friday's shared action "
            f"catalog. Available actions: {available}. Pass only arguments "
            "declared by that action as one JSON object string."
        ),
    )
    async def run_action(
        context: RunContext,
        action: str,
        arguments_json: str | None = None,
    ) -> str:
        """Run one registered action."""
        manifest = catalog.get(action)
        if manifest is None:
            return f"Unknown action: {action}."
        try:
            arguments = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError:
            return "arguments_json must be valid JSON."
        if not isinstance(arguments, dict):
            return "arguments_json must contain a JSON object."
        normalized = catalog.normalize_arguments(action, arguments)
        if normalized is None:
            return f"Invalid arguments for {action}."
        if manifest.get("permission") != "read_only":
            context.disallow_interruptions()

        target = manifest["target"]
        if target.get("kind") == "primitive":
            tool_name = str(target.get("tool") or "")
            if not tool_name:
                return f"{action} has no primitive target."
            return _primitive_result(await call_primitive(tool_name, normalized))

        response = _decode(
            await rpc_call(
                "capability_call",
                json.dumps(
                    {
                        "operation": "action",
                        "action": action,
                        "goal": f"Run {action}.",
                        "arguments": normalized,
                    }
                ),
            )
        )
        if not response.get("ok"):
            return json.dumps(
                {
                    "error": response.get("error") or f"{action} failed.",
                    "attempts": response.get("attempts"),
                }
            )
        return json.dumps(
            {
                "status": response.get("status"),
                "provider": response.get("provider"),
                "result": response.get("result"),
                "attempts": response.get("attempts"),
            }
        )

    return run_action


__all__ = ["build_action_tool"]
