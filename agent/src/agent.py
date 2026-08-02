from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    ModelSettings,
    function_tool,
    llm,
    room_io,
)
from livekit.agents.beta.toolsets import ToolProxyToolset
from livekit.plugins import anthropic, deepgram, openai, silero
from openai.types import Reasoning as OpenAIReasoning

from action_catalog import ActionCatalog, merge_action_manifests
from action_tool import build_action_tool
from capability_tool import build_capability_tool
from hud import HudPublisher
from model_router import DeterministicToolRoute, deterministic_tool_route, route_request
from turn_gate import PreRollAudioInput, PreRollReceiver

FOLLOWUP_SECONDS = 5.0
LOCATION_PROFILE_KEYS = {
    "city",
    "default_location",
    "home_city",
    "location",
}
CONTEXT_RELEVANCE_PATTERN = re.compile(
    r"\b(?:this|that|these|those|current|project|repo|repository|file|document|"
    r"page|website|site|tab|app|application|window|calendar|schedule|meeting|"
    r"event|today|tomorrow|next|coming\s+up|working\s+on|doing\s+now)\b",
    re.IGNORECASE,
)

load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")

logger = logging.getLogger("friday-agent")

AGENT_NAME = "friday-agent"
FAST_MODEL = os.getenv("FRIDAY_FAST_MODEL", "gpt-4.1-nano")
COMPLEX_MODEL = os.getenv("FRIDAY_COMPLEX_MODEL", "gpt-5.6-terra")
COMPLEX_EFFORT = os.getenv("FRIDAY_COMPLEX_EFFORT", "low")

BASE_INSTRUCTIONS = """You are Friday, a personal voice assistant on the user's Mac.
Your name is Friday.
Speak naturally and concisely. Avoid markdown, lists, or special characters -
your replies are spoken aloud. Default to one or two short sentences unless the
user explicitly asks for detail.

You have a fast action runner, a high-level capability runner, and discoverable
fallback primitives. Use run_action for clear operations supported by the
shared action catalog. Actions are declared by integrations and execute through
the fastest available provider. Use run_capability when the request requires
reasoning, discovery, or several steps. Use tool_search and call_tool only for
low-level operations that the action and capability layers do not support. Do
not claim you cannot inspect or control something until you have tried the
relevant action, capability, or read-only primitive.

Important mappings and workflows:
- For run_capability inputs_json, pass a JSON object as a string.
- For files, use capability files. Human paths include Downloads, Documents,
  Desktop, and Friday project. Never guess a path such as /Downloads.
- For broad research, use capability research with the query in inputs_json.
- To read one known URL, use capability web with the URL in inputs_json.
- For repository questions, use capability coding. It is read-only.
- Prefer a registered action over UI inspection, AppleScript, or a generic
  capability whenever the action catalog supports the request.
- For current weather, use the ambient latitude and longitude with fetch_url and
  Open-Meteo's forecast endpoint through the fallback primitive search. Request current temperature, apparent
  temperature, weather code, and wind, use timezone=auto, and honor the user's
  preferred temperature unit.
- To use an app, call list_apps or open_app, then inspect_ui to discover its
  current controls. Call interact_ui only with an exact element ID and an action
  returned by the latest inspect_ui result.
- When asked where the user is, answer with the human-readable place. Mention
  coordinates only if the user asks for coordinates.

Tool results contain structured data. Read that data and answer from it instead
of repeating the tool's short status message. Actions execute immediately when
the user requests them. Do not ask the user to confirm an action."""

PARAM_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list[str],
}

TurnContextProvider = Callable[[str], Awaitable[dict[str, Any]]]
HudEventSink = Callable[[str, dict[str, Any]], None]


def matching_route_tools(
    tools: list[llm.Tool],
    tool_name: str,
) -> list[llm.Tool]:
    return [
        tool
        for tool in tools
        if getattr(getattr(tool, "info", None), "name", None) == tool_name
    ]


def render_instructions(
    profile: dict | None,
    location: dict | None = None,
) -> str:
    sections = [BASE_INSTRUCTIONS]
    facts = {
        key: value
        for key, value in ((profile or {}).get("facts") or {}).items()
        if key not in LOCATION_PROFILE_KEYS
    }
    if facts:
        lines = [f"- {k}: {v}" for k, v in facts.items()]
        sections.append("<profile>\n" + "\n".join(lines) + "\n</profile>")

    if (location or {}).get("status") == "available":
        location_lines = []
        for key in (
            "place",
            "city",
            "region",
            "country",
            "countryCode",
            "postalCode",
            "latitude",
            "longitude",
            "horizontalAccuracyMeters",
            "timestamp",
        ):
            value = (location or {}).get(key)
            if value is not None:
                location_lines.append(f"- {key}: {value}")
        sections.append(
            "<current_location>\n"
            + "\n".join(location_lines)
            + "\n"
            + "</current_location>"
        )

    return "\n\n".join(sections)


class FridayAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        tools: list[llm.Tool | llm.Toolset],
        fast_llm: llm.LLM,
        complex_llm: llm.LLM,
        action_catalog: ActionCatalog | None = None,
        complex_extra_kwargs: dict[str, Any] | None = None,
        turn_context_provider: TurnContextProvider | None = None,
        hud_event_sink: HudEventSink | None = None,
    ) -> None:
        super().__init__(
            instructions=instructions,
            tools=tools,
            llm=fast_llm,
        )
        self._fast_llm = fast_llm
        self._complex_llm = complex_llm
        self._action_catalog = action_catalog or ActionCatalog()
        self._complex_extra_kwargs = complex_extra_kwargs or {}
        self._turn_context_provider = turn_context_provider
        self._hud_event_sink = hud_event_sink
        self._current_turn_context: dict[str, Any] = {}
        self._timezone = ZoneInfo("UTC")

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        self._current_turn_context = {}
        if self._turn_context_provider is None or not new_message.text_content:
            return
        try:
            context = await asyncio.wait_for(
                self._turn_context_provider(new_message.text_content),
                timeout=0.35,
            )
        except TimeoutError:
            logger.warning("turn context timed out")
            return
        except Exception:
            logger.exception("turn context retrieval failed")
            return
        if not context:
            return
        self._current_turn_context = context
        if self._context_is_relevant(new_message.text_content, context):
            turn_ctx.add_message(
                role="system",
                content=(
                    "<working_context>\n"
                    + json.dumps(context, separators=(",", ":"))
                    + "\n</working_context>\n"
                    "Use this only for the current request. Prefer explicit saved "
                    "reference resolutions over guesses. If the context is still "
                    "ambiguous, ask one short question."
                ),
                created_at=new_message.created_at - 0.000001,
            )
        if self._hud_event_sink:
            self._hud_event_sink("context", context)

    def update_ambient_context(
        self,
        profile: dict | None,
        location: dict | None,
    ) -> None:
        facts = (profile or {}).get("facts") or {}
        timezone_name = facts.get("timezone") or (location or {}).get("timezone")
        if not isinstance(timezone_name, str):
            return
        try:
            self._timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning("unknown ambient timezone: %s", timezone_name)

    @staticmethod
    def _latest_user_text(chat_ctx: llm.ChatContext) -> str | None:
        for item in reversed(chat_ctx.items):
            if isinstance(item, llm.ChatMessage) and item.role == "user":
                return item.text_content
        return None

    @staticmethod
    def _is_initial_user_inference(chat_ctx: llm.ChatContext) -> bool:
        if not chat_ctx.items:
            return False
        latest = chat_ctx.items[-1]
        return isinstance(latest, llm.ChatMessage) and latest.role == "user"

    @staticmethod
    def _context_is_relevant(query: str, context: dict[str, Any]) -> bool:
        return bool(context.get("resolutions")) or bool(
            CONTEXT_RELEVANCE_PATTERN.search(query)
        )

    def _deterministic_route_for_turn(
        self,
        chat_ctx: llm.ChatContext,
    ) -> DeterministicToolRoute | None:
        if not self._is_initial_user_inference(chat_ctx):
            return None
        route = deterministic_tool_route(
            self._latest_user_text(chat_ctx),
            self._action_catalog,
        )
        if route is None or not self._current_turn_context.get("resolutions"):
            return route
        action = route.arguments.get("action")
        if isinstance(action, str) and action.startswith("context."):
            return route
        return None

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk]:
        user_text = self._latest_user_text(chat_ctx)
        deterministic_route = self._deterministic_route_for_turn(chat_ctx)
        if deterministic_route:
            matching_tools = matching_route_tools(
                tools,
                deterministic_route.tool_name,
            )
            if matching_tools:
                logger.info(
                    "tool_route tool=%s reason=%s model=none",
                    deterministic_route.tool_name,
                    deterministic_route.reason,
                )
                yield llm.ChatChunk(
                    id=f"friday-action-{uuid.uuid4().hex}",
                    delta=llm.ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            llm.FunctionToolCall(
                                name=deterministic_route.tool_name,
                                arguments=json.dumps(deterministic_route.arguments),
                                call_id=f"call-{uuid.uuid4().hex}",
                            )
                        ],
                    ),
                )
                return
            logger.warning(
                "deterministic tool %s is unavailable",
                deterministic_route.tool_name,
            )

        decision = route_request(user_text, self._action_catalog)
        selected_llm = (
            self._complex_llm if decision.route == "complex" else self._fast_llm
        )
        extra_kwargs = self._complex_extra_kwargs if decision.route == "complex" else {}

        logger.info(
            "llm_route route=%s model=%s reason=%s",
            decision.route,
            selected_llm.model,
            decision.reason,
        )

        current_time = datetime.now(self._timezone)
        turn_chat_ctx = chat_ctx.copy()
        turn_chat_ctx.add_message(
            role="system",
            content=(
                "<current_time>\n"
                f"- local_datetime: {current_time.isoformat(timespec='seconds')}\n"
                f"- weekday: {current_time.strftime('%A')}\n"
                f"- timezone: {self._timezone.key}\n"
                "</current_time>"
            ),
        )

        response_text = ""
        last_hud_update = 0.0
        async with selected_llm.chat(
            chat_ctx=turn_chat_ctx,
            tools=tools,
            tool_choice=model_settings.tool_choice,
            conn_options=self.session.conn_options.llm_conn_options,
            extra_kwargs=extra_kwargs,
        ) as stream:
            async for chunk in stream:
                content = chunk.delta.content if chunk.delta else None
                if content:
                    response_text += content
                    now = asyncio.get_running_loop().time()
                    if self._hud_event_sink and now - last_hud_update >= 0.05:
                        self._hud_event_sink(
                            "transcript",
                            {
                                "role": "assistant",
                                "text": response_text,
                                "isFinal": False,
                            },
                        )
                        last_hud_update = now
                yield chunk
        if response_text and self._hud_event_sink:
            self._hud_event_sink(
                "transcript",
                {
                    "role": "assistant",
                    "text": response_text,
                    "isFinal": True,
                },
            )


server = AgentServer()


def build_complex_llm() -> tuple[llm.LLM, dict[str, Any]]:
    if COMPLEX_MODEL.startswith("claude-"):
        return (
            anthropic.LLM(
                model=COMPLEX_MODEL,
                max_tokens=1024,
                caching="ephemeral",
            ),
            {"output_config": {"effort": COMPLEX_EFFORT}},
        )

    return (
        openai.responses.LLM(
            model=COMPLEX_MODEL,
            reasoning=OpenAIReasoning(effort=COMPLEX_EFFORT),
            verbosity="low",
            max_output_tokens=800,
        ),
        {},
    )


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    fast_llm = openai.responses.LLM(
        model=FAST_MODEL,
        max_output_tokens=300,
    )
    complex_llm, complex_extra_kwargs = build_complex_llm()
    complex_llm.prewarm()
    ctx.add_shutdown_callback(complex_llm.aclose)

    session = AgentSession(
        stt=deepgram.STTv2(
            model="flux-general-en",
            eot_threshold=0.5,
            eot_timeout_ms=700,
        ),
        llm=fast_llm,
        tts=deepgram.TTS(model="aura-2-athena-en"),
        vad=ctx.proc.userdata["vad"],
        # LLM already runs preemptively by default; also start TTS before the
        # turn is confirmed so first audio is ready the moment it commits.
        turn_handling={"preemptive_generation": {"preemptive_tts": True}},
        max_tool_steps=6,
    )

    await ctx.connect()

    hud = HudPublisher(ctx.room.local_participant)
    hud.start()
    ctx.add_shutdown_callback(hud.aclose)

    preroll_receiver = PreRollReceiver(ctx.room)
    preroll_receiver.register()
    # Set after session.start wraps RoomIO's audio input.
    gate: PreRollAudioInput | None = None

    followup_task: asyncio.Task | None = None
    turn_active = False

    def mac_identity() -> str | None:
        for p in ctx.room.remote_participants.values():
            if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                ident = p.identity
                return (
                    ident
                    if isinstance(ident, str)
                    else getattr(ident, "stringValue", None) or str(ident)
                )
        return None

    def mac_rpc_ready() -> bool:
        for p in ctx.room.remote_participants.values():
            if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                return p.attributes.get("friday.rpcReady") == "true"
        return False

    async def rpc_to_mac(
        method: str,
        payload: str = "",
        *,
        log_failure: bool = True,
    ) -> str | None:
        identity = mac_identity()
        if not identity:
            if log_failure:
                logger.warning("no mac participant for rpc %s", method)
            return None
        try:
            return await ctx.room.local_participant.perform_rpc(
                destination_identity=identity,
                method=method,
                payload=payload,
            )
        # LiveKit RPC can surface transport, timeout, or participant errors
        # from several SDK exception families.
        except Exception as e:  # noqa: BLE001
            if log_failure:
                logger.warning("rpc %s failed: %s", method, e)
            return None

    async def wait_for_mac(timeout: float = 15.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if mac_identity() and mac_rpc_ready():
                return
            await asyncio.sleep(0.1)
        logger.warning("mac participant did not signal RPC readiness before timeout")

    async def startup_rpc(method: str, payload: str = "") -> str | None:
        attempts = 3
        for attempt in range(attempts):
            raw = await rpc_to_mac(
                method,
                payload,
                log_failure=attempt == attempts - 1,
            )
            if raw:
                if attempt:
                    logger.info(
                        "startup rpc %s succeeded on attempt %d",
                        method,
                        attempt + 1,
                    )
                return raw
            if attempt < attempts - 1:
                logger.info(
                    "startup rpc %s unavailable on attempt %d; retrying",
                    method,
                    attempt + 1,
                )
                await asyncio.sleep(0.25 * (2**attempt))
        return None

    def emit_hud(event_type: str, payload: dict[str, Any]) -> None:
        hud.emit(event_type, **payload)

    async def fetch_turn_context(query: str) -> dict[str, Any]:
        raw = await rpc_to_mac(
            "get_turn_context",
            json.dumps({"query": query}),
        )
        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("turn context returned invalid JSON")
            return {}
        if not isinstance(result, dict) or not result.get("ok", True):
            return {}
        result.pop("ok", None)
        return result

    async def call_tool(
        tool_name: str,
        arguments: dict[str, Any],
        *,
        startup: bool = False,
    ) -> dict[str, Any]:
        rpc = startup_rpc if startup else rpc_to_mac
        raw = await rpc(
            "tool_call",
            json.dumps({"tool": tool_name, "arguments": arguments}),
        )
        if not raw:
            return {"ok": False, "error": "no response from local service"}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": "bad response from local service"}

    def build_proxy(tool_name: str, params: list[dict[str, Any]]):
        """Build a callable with a real signature so function_tool can introspect it."""
        parameters: list[inspect.Parameter] = []
        annotations: dict[str, Any] = {}
        for p in params:
            py_type = PARAM_TYPE_MAP.get(p.get("type", "string"), str)
            required = p.get("required", True)
            if required:
                param = inspect.Parameter(
                    p["name"],
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=py_type,
                )
                annotations[p["name"]] = py_type
            else:
                opt = py_type | None
                param = inspect.Parameter(
                    p["name"],
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=opt,
                )
                annotations[p["name"]] = opt
            parameters.append(param)
        annotations["return"] = str

        async def _proxy(**kwargs: Any) -> str:
            logger.info("tool_call name=%s", tool_name)
            envelope = await call_tool(tool_name, kwargs)
            logger.info("tool_result name=%s ok=%s", tool_name, envelope.get("ok"))
            if not envelope.get("ok"):
                return envelope.get("error") or f"{tool_name} failed"
            result = {
                "message": envelope.get("spoken"),
                "data": envelope.get("data"),
            }
            return json.dumps(
                {key: value for key, value in result.items() if value is not None}
            )

        _proxy.__name__ = tool_name
        _proxy.__signature__ = inspect.Signature(
            parameters=parameters, return_annotation=str
        )
        _proxy.__annotations__ = annotations
        return _proxy

    async def fetch_tools() -> tuple[list, list[dict[str, Any]]]:
        envelope = await call_tool("__list__", {}, startup=True)
        if not envelope.get("ok"):
            logger.warning("tool list fetch failed: %s", envelope.get("error"))
            return [], []
        manifests = (envelope.get("data") or {}).get("tools") or []
        built = []
        for m in manifests:
            try:
                proxy = build_proxy(m["name"], m.get("parameters") or [])
                built.append(
                    function_tool(
                        proxy,
                        name=m["name"],
                        description=m.get("description", ""),
                    )
                )
            except Exception:
                logger.exception("failed to build tool %s", m.get("name"))
        logger.info(
            "registered %d tools: %s", len(built), [m["name"] for m in manifests]
        )
        return built, manifests

    async def fetch_capability_catalog() -> dict[str, Any]:
        raw = await startup_rpc(
            "capability_call",
            json.dumps({"operation": "list"}),
        )
        if not raw:
            logger.warning("capability catalog fetch failed: no response")
            return {}
        try:
            catalog = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("capability catalog returned invalid JSON")
            return {}
        if not catalog.get("ok"):
            logger.warning(
                "capability catalog fetch failed: %s",
                catalog.get("error"),
            )
            return {}
        return catalog

    profile: dict = {}
    location: dict = {}

    # Wait for the Mac participant before any RPC. Fetch the initial tool set
    # and capability catalog one at a time before Agent construction. LiveKit
    # RPC startup is more reliable when the Mac receives one request at a time.
    # Ambient context can then load alongside session.start.
    await wait_for_mac()
    hud.set_destination(mac_identity())
    primitive_tools, primitive_manifests = await fetch_tools()
    capability_catalog = await fetch_capability_catalog()
    context_task = asyncio.create_task(startup_rpc("get_context"))
    supported_capabilities = capability_catalog.get("capabilities") or []
    capability_tool = build_capability_tool(
        rpc_to_mac,
        supported_capabilities,
        event_sink=emit_hud,
    )
    action_catalog = ActionCatalog(
        merge_action_manifests(capability_catalog, primitive_manifests)
    )
    action_tool = build_action_tool(
        rpc_to_mac,
        call_tool,
        action_catalog,
        event_sink=emit_hud,
    )
    fallback_tools = primitive_tools
    tools_list: list[llm.Tool | llm.Toolset] = [
        action_tool,
        capability_tool,
    ]
    if fallback_tools:
        tools_list.append(
            ToolProxyToolset(
                id="friday_primitives",
                tools=fallback_tools,
                max_results=4,
                search_description=(
                    "Search Friday's low-level fallback primitives. Use this "
                    "for weather, file writes, UI inspection, or when the "
                    "high-level capability tools do not support the task."
                ),
                query_description=("Describe the exact primitive action you need."),
                call_description=(
                    "Call one primitive returned by tool_search using its "
                    "exact name and arguments."
                ),
            )
        )
    logger.info(
        "exposed actions=%s capabilities=%s plus %d proxied primitives",
        action_catalog.action_ids,
        supported_capabilities,
        len(fallback_tools),
    )

    friday_agent = FridayAgent(
        instructions=render_instructions(profile, location),
        tools=tools_list,
        fast_llm=fast_llm,
        complex_llm=complex_llm,
        action_catalog=action_catalog,
        complex_extra_kwargs=complex_extra_kwargs,
        turn_context_provider=fetch_turn_context,
        hud_event_sink=emit_hud,
    )

    @ctx.room.local_participant.register_rpc_method("profile_updated")
    async def on_profile_updated(data: rtc.RpcInvocationData) -> str:
        nonlocal profile
        try:
            payload = json.loads(data.payload)
        except json.JSONDecodeError:
            return "bad payload"
        # Mac forwards the raw event {"type": "profile_updated", "profile": {...}}.
        profile = payload.get("profile", payload) or {}
        try:
            friday_agent.update_ambient_context(profile, location)
            await friday_agent.update_instructions(
                render_instructions(profile, location)
            )
        except Exception:
            logger.exception("failed to update agent instructions")
        return "ok"

    @ctx.room.local_participant.register_rpc_method("location_updated")
    async def on_location_updated(data: rtc.RpcInvocationData) -> str:
        nonlocal location
        try:
            location = json.loads(data.payload) or {}
        except json.JSONDecodeError:
            return "bad payload"
        try:
            friday_agent.update_ambient_context(profile, location)
            await friday_agent.update_instructions(
                render_instructions(profile, location)
            )
        except Exception:
            logger.exception("failed to update location instructions")
        return "ok"

    def cancel_followup() -> None:
        nonlocal followup_task
        if followup_task and not followup_task.done():
            followup_task.cancel()
        followup_task = None

    async def drain_gate_soon() -> None:
        # The Mac mutes its mic during return_to_sleep; give the last
        # in-flight frames a beat to arrive, then discard them so the tail
        # of this turn can't leak into the start of the next.
        await asyncio.sleep(0.5)
        if gate is not None:
            await gate.drain_stale()

    async def run_followup_window() -> None:
        nonlocal turn_active
        await rpc_to_mac("set_assistant_state", "followupWindow")
        try:
            await asyncio.sleep(FOLLOWUP_SECONDS)
        except asyncio.CancelledError:
            return
        session.input.set_audio_enabled(False)
        turn_active = False
        hud.emit("turn_finished")
        await rpc_to_mac("return_to_sleep")
        await drain_gate_soon()

    @ctx.room.local_participant.register_rpc_method("activate_turn")
    async def on_activate_turn(data: rtc.RpcInvocationData) -> str:
        nonlocal turn_active
        logger.info("activate_turn from %s", data.caller_identity)
        cancel_followup()
        turn_active = True
        hud.begin_turn()
        # Wait briefly for the pre-roll byte stream (sent just before this
        # RPC) so speech spoken across the wake word is prepended in order.
        frames = await preroll_receiver.take(timeout=1.0)
        if frames and gate is not None:
            gate.queue_preroll(frames)
        session.input.set_audio_enabled(True)
        return "ok"

    @ctx.room.local_participant.register_rpc_method("cancel_turn")
    async def on_cancel_turn(data: rtc.RpcInvocationData) -> str:
        nonlocal turn_active
        logger.info("cancel_turn from %s", data.caller_identity)
        cancel_followup()
        turn_active = False
        session.interrupt()
        session.input.set_audio_enabled(False)
        asyncio.create_task(drain_gate_soon())
        return "ok"

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        nonlocal followup_task, turn_active
        new_state = getattr(ev, "new_state", None)
        old_state = getattr(ev, "old_state", None)
        logger.info("agent_state %s -> %s", old_state, new_state)
        if new_state == "thinking":
            cancel_followup()
            asyncio.create_task(rpc_to_mac("set_assistant_state", "thinking"))
        elif new_state == "speaking":
            cancel_followup()
            asyncio.create_task(rpc_to_mac("set_assistant_state", "speaking"))
        elif new_state == "listening":
            if old_state == "speaking":
                followup_task = asyncio.create_task(run_followup_window())
            elif turn_active:
                asyncio.create_task(rpc_to_mac("set_assistant_state", "listening"))

    @session.on("user_input_transcribed")
    def _on_user_input(ev) -> None:
        cancel_followup()
        transcript = str(getattr(ev, "transcript", "") or "")
        if transcript:
            hud.emit(
                "transcript",
                role="user",
                text=transcript,
                isFinal=bool(getattr(ev, "is_final", False)),
            )

    @session.on("conversation_item_added")
    def _on_conversation_item(ev) -> None:
        item = getattr(ev, "item", None)
        if not isinstance(item, llm.ChatMessage):
            return
        text = item.text_content or ""
        role = str(item.role)
        if text and role in {"user", "assistant"}:
            hud.emit(
                "transcript",
                role=role,
                text=text,
                isFinal=True,
                interrupted=bool(getattr(item, "interrupted", False)),
            )
        metrics_report = getattr(item, "metrics", None)
        if role != "assistant" or metrics_report is None:
            return
        latency: dict[str, float] = {}
        for key in (
            "e2e_latency",
            "llm_node_ttft",
            "tts_node_ttfb",
            "playback_latency",
        ):
            try:
                value = metrics_report.get(key)
            except (AttributeError, TypeError):
                value = None
            if isinstance(value, int | float):
                latency[key] = round(float(value) * 1000, 1)
        if latency:
            hud.emit("latency", unit="ms", metrics=latency)

    async def rearm_mac_after_session_error() -> None:
        await rpc_to_mac("return_to_sleep")
        await drain_gate_soon()

    @session.on("error")
    def _on_session_error(ev) -> None:
        nonlocal turn_active
        error = getattr(ev, "error", None)
        hud.emit(
            "error",
            message=str(error or "Friday encountered an error."),
            recoverable=bool(getattr(error, "recoverable", True)),
        )
        if not turn_active or getattr(error, "recoverable", True):
            return

        logger.error("active turn failed; returning Mac to wake-word mode")
        cancel_followup()
        turn_active = False
        session.input.set_audio_enabled(False)
        asyncio.create_task(rearm_mac_after_session_error())

    audio_input_options = room_io.AudioInputOptions()
    await session.start(
        room=ctx.room,
        agent=friday_agent,
        room_options=room_io.RoomOptions(audio_input=audio_input_options),
    )

    # Wrap RoomIO's audio input so pre-roll can be prepended per turn. The
    # wrapper also keeps the source attached while audio is "disabled", so
    # frames sent after the Mac unmutes but before activate_turn lands are
    # buffered rather than dropped.
    assert session.input.audio is not None
    gate = PreRollAudioInput(
        session.input.audio, sample_rate=audio_input_options.sample_rate
    )
    session.input.audio = gate

    # Context fetch was kicked off in parallel with session.start above; apply
    # it now so the first user turn has profile and location-aware instructions.
    # The mic is gated on activate_turn.
    raw_context = await context_task
    if raw_context:
        try:
            context = json.loads(raw_context)
            profile = context.get("profile") or {}
            location = context.get("location") or {}
        except json.JSONDecodeError:
            logger.warning("get_context returned non-JSON: %r", raw_context)

    try:
        friday_agent.update_ambient_context(profile, location)
        await friday_agent.update_instructions(render_instructions(profile, location))
    except Exception:
        logger.exception("failed to apply initial context")

    if os.getenv("FRIDAY_TEST_MODE") == "1":
        logger.info("FRIDAY_TEST_MODE=1 - mic enabled, greeting on connect")
        await session.generate_reply(
            instructions="Briefly greet the user and say you are ready."
        )
    else:
        session.input.set_audio_enabled(False)


if __name__ == "__main__":
    agents.cli.run_app(server)
