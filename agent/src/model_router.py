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

    return RouteDecision("fast", "default")
