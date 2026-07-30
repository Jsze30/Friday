from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger("friday.tools")

Permission = Literal["read_only", "low_risk_write", "sensitive"]
ParamType = Literal["string", "integer", "number", "boolean", "array"]
CONFIRMATION_TTL_SECONDS = 60.0


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


@dataclass
class PendingAction:
    tool_name: str
    arguments: dict[str, Any]
    created_at: float


PENDING_ACTIONS: dict[str, PendingAction] = {}


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
    log.info("tool_call name=%s permission=%s", name, tool_def.permission)
    if tool_def.permission == "sensitive":
        _discard_expired_confirmations()
        confirmation_id = str(uuid.uuid4())
        PENDING_ACTIONS[confirmation_id] = PendingAction(
            tool_name=name,
            arguments=dict(arguments or {}),
            created_at=time.monotonic(),
        )
        envelope = ToolResult(
            spoken=f"I need confirmation before I run {name}.",
            data={
                "tool": name,
                "arguments": _preview_arguments(arguments or {}),
                "expiresInSeconds": int(CONFIRMATION_TTL_SECONDS),
            },
            needs_confirmation=True,
            confirmation_id=confirmation_id,
        ).to_envelope()
        log.info("tool_staged name=%s confirmation=%s", name, confirmation_id)
        return envelope
    envelope = await _execute_tool(tool_def, arguments)
    log.info("tool_result name=%s ok=%s", name, envelope.get("ok"))
    return envelope


async def resolve_confirmation(
    confirmation_id: str,
    approve: bool,
) -> ToolResult:
    _discard_expired_confirmations()
    pending = PENDING_ACTIONS.pop(confirmation_id, None)
    if pending is None:
        return ToolResult(
            spoken="That confirmation has expired or does not exist.",
            data={"error": "unknown_confirmation"},
        )
    if not approve:
        log.info("tool_confirmation rejected name=%s", pending.tool_name)
        return ToolResult(
            spoken="Cancelled.",
            data={"cancelled": True, "tool": pending.tool_name},
        )

    tool_def = REGISTRY.get(pending.tool_name)
    if tool_def is None:
        return ToolResult(
            spoken="That action is no longer available.",
            data={"error": "unknown_tool"},
        )
    envelope = await _execute_tool(tool_def, pending.arguments)
    log.info(
        "tool_confirmation approved name=%s ok=%s",
        pending.tool_name,
        envelope.get("ok"),
    )
    if not envelope.get("ok"):
        return ToolResult(
            spoken=envelope.get("error") or "The confirmed action failed.",
            data={"error": "execution_failed"},
        )
    return ToolResult(
        spoken=envelope.get("spoken"),
        data=envelope.get("data"),
    )


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


def _discard_expired_confirmations() -> None:
    cutoff = time.monotonic() - CONFIRMATION_TTL_SECONDS
    expired = [
        action_id
        for action_id, action in PENDING_ACTIONS.items()
        if action.created_at < cutoff
    ]
    for action_id in expired:
        PENDING_ACTIONS.pop(action_id, None)


def _preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > 500:
            preview[key] = value[:500] + "..."
        elif isinstance(value, list):
            preview[key] = value[:50]
        else:
            preview[key] = value
    return preview
