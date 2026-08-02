from __future__ import annotations

import re

_STOP_COMMAND = re.compile(
    r"^(?:(?:hey\s+)?friday[\s,]+)?"
    r"(?:please\s+)?(?:"
    r"stop(?:\s+(?:that|it|now|trying|what\s+you(?:'re|\s+are)\s+doing))?"
    r"|cancel(?:\s+(?:that|it|the\s+(?:task|action)))?"
    r"|abort(?:\s+(?:that|it|the\s+(?:task|action)))?"
    r"|never\s*mind"
    r")$",
    re.IGNORECASE,
)


def is_stop_command(text: str | None) -> bool:
    command = " ".join((text or "").split()).strip(" .!?")
    return bool(_STOP_COMMAND.fullmatch(command))


__all__ = ["is_stop_command"]
