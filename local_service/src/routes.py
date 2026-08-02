from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from . import capabilities, perception, profile, runtime, tools
from .context_store import store as context_store
from .events import bus
from .tokens import mint_token

log = logging.getLogger("friday.routes")
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "wakePaused": runtime.detector.is_paused if runtime.detector else None,
    }


@router.post("/token")
async def token() -> dict[str, str | int]:
    return mint_token()


@router.post("/wake/resume")
async def wake_resume() -> dict[str, bool]:
    if runtime.detector:
        runtime.detector.resume()
    return {"paused": False}


@router.get("/profile")
async def get_profile() -> dict[str, object]:
    return profile.load()


@router.put("/profile")
async def put_profile(data: dict) -> dict[str, object]:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="profile must be an object")
    saved = profile.save(data)
    context_store.import_profile(saved)
    return saved


@router.post("/tools/execute")
async def tools_execute(payload: dict) -> dict[str, object]:
    name = payload.get("tool")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="tool is required")
    args = payload.get("arguments") or {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=400, detail="arguments must be an object")
    return await tools.execute(name, args)


@router.post("/capabilities/execute")
async def capabilities_execute(payload: dict) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return await capabilities.execute(payload)


@router.post("/context/resolve")
async def context_resolve(payload: dict) -> dict[str, object]:
    query = payload.get("query") or ""
    working = payload.get("working") or {}
    if not isinstance(query, str):
        raise HTTPException(status_code=400, detail="query must be a string")
    if not isinstance(working, dict):
        raise HTTPException(status_code=400, detail="working must be an object")
    session_id = payload.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="sessionId must be a string")
    maximum = payload.get("maxCharacters", 8_000)
    if not isinstance(maximum, int):
        raise HTTPException(status_code=400, detail="maxCharacters must be an integer")
    return {
        "ok": True,
        **context_store.resolve(
            query,
            working,
            session_id=session_id,
            max_characters=maximum,
        ),
    }


@router.get("/context/references")
async def context_references() -> dict[str, object]:
    return {"ok": True, "memories": context_store.list_references()}


@router.get("/context/status")
async def context_status() -> dict[str, object]:
    return {"ok": True, **context_store.status()}


@router.get("/context/memories")
async def context_memories(
    kind: str | None = None,
    query: str = "",
    limit: int = 100,
) -> dict[str, object]:
    return {
        "ok": True,
        "memories": context_store.list_memories(
            kind=kind,
            query=query,
            limit=limit,
        ),
    }


@router.delete("/context/memories/{memory_id:path}")
async def context_forget_memory(memory_id: str) -> dict[str, object]:
    return {
        "ok": True,
        "memoryId": memory_id,
        "forgotten": context_store.forget_memory(memory_id),
    }


@router.get("/context/timeline")
async def context_timeline(query: str = "", limit: int = 50) -> dict[str, object]:
    return {
        "ok": True,
        "events": context_store.search_timeline(query, limit=limit),
    }


@router.post("/context/events")
async def context_record_event(payload: dict) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    event = context_store.record_client_event(payload)
    return {"ok": True, "recorded": event is not None, "event": event}


@router.put("/context/references")
async def context_remember_reference(payload: dict) -> dict[str, object]:
    alias = payload.get("alias")
    target = payload.get("target")
    if not isinstance(alias, str) or not isinstance(target, str):
        raise HTTPException(status_code=400, detail="alias and target must be strings")
    kind = payload.get("kind") or "entity"
    if not isinstance(kind, str):
        raise HTTPException(status_code=400, detail="kind must be a string")
    return {
        "ok": True,
        "memory": context_store.remember_reference(alias, target, kind=kind),
    }


@router.put("/context/preferences/{key}")
async def context_set_preference(key: str, payload: dict) -> dict[str, object]:
    if "value" not in payload:
        raise HTTPException(status_code=400, detail="value is required")
    return {
        "ok": True,
        "memory": context_store.set_preference(key, payload["value"]),
    }


@router.post("/context/facts")
async def context_remember_fact(payload: dict) -> dict[str, object]:
    required = ("subject", "predicate", "object")
    if any(not isinstance(payload.get(key), str) for key in required):
        raise HTTPException(
            status_code=400,
            detail="subject, predicate, and object must be strings",
        )
    return {
        "ok": True,
        "memory": context_store.remember_fact(
            payload["subject"],
            payload["predicate"],
            payload["object"],
            subject_kind=str(payload.get("subjectKind") or "entity"),
            object_kind=str(payload.get("objectKind") or "entity"),
        ),
    }


@router.post("/context/retention/run")
async def context_run_retention() -> dict[str, object]:
    return {"ok": True, "removed": context_store.run_retention()}


@router.post("/perception/analyze")
async def perception_analyze(payload: dict) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    try:
        return await perception.analyze(payload)
    except perception.VisualAnalysisError as error:
        return {
            "ok": False,
            "available": True,
            "error": str(error),
        }


@router.websocket("/wake/audio")
async def wake_audio(ws: WebSocket) -> None:
    await ws.accept()
    log.info("wake audio client connected")
    try:
        while True:
            data = await ws.receive_bytes()
            detector = runtime.detector
            if detector is None:
                continue
            event = await asyncio.to_thread(detector.process_pcm, data)
            if event is not None:
                bus.publish(event)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("wake audio websocket error")
    finally:
        log.info("wake audio client disconnected")


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
