from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Permission = Literal["read_only", "low_risk_write", "sensitive"]
ActionParamType = Literal["string", "integer", "number", "boolean"]
ProgressCallback = Callable[[str, str], Awaitable[None]]


class ProviderUnavailable(RuntimeError):
    pass


class ProviderFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class CapabilityRequest:
    capability: str
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)
    permission: Permission = "read_only"


@dataclass
class CapabilityResult:
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "data": self.data}


@dataclass(frozen=True)
class ActionParameter:
    name: str
    type: ActionParamType
    description: str = ""
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
        }
        if self.minimum is not None:
            value["minimum"] = self.minimum
        if self.maximum is not None:
            value["maximum"] = self.maximum
        if self.choices:
            value["choices"] = list(self.choices)
        return value


@dataclass(frozen=True)
class ActionRoute:
    pattern: str
    fixed_arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"pattern": self.pattern}
        if self.fixed_arguments:
            value["arguments"] = self.fixed_arguments
        return value


@dataclass(frozen=True)
class ActionDefinition:
    action_id: str
    capability: str
    operation: str
    description: str
    parameters: tuple[ActionParameter, ...] = ()
    routes: tuple[ActionRoute, ...] = ()
    permission: Permission = "read_only"
    latency_ms: int = 500
    priority: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.action_id,
            "capability": self.capability,
            "description": self.description,
            "parameters": [parameter.to_dict() for parameter in self.parameters],
            "routes": [route.to_dict() for route in self.routes],
            "permission": self.permission,
            "latencyMs": self.latency_ms,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class ProviderInfo:
    provider_id: str
    name: str
    description: str
    capabilities: tuple[str, ...]
    actions: tuple[ActionDefinition, ...] = ()
    permission: Permission = "read_only"
    priority: int = 50
    reliability: float = 0.8
    latency: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": self.name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "actions": [action.action_id for action in self.actions],
            "permission": self.permission,
            "priority": self.priority,
            "reliability": self.reliability,
            "latency": self.latency,
        }


class CapabilityProvider(ABC):
    info: ProviderInfo

    async def available(self) -> bool:
        return True

    async def shutdown(self) -> None:
        return None

    @abstractmethod
    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> CapabilityResult:
        raise NotImplementedError

    async def verify(
        self,
        request: CapabilityRequest,
        result: CapabilityResult,
    ) -> tuple[bool, str | None]:
        if result.summary.strip():
            return True, None
        return False, "provider returned an empty result"
