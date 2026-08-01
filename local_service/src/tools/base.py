from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger("friday.tools")

Permission = Literal["read_only", "low_risk_write", "sensitive"]
ParamType = Literal["string", "integer", "number", "boolean", "array"]


@dataclass
class ToolResult:
    spoken: str | None = None
    data: dict[str, Any] | None = None

    def to_envelope(self, ok: bool = True, error: str | None = None) -> dict[str, Any]:
        return {
            "ok": ok,
            "spoken": self.spoken,
            "data": self.data,
            "error": error,
        }


@dataclass
class ToolParam:
    name: str
    type: ParamType
    description: str = ""
    required: bool = True


@dataclass
class ToolDef:
    name: str
    description: str
    permission: Permission
    parameters: list[ToolParam]
    actions: list[dict[str, Any]]
    handler: Callable[..., Awaitable[ToolResult]]

    def manifest(self) -> dict[str, Any]:
        value = {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                }
                for p in self.parameters
            ],
        }
        if self.actions:
            value["actions"] = self.actions
        return value


REGISTRY: dict[str, ToolDef] = {}


def tool(
    *,
    name: str,
    description: str,
    parameters: list[ToolParam] | None = None,
    permission: Permission = "read_only",
    actions: list[dict[str, Any]] | None = None,
):
    def decorator(fn: Callable[..., Awaitable[ToolResult]]):
        if name in REGISTRY:
            raise ValueError(f"duplicate tool name: {name}")
        REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            permission=permission,
            parameters=parameters or [],
            actions=actions or [],
            handler=fn,
        )
        return fn

    return decorator


async def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "__list__":
        return {
            "ok": True,
            "spoken": None,
            "data": {"tools": [t.manifest() for t in REGISTRY.values()]},
            "error": None,
        }
    tool_def = REGISTRY.get(name)
    if tool_def is None:
        return ToolResult().to_envelope(ok=False, error=f"unknown tool: {name}")
    log.info("tool_call name=%s permission=%s", name, tool_def.permission)
    envelope = await _execute_tool(tool_def, arguments)
    log.info("tool_result name=%s ok=%s", name, envelope.get("ok"))
    return envelope


async def _execute_tool(
    tool_def: ToolDef,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = await tool_def.handler(**(arguments or {}))
    except TypeError as e:
        return ToolResult().to_envelope(ok=False, error=f"bad arguments: {e}")
    except Exception as e:
        log.exception("tool %s failed", tool_def.name)
        return ToolResult().to_envelope(ok=False, error=str(e))
    if not isinstance(result, ToolResult):
        result = ToolResult(spoken=str(result))
    return result.to_envelope()
