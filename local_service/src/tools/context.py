from __future__ import annotations

from ..context_store import store
from .base import ToolParam, ToolResult, tool


@tool(
    name="remember_reference",
    description=(
        "Remember that a phrase refers to a specific person, project, file, "
        "event, app, or other entity. Use this when the user explicitly says "
        "what a phrase means."
    ),
    permission="low_risk_write",
    parameters=[
        ToolParam("alias", "string", "The phrase the user will say."),
        ToolParam("target", "string", "What that phrase refers to."),
        ToolParam(
            "kind",
            "string",
            "Optional entity kind such as project, person, file, event, or app.",
            required=False,
        ),
    ],
    actions=[
        {
            "id": "context.remember_reference",
            "description": "Remember what a personal reference phrase means.",
            "parameters": [
                {
                    "name": "alias",
                    "type": "string",
                    "description": "The phrase to remember.",
                    "required": True,
                },
                {
                    "name": "target",
                    "type": "string",
                    "description": "What the phrase means.",
                    "required": True,
                },
                {
                    "name": "kind",
                    "type": "string",
                    "description": "Optional entity kind.",
                    "required": False,
                },
            ],
            "routes": [
                {
                    "pattern": (
                        r"(?:remember\s+(?:that\s+)?)?when\s+i\s+say\s+"
                        r"[\"']?(?P<alias>.+?)[\"']?,?\s+i\s+mean\s+"
                        r"[\"']?(?P<target>.+?)[\"']?"
                    )
                },
                {
                    "pattern": (
                        r"remember\s+(?:that\s+)?[\"']?(?P<alias>.+?)"
                        r"[\"']?\s+means\s+[\"']?(?P<target>.+?)[\"']?"
                    )
                },
            ],
            "latencyMs": 20,
            "priority": 180,
        }
    ],
)
async def remember_reference(
    alias: str,
    target: str,
    kind: str | None = None,
) -> ToolResult:
    memory = store.remember_reference(alias, target, kind=kind or "entity")
    label = memory["metadata"].get("label") or memory["target"]
    return ToolResult(
        spoken=f"I will remember that {memory['alias']} means {label}.",
        data={"memory": memory},
    )


@tool(
    name="list_reference_memories",
    description="List the personal reference phrases Friday currently remembers.",
    permission="read_only",
)
async def list_reference_memories() -> ToolResult:
    memories = store.list_references()
    return ToolResult(
        spoken=f"I found {len(memories)} saved reference memories.",
        data={"memories": memories},
    )


@tool(
    name="forget_reference",
    description="Forget one saved personal reference phrase.",
    permission="low_risk_write",
    parameters=[ToolParam("alias", "string", "The phrase to forget.")],
    actions=[
        {
            "id": "context.forget_reference",
            "description": "Forget one personal reference phrase.",
            "parameters": [
                {
                    "name": "alias",
                    "type": "string",
                    "description": "The phrase to forget.",
                    "required": True,
                }
            ],
            "routes": [
                {
                    "pattern": (
                        r"forget\s+(?:what\s+)?[\"']?(?P<alias>.+?)"
                        r"[\"']?\s+(?:means|refers\s+to)"
                    )
                }
            ],
            "latencyMs": 20,
            "priority": 180,
        }
    ],
)
async def forget_reference(alias: str) -> ToolResult:
    removed = store.forget_reference(alias)
    if removed:
        return ToolResult(
            spoken=f"I forgot what {alias} means.",
            data={"alias": alias, "forgotten": True},
        )
    return ToolResult(
        spoken=f"I did not have a saved meaning for {alias}.",
        data={"alias": alias, "forgotten": False},
    )
