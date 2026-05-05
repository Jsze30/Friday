from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import REPO_ROOT
from .events import bus

log = logging.getLogger("friday.profile")

PROFILE_PATH = Path.home() / "Library" / "Application Support" / "Friday" / "profile.json"
SEED_PATH = REPO_ROOT / "profile.seed.json"

_lock = threading.Lock()


def _ensure_exists() -> None:
    if PROFILE_PATH.exists():
        return
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SEED_PATH.exists():
        shutil.copyfile(SEED_PATH, PROFILE_PATH)
        log.info("seeded profile from %s", SEED_PATH)
    else:
        PROFILE_PATH.write_text(json.dumps({"version": 1, "facts": {}, "updated_at": None}))
        log.info("created empty profile at %s", PROFILE_PATH)


def load() -> dict[str, Any]:
    with _lock:
        _ensure_exists()
        try:
            return json.loads(PROFILE_PATH.read_text() or "{}")
        except json.JSONDecodeError:
            log.exception("profile.json corrupt; returning empty")
            return {"version": 1, "facts": {}, "updated_at": None}


def _atomic_write(data: dict[str, Any]) -> None:
    tmp = PROFILE_PATH.with_suffix(".json.tmp")
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, PROFILE_PATH)


def save(data: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        data = dict(data)
        data.setdefault("version", 1)
        data.setdefault("facts", {})
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(data)
    bus.publish({"type": "profile_updated", "profile": data})
    return data


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def update(patch: dict[str, Any]) -> dict[str, Any]:
    current = load()
    merged = _deep_merge(current, patch)
    return save(merged)


def set_fact(key: str, value: Any) -> dict[str, Any]:
    return update({"facts": {key: value}})
