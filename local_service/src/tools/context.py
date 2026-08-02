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
    name="remember_fact",
    description=(
        "Store one explicit durable relationship the user asked Friday to "
        "remember. Do not use this for guesses or incidental conversation."
    ),
    permission="low_risk_write",
    parameters=[
        ToolParam("subject", "string", "The entity the fact is about."),
        ToolParam("predicate", "string", "The relationship, such as works_on or knows."),
        ToolParam("object", "string", "The related entity."),
        ToolParam("subject_kind", "string", "Optional subject kind.", required=False),
        ToolParam("object_kind", "string", "Optional object kind.", required=False),
    ],
)
async def remember_fact(
    subject: str,
    predicate: str,
    object: str,
    subject_kind: str | None = None,
    object_kind: str | None = None,
) -> ToolResult:
    relationship = store.remember_fact(
        subject,
        predicate,
        object,
        subject_kind=subject_kind or "entity",
        object_kind=object_kind or "entity",
    )
    return ToolResult(
        spoken=f"I will remember that {subject} {predicate.replace('_', ' ')} {object}.",
        data={"memory": relationship},
    )


@tool(
    name="remember_preference",
    description=(
        "Store a preference only when the user explicitly asks Friday to "
        "remember or consistently use it."
    ),
    permission="low_risk_write",
    parameters=[
        ToolParam("key", "string", "A stable preference key."),
        ToolParam("value", "string", "The preferred value."),
    ],
)
async def remember_preference(key: str, value: str) -> ToolResult:
    memory = store.set_preference(key, value)
    return ToolResult(
        spoken=f"I will remember your {key.replace('_', ' ')} preference.",
        data={"memory": memory},
    )


@tool(
    name="list_memories",
    description=(
        "Inspect Friday's durable memories, including references, preferences, "
        "entities, and relationships."
    ),
    permission="read_only",
    parameters=[
        ToolParam(
            "kind",
            "string",
            "Optional reference, preference, entity, or relationship filter.",
            required=False,
        ),
        ToolParam("query", "string", "Optional text filter.", required=False),
    ],
)
async def list_memories(
    kind: str | None = None,
    query: str | None = None,
) -> ToolResult:
    memories = store.list_memories(kind=kind, query=query or "", limit=100)
    return ToolResult(
        spoken=f"I found {len(memories)} memories.",
        data={"memories": memories},
    )


@tool(
    name="forget_memory",
    description="Delete one durable memory by the ID returned from list_memories.",
    permission="low_risk_write",
    parameters=[ToolParam("memory_id", "string", "The exact memory ID to delete.")],
)
async def forget_memory(memory_id: str) -> ToolResult:
    removed = store.forget_memory(memory_id)
    return ToolResult(
        spoken="I forgot that memory." if removed else "I could not find that memory.",
        data={"memoryId": memory_id, "forgotten": removed},
    )


@tool(
    name="forget_latest_memory",
    description=(
        "Forget the most recent explicit reference, preference, or fact the user "
        "asked Friday to remember. Use for 'forget that' or 'do not remember that'."
    ),
    permission="low_risk_write",
    actions=[
        {
            "id": "context.forget_latest_memory",
            "description": "Forget the last explicit memory Friday saved.",
            "parameters": [],
            "routes": [
                {
                    "pattern": (
                        r"(?:do\s+not|don't)\s+remember\s+that|"
                        r"forget\s+that"
                    )
                }
            ],
            "latencyMs": 20,
            "priority": 190,
        }
    ],
)
async def forget_latest_memory() -> ToolResult:
    memory = store.forget_latest_explicit_memory()
    return ToolResult(
        spoken="I forgot it." if memory else "There was no recent explicit memory to forget.",
        data={"memory": memory, "forgotten": memory is not None},
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
