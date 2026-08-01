from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from .base import (
    ActionDefinition,
    CapabilityProvider,
    CapabilityRequest,
    CapabilityResult,
    ProgressCallback,
    ProviderFailed,
    ProviderUnavailable,
)

log = logging.getLogger("friday.capabilities.broker")


@dataclass
class ProviderAttempt:
    provider_id: str
    status: str
    duration_ms: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "provider": self.provider_id,
            "status": self.status,
            "durationMs": self.duration_ms,
        }
        if self.reason:
            value["reason"] = self.reason
        return value


class AllProvidersFailed(ProviderFailed):
    def __init__(
        self,
        capability: str,
        attempts: list[ProviderAttempt],
    ) -> None:
        self.attempts = attempts
        reasons = "; ".join(
            f"{attempt.provider_id}: {attempt.reason or attempt.status}"
            for attempt in attempts
        )
        super().__init__(f"Every provider for {capability} failed. {reasons}")


class CapabilityBroker:
    def __init__(self, providers: list[CapabilityProvider]) -> None:
        self._providers = providers
        self._availability: dict[str, tuple[float, bool]] = {}

    async def catalog(self) -> dict[str, Any]:
        available = await asyncio.gather(
            *(self._is_available(provider) for provider in self._providers)
        )
        provider_rows = [
            {**provider.info.to_dict(), "available": is_available}
            for provider, is_available in zip(
                self._providers,
                available,
                strict=True,
            )
        ]
        capabilities = sorted(
            {
                capability
                for provider, is_available in zip(
                    self._providers,
                    available,
                    strict=True,
                )
                if is_available
                for capability in provider.info.capabilities
            }
        )
        actions: dict[str, dict[str, Any]] = {}
        ranked_available = sorted(
            (
                provider
                for provider, is_available in zip(
                    self._providers,
                    available,
                    strict=True,
                )
                if is_available
            ),
            key=self._provider_rank,
        )
        for provider in ranked_available:
            for action in provider.info.actions:
                existing = actions.get(action.action_id)
                if existing is None:
                    existing = {
                        **action.to_dict(),
                        "target": {
                            "kind": "capability",
                            "action": action.action_id,
                        },
                        "providers": [],
                    }
                    actions[action.action_id] = existing
                existing["providers"].append(provider.info.provider_id)
        return {
            "capabilities": capabilities,
            "actions": list(actions.values()),
            "providers": provider_rows,
        }

    async def execute(
        self,
        request: CapabilityRequest,
        progress: ProgressCallback,
    ) -> tuple[CapabilityResult, list[ProviderAttempt], str]:
        candidates = await self._ranked_candidates(request)
        if not candidates:
            raise ProviderUnavailable(
                f"No available provider supports {request.capability}."
            )
        return await self._execute_candidates(request, candidates, progress)

    async def execute_action(
        self,
        action_id: str,
        arguments: dict[str, Any],
        goal: str,
        progress: ProgressCallback,
    ) -> tuple[CapabilityResult, list[ProviderAttempt], str]:
        matches: list[tuple[CapabilityProvider, ActionDefinition]] = [
            (provider, action)
            for provider in self._providers
            for action in provider.info.actions
            if action.action_id == action_id
        ]
        availability = await asyncio.gather(
            *(self._is_available(provider) for provider, _action in matches)
        )
        available_matches = [
            match
            for match, is_available in zip(matches, availability, strict=True)
            if is_available
        ]
        if not available_matches:
            raise ProviderUnavailable(f"No available provider supports {action_id}.")
        available_matches.sort(key=lambda match: self._provider_rank(match[0]))
        action = available_matches[0][1]
        normalized_arguments = self._validate_action_arguments(action, arguments)
        request = CapabilityRequest(
            capability=action.capability,
            goal=goal.strip() or action.description,
            inputs={"action": action.operation, **normalized_arguments},
            permission=action.permission,
        )
        candidates = [provider for provider, _action in available_matches]
        return await self._execute_candidates(request, candidates, progress)

    async def _execute_candidates(
        self,
        request: CapabilityRequest,
        candidates: list[CapabilityProvider],
        progress: ProgressCallback,
    ) -> tuple[CapabilityResult, list[ProviderAttempt], str]:
        attempts: list[ProviderAttempt] = []
        for index, provider in enumerate(candidates):
            if index:
                await progress(
                    "fallback",
                    f"Trying {provider.info.name} instead.",
                )
            await progress("provider", f"Using {provider.info.name}.")
            started = time.monotonic()
            try:
                result = await provider.execute(request, progress)
                verified, reason = await provider.verify(request, result)
                duration = int((time.monotonic() - started) * 1000)
                if not verified:
                    attempts.append(
                        ProviderAttempt(
                            provider_id=provider.info.provider_id,
                            status="verification_failed",
                            duration_ms=duration,
                            reason=reason,
                        )
                    )
                    log.warning(
                        "provider verification failed provider=%s reason=%s",
                        provider.info.provider_id,
                        reason,
                    )
                    continue
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider.info.provider_id,
                        status="succeeded",
                        duration_ms=duration,
                    )
                )
                return result, attempts, provider.info.provider_id
            except asyncio.CancelledError:
                raise
            except (ProviderUnavailable, ProviderFailed, TimeoutError) as error:
                duration = int((time.monotonic() - started) * 1000)
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider.info.provider_id,
                        status="failed",
                        duration_ms=duration,
                        reason=str(error),
                    )
                )
                log.warning(
                    "provider failed provider=%s error=%s",
                    provider.info.provider_id,
                    error,
                )
            except Exception as error:
                duration = int((time.monotonic() - started) * 1000)
                attempts.append(
                    ProviderAttempt(
                        provider_id=provider.info.provider_id,
                        status="failed",
                        duration_ms=duration,
                        reason=type(error).__name__,
                    )
                )
                log.exception(
                    "unexpected provider failure provider=%s",
                    provider.info.provider_id,
                )

        raise AllProvidersFailed(request.capability, attempts)

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(provider.shutdown() for provider in self._providers),
            return_exceptions=True,
        )

    async def _ranked_candidates(
        self,
        request: CapabilityRequest,
    ) -> list[CapabilityProvider]:
        matching = [
            provider
            for provider in self._providers
            if request.capability in provider.info.capabilities
            and provider.info.permission == request.permission
        ]
        availability = await asyncio.gather(
            *(self._is_available(provider) for provider in matching)
        )
        candidates = [
            provider
            for provider, is_available in zip(
                matching,
                availability,
                strict=True,
            )
            if is_available
        ]
        return sorted(
            candidates,
            key=self._provider_rank,
        )

    @staticmethod
    def _provider_rank(provider: CapabilityProvider) -> tuple[float | int | str, ...]:
        return (
            -provider.info.priority,
            -provider.info.reliability,
            provider.info.latency,
            provider.info.provider_id,
        )

    @staticmethod
    def _validate_action_arguments(
        action: ActionDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise ProviderFailed("action arguments must be an object")
        parameters = {parameter.name: parameter for parameter in action.parameters}
        unknown = sorted(set(arguments) - set(parameters))
        if unknown:
            raise ProviderFailed(
                f"Unknown arguments for {action.action_id}: {', '.join(unknown)}."
            )
        normalized: dict[str, Any] = {}
        for name, parameter in parameters.items():
            if name not in arguments:
                if parameter.required:
                    raise ProviderFailed(f"{name} is required for {action.action_id}.")
                continue
            value = arguments[name]
            valid = {
                "string": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, (int, float))
                and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
            }[parameter.type]
            if not valid:
                raise ProviderFailed(
                    f"{name} must be a {parameter.type} for {action.action_id}."
                )
            if (
                parameter.minimum is not None
                and isinstance(value, (int, float))
                and value < parameter.minimum
            ):
                raise ProviderFailed(f"{name} must be at least {parameter.minimum:g}.")
            if (
                parameter.maximum is not None
                and isinstance(value, (int, float))
                and value > parameter.maximum
            ):
                raise ProviderFailed(f"{name} must be at most {parameter.maximum:g}.")
            if parameter.choices and str(value) not in parameter.choices:
                raise ProviderFailed(
                    f"{name} must be one of {', '.join(parameter.choices)}."
                )
            normalized[name] = value
        return normalized

    async def _is_available(self, provider: CapabilityProvider) -> bool:
        now = time.monotonic()
        cached = self._availability.get(provider.info.provider_id)
        if cached and now - cached[0] < 10:
            return cached[1]
        try:
            value = bool(await asyncio.wait_for(provider.available(), timeout=1))
        except Exception:
            log.exception(
                "provider availability failed provider=%s",
                provider.info.provider_id,
            )
            value = False
        self._availability[provider.info.provider_id] = (now, value)
        return value
