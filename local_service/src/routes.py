from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Body, HTTPException, WebSocket, WebSocketDisconnect

from . import profile, runtime, tools
from .events import bus
from .tokens import mint_token

log = logging.getLogger("friday.routes")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    paused = runtime.detector.is_paused if runtime.detector else None
    return {
        "ok": True,
        "wakePaused": paused,
    }


@router.post("/token")
async def token() -> dict[str, str | int]:
    return mint_token()


@router.post("/wake/pause")
async def wake_pause() -> dict[str, bool]:
    if runtime.detector:
        runtime.detector.pause()
    return {"paused": True}


@router.post("/wake/resume")
async def wake_resume() -> dict[str, bool]:
    if runtime.detector:
        runtime.detector.resume()
    return {"paused": False}


@router.get("/profile")
async def get_profile() -> dict[str, object]:
    return profile.load()


@router.put("/profile")
async def put_profile(data: dict = Body(...)) -> dict[str, object]:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="profile must be an object")
    return profile.save(data)


@router.post("/tools/execute")
async def tools_execute(payload: dict = Body(...)) -> dict[str, object]:
    name = payload.get("tool")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="tool is required")
    args = payload.get("arguments") or {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="arguments must be an object")
    return await tools.execute(name, args)


@router.websocket("/events")
async def events_ws(ws: WebSocket) -> None:
    await ws.accept()
    queue = await bus.subscribe()
    log.info("events client connected")
    try:
        while True:
            event = await queue.get()
            await ws.send_text(json.dumps(event))
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("events ws error")
    finally:
        await bus.unsubscribe(queue)
        log.info("events client disconnected")
