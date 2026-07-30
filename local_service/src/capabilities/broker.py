from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from .base import (
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
        super().__init__(
            f"Every provider for {capability} failed. {reasons}"
        )


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
        return {"capabilities": capabilities, "providers": provider_rows}

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
            key=lambda provider: (
                -provider.info.priority,
                -provider.info.reliability,
                provider.info.latency,
                provider.info.provider_id,
            ),
        )

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
