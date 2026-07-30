from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

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

_SIMPLE_SYSTEM_PATTERN = re.compile(
    r"^(?:(?:please|(?:can|could|would)\s+you)\s+)?(?:"
    r"(?:open|visit|go\s+to)\s+(?:https?://|www\.)\S+"
    r"(?:\s+(?:in|with|using)\s+[\w .'-]+)?|"
    r"(?:open|launch|focus|activate|quit|close)\s+(?:the\s+)?[\w .'-]+|"
    r"(?:set|change|turn)\s+(?:the\s+)?(?:mac\s+)?volume\s+(?:to\s+)?\d{1,3}|"
    r"(?:raise|increase|lower|decrease|turn\s+up|turn\s+down)\s+"
    r"(?:the\s+)?(?:mac\s+)?volume|"
    r"(?:mute|unmute)\s+(?:the\s+)?(?:mac|audio|sound|volume)?|"
    r"(?:what(?:'s| is)\s+)?(?:the\s+)?(?:mac\s+)?volume(?:\s+level)?|"
    r"list\s+(?:the\s+)?(?:running\s+)?apps"
    r")[.!?]?$",
    re.IGNORECASE,
)

_SIMPLE_MUSIC_PATTERN = re.compile(
    r"^(?:(?:please|(?:can|could|would)\s+you)\s+)?(?:"
    r"(?:play|pause|resume)\b.+|"
    r"(?:open|show|list|browse)\b.+\bplaylists?\b|"
    r"(?:what|which)\s+(?:songs|tracks)\b.+\bplaylists?\b|"
    r"look\s+(?:through|in)\b.+\bplaylists?\b|"
    r"(?:skip|next|previous)(?:\s+(?:song|track))?|"
    r"(?:what(?:'s| is)\s+(?:currently\s+)?playing)|"
    r"(?:what\s+(?:song|track)\s+is\s+(?:this|playing))|"
    r"(?:add|queue)\b.+|"
    r"(?:turn\s+)?shuffle\s+(?:on|off)|"
    r"(?:set\s+)?spotify\s+volume\s+(?:to\s+)?\d{1,3}"
    r")[.!?]?$",
    re.IGNORECASE,
)

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


def route_request(text: str | None) -> RouteDecision:
    """Route a user request without making another model call."""
    normalized = " ".join((text or "").split())
    if not normalized:
        return RouteDecision("fast", "no user text")

    word_count = len(normalized.split())
    if word_count >= _LONG_REQUEST_WORDS:
        return RouteDecision("complex", f"long request ({word_count} words)")

    if normalized.count("?") >= 2:
        return RouteDecision("complex", "multiple questions")

    for reason, pattern in _COMPLEX_PATTERNS:
        if pattern.search(normalized):
            return RouteDecision("complex", reason)

    if _SIMPLE_MUSIC_PATTERN.fullmatch(normalized):
        return RouteDecision("fast", "simple music control")

    if _SIMPLE_SYSTEM_PATTERN.fullmatch(normalized):
        return RouteDecision("fast", "simple Mac control")

    if _TOOL_REQUEST_PATTERN.search(normalized):
        return RouteDecision("complex", "computer or web action")

    return RouteDecision("fast", "default")
