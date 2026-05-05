from __future__ import annotations

import importlib
import logging
import pkgutil

from .base import REGISTRY, ToolDef, ToolParam, ToolResult, execute, tool

log = logging.getLogger("friday.tools")

_loaded = False


def load_all() -> None:
    """Import every sibling module so @tool decorators register on import."""
    global _loaded
    if _loaded:
        return
    for _, mod_name, _ in pkgutil.iter_modules(__path__):
        if mod_name.startswith("_") or mod_name == "base":
            continue
        importlib.import_module(f"{__name__}.{mod_name}")
    _loaded = True
    log.info("loaded %d tools: %s", len(REGISTRY), sorted(REGISTRY.keys()))


__all__ = [
    "REGISTRY",
    "ToolDef",
    "ToolParam",
    "ToolResult",
    "execute",
    "load_all",
    "tool",
]
