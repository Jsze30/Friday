from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

log = logging.getLogger("friday.tools")

Permission = Literal["read_only", "low_risk_write", "sensitive"]
ParamType = Literal["string", "integer", "number", "boolean"]


@dataclass
class ToolResult:
    spoken: str | None = None
    data: dict[str, Any] | None = None
    needs_confirmation: bool = False
    confirmation_id: str | None = None

    def to_envelope(self, ok: bool = True, error: str | None = None) -> dict[str, Any]:
        return {
            "ok": ok,
            "spoken": self.spoken,
            "data": self.data,
            "needsConfirmation": self.needs_confirmation,
            "confirmationId": self.confirmation_id,
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
    handler: Callable[..., Awaitable[ToolResult]]

    def manifest(self) -> dict[str, Any]:
        return {
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


REGISTRY: dict[str, ToolDef] = {}


def tool(
    *,
    name: str,
    description: str,
    parameters: list[ToolParam] | None = None,
    permission: Permission = "read_only",
):
    def decorator(fn: Callable[..., Awaitable[ToolResult]]):
        if name in REGISTRY:
            raise ValueError(f"duplicate tool name: {name}")
        REGISTRY[name] = ToolDef(
            name=name,
            description=description,
            permission=permission,
            parameters=parameters or [],
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
            "needsConfirmation": False,
            "confirmationId": None,
            "error": None,
        }
    tool_def = REGISTRY.get(name)
    if tool_def is None:
        return ToolResult().to_envelope(ok=False, error=f"unknown tool: {name}")
    try:
        result = await tool_def.handler(**(arguments or {}))
    except TypeError as e:
        return ToolResult().to_envelope(ok=False, error=f"bad arguments: {e}")
    except Exception as e:
        log.exception("tool %s failed", name)
        return ToolResult().to_envelope(ok=False, error=str(e))
    if not isinstance(result, ToolResult):
        result = ToolResult(spoken=str(result))
    return result.to_envelope()
