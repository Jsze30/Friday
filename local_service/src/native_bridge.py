from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .events import bus

DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_PENDING_REQUESTS = 8


class NativeBridgeError(RuntimeError):
    pass


class NativeToolBridge:
    """Correlates local capability requests with the signed Mac process."""

    def __init__(self) -> None:
        self._connection_count = 0
        self._connected_event = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def connected(self) -> bool:
        return self._connection_count > 0

    def connect(self) -> None:
        self._connection_count += 1
        self._connected_event.set()

    def disconnect(self) -> None:
        self._connection_count = max(0, self._connection_count - 1)
        if self._connection_count:
            return
        self._connected_event.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(NativeBridgeError("the Mac app disconnected"))
        self._pending.clear()

    async def wait_until_connected(self, timeout: float = 0.75) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return self.connected

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if not self.connected:
            raise NativeBridgeError("the Mac app is not connected")
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise NativeBridgeError("too many native operations are already running")

        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        bus.publish(
            {
                "type": "native_tool_request",
                "requestId": request_id,
                "tool": tool,
                "arguments": arguments or {},
            }
        )
        try:
            return await asyncio.wait_for(future, timeout=max(0.1, timeout))
        except TimeoutError as error:
            raise NativeBridgeError(f"native tool {tool} timed out") from error
        finally:
            self._pending.pop(request_id, None)

    def handle_response(self, payload: dict[str, Any]) -> bool:
        if payload.get("type") != "native_tool_response":
            return False
        request_id = payload.get("requestId")
        if not isinstance(request_id, str):
            return True
        future = self._pending.get(request_id)
        if future is None or future.done():
            return True
        result = payload.get("result")
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": "the Mac returned an invalid native tool response",
            }
        future.set_result(result)
        return True


native_bridge = NativeToolBridge()
