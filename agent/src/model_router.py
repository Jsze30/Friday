from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from action_catalog import ActionCatalog

ModelRoute = Literal["fast", "complex"]

_COMPLEX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "explicit complex-model request",
        re.compile(
            r"\b(?:use|switch to)\s+(?:the\s+)?"
            r"(?:smart|smarter|strong|stronger|better|complex|sonnet)\s+model\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deep reasoning request",
        re.compile(
            r"\b(?:think deeply|reason (?:this )?through|step by step|"
            r"in detail|take your time)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "analysis request",
        re.compile(
            r"\b(?:research|investigate|analy[sz]e|debug|diagnose|architect)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "comparison request",
        re.compile(
            r"\b(?:compare|evaluate|weigh)\b|"
            r"\bpros\s+and\s+cons\b|\btrade[- ]?offs?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "planning request",
        re.compile(
            r"\b(?:create|develop|make|design)\s+(?:me\s+)?(?:a\s+)?"
            r"(?:plan|strategy|architecture)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "multi-step request",
        re.compile(
            r"\b(?:and then|after that|followed by|before you do that)\b",
            re.IGNORECASE,
        ),
    ),
)

_LONG_REQUEST_WORDS = 45

_TOOL_REQUEST_PATTERN = re.compile(
    r"\b(?:"
    r"weather|forecast|search|look\s+up|find|open|launch|"
    r"play|pause|skip|file|folder|directory|downloads?|desktop|documents?|"
    r"browser|website|web|spotify|vscode|visual\s+studio\s+code|arc|finder|"
    r"click|type|write|move|rename|delete|trash|run|execute|"
    r"screen|window|current\s+app"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RouteDecision:
    route: ModelRoute
    reason: str


@dataclass(frozen=True)
class DeterministicToolRoute:
    tool_name: str
    arguments: dict[str, Any]
    reason: str


def deterministic_tool_route(
    text: str | None,
    action_catalog: ActionCatalog | None = None,
) -> DeterministicToolRoute | None:
    """Map a clear command through provider-declared action routes."""
    if action_catalog is None:
        return None
    match = action_catalog.match(text)
    if match is None:
        return None
    return DeterministicToolRoute(
        tool_name="run_action",
        arguments={
            "action": match.action_id,
            "arguments_json": json.dumps(match.arguments),
        },
        reason=match.reason,
    )


def route_request(
    text: str | None,
    action_catalog: ActionCatalog | None = None,
) -> RouteDecision:
    """Route a user request without making another model call."""
    normalized = " ".join((text or "").split())
    if not normalized:
        return RouteDecision("fast", "no user text")

    if action_catalog and action_catalog.match(normalized):
        return RouteDecision("fast", "deterministic catalog action")

    word_count = len(normalized.split())
    if word_count >= _LONG_REQUEST_WORDS:
        return RouteDecision("complex", f"long request ({word_count} words)")

    if normalized.count("?") >= 2:
        return RouteDecision("complex", "multiple questions")

    for reason, pattern in _COMPLEX_PATTERNS:
        if pattern.search(normalized):
            return RouteDecision("complex", reason)

    if _TOOL_REQUEST_PATTERN.search(normalized):
        return RouteDecision("complex", "computer or web action")

    return RouteDecision("fast", "default")
