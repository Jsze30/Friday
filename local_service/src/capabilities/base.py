from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Permission = Literal["read_only", "low_risk_write", "sensitive"]
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
class ProviderInfo:
    provider_id: str
    name: str
    description: str
    capabilities: tuple[str, ...]
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
