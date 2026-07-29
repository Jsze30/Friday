from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

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
)
from livekit.agents.voice.room_io import RoomInputOptions
from livekit.plugins import anthropic, deepgram, openai, silero
from openai.types import Reasoning as OpenAIReasoning

from model_router import route_request
from turn_gate import PreRollAudioInput, PreRollReceiver

FOLLOWUP_SECONDS = 5.0

load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")

logger = logging.getLogger("friday-agent")

AGENT_NAME = "friday-agent"
FAST_MODEL = os.getenv("FRIDAY_FAST_MODEL", "gpt-4.1-nano")
COMPLEX_MODEL = os.getenv("FRIDAY_COMPLEX_MODEL", "gpt-5.6-terra")
COMPLEX_EFFORT = os.getenv("FRIDAY_COMPLEX_EFFORT", "low")

BASE_INSTRUCTIONS = """You are Jarvis, a personal voice assistant on the user's Mac.
Your name is Jarvis. Never correct the user for calling you Jarvis.
Speak naturally and concisely. Avoid markdown, lists, or special characters -
your replies are spoken aloud. Default to one or two short sentences unless the
user explicitly asks for detail.

Use the `remember` tool only for stable user facts and preferences (name, units,
recurring people or places, long-term goals). Do not save transient context like
the current task, today's plans, or one-off questions."""

PARAM_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def render_instructions(profile: dict | None) -> str:
    facts = (profile or {}).get("facts") or {}
    if not facts:
        return BASE_INSTRUCTIONS
    lines = [f"- {k}: {v}" for k, v in facts.items()]
    return BASE_INSTRUCTIONS + "\n\n<profile>\n" + "\n".join(lines) + "\n</profile>"


class JarvisAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        tools: list[llm.Tool],
        fast_llm: llm.LLM,
        complex_llm: llm.LLM,
        complex_extra_kwargs: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            instructions=instructions,
            tools=tools,
            llm=fast_llm,
        )
        self._fast_llm = fast_llm
        self._complex_llm = complex_llm
        self._complex_extra_kwargs = complex_extra_kwargs or {}

    @staticmethod
    def _latest_user_text(chat_ctx: llm.ChatContext) -> str | None:
        for item in reversed(chat_ctx.items):
            if isinstance(item, llm.ChatMessage) and item.role == "user":
                return item.text_content
        return None

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk]:
        user_text = self._latest_user_text(chat_ctx)
        decision = route_request(user_text)
        selected_llm = (
            self._complex_llm if decision.route == "complex" else self._fast_llm
        )
        extra_kwargs = (
            self._complex_extra_kwargs if decision.route == "complex" else {}
        )

        logger.info(
            "llm_route route=%s model=%s reason=%s",
            decision.route,
            selected_llm.model,
            decision.reason,
        )

        async with selected_llm.chat(
            chat_ctx=chat_ctx,
            tools=tools,
            tool_choice=model_settings.tool_choice,
            conn_options=self.session.conn_options.llm_conn_options,
            extra_kwargs=extra_kwargs,
        ) as stream:
            async for chunk in stream:
                yield chunk


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
    )

    await ctx.connect()

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
                return ident if isinstance(ident, str) else getattr(ident, "stringValue", None) or str(ident)
        return None

    def mac_rpc_ready() -> bool:
        for p in ctx.room.remote_participants.values():
            if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                return p.attributes.get("friday.rpcReady") == "true"
        return False

    async def rpc_to_mac(method: str, payload: str = "") -> str | None:
        identity = mac_identity()
        if not identity:
            logger.warning("no mac participant for rpc %s", method)
            return None
        try:
            return await ctx.room.local_participant.perform_rpc(
                destination_identity=identity,
                method=method,
                payload=payload,
            )
        except Exception as e:
            logger.warning("rpc %s failed: %s", method, e)
            return None

    async def wait_for_mac(timeout: float = 15.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if mac_identity() and mac_rpc_ready():
                return
            await asyncio.sleep(0.1)
        logger.warning("mac participant did not signal RPC readiness before timeout")

    async def call_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = await rpc_to_mac(
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
            envelope = await call_tool(tool_name, kwargs)
            if not envelope.get("ok"):
                return envelope.get("error") or f"{tool_name} failed"
            return envelope.get("spoken") or "done"

        _proxy.__name__ = tool_name
        _proxy.__signature__ = inspect.Signature(
            parameters=parameters, return_annotation=str
        )
        _proxy.__annotations__ = annotations
        return _proxy

    async def fetch_tools() -> list:
        envelope = await call_tool("__list__", {})
        if not envelope.get("ok"):
            logger.warning("tool list fetch failed: %s", envelope.get("error"))
            return []
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
        logger.info("registered %d tools: %s", len(built), [m["name"] for m in manifests])
        return built

    profile: dict = {}

    # Wait for the Mac participant before any RPC. Tools must be present at
    # Agent construction (post-start tool mutation is not part of the public
    # API), but the profile fetch can run concurrently with session.start and
    # be applied via update_instructions once it arrives.
    await wait_for_mac()
    tools_list = await fetch_tools()
    profile_task = asyncio.create_task(rpc_to_mac("get_profile"))

    friday_agent = JarvisAgent(
        instructions=render_instructions(profile),
        tools=tools_list,
        fast_llm=fast_llm,
        complex_llm=complex_llm,
        complex_extra_kwargs=complex_extra_kwargs,
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
            await friday_agent.update_instructions(render_instructions(profile))
        except Exception:
            logger.exception("failed to update agent instructions")
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
        await rpc_to_mac("return_to_sleep")
        await drain_gate_soon()

    @ctx.room.local_participant.register_rpc_method("activate_turn")
    async def on_activate_turn(data: rtc.RpcInvocationData) -> str:
        nonlocal turn_active
        logger.info("activate_turn from %s", data.caller_identity)
        cancel_followup()
        turn_active = True
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
    def _on_user_input(_ev) -> None:
        cancel_followup()

    async def rearm_mac_after_session_error() -> None:
        await rpc_to_mac("return_to_sleep")
        await drain_gate_soon()

    @session.on("error")
    def _on_session_error(ev) -> None:
        nonlocal turn_active
        error = getattr(ev, "error", None)
        if not turn_active or getattr(error, "recoverable", True):
            return

        logger.error("active turn failed; returning Mac to wake-word mode")
        cancel_followup()
        turn_active = False
        session.input.set_audio_enabled(False)
        asyncio.create_task(rearm_mac_after_session_error())

    room_input_options = RoomInputOptions()
    await session.start(
        room=ctx.room,
        agent=friday_agent,
        room_input_options=room_input_options,
    )

    # Wrap RoomIO's audio input so pre-roll can be prepended per turn. The
    # wrapper also keeps the source attached while audio is "disabled", so
    # frames sent after the Mac unmutes but before activate_turn lands are
    # buffered rather than dropped.
    assert session.input.audio is not None
    gate = PreRollAudioInput(
        session.input.audio, sample_rate=room_input_options.audio_sample_rate
    )
    session.input.audio = gate

    # Profile fetch was kicked off in parallel with session.start above; apply
    # it now so the first user turn has profile-aware instructions. The mic is
    # gated on activate_turn, so this finishes well before the first turn.
    raw_profile = await profile_task
    if raw_profile:
        try:
            profile = json.loads(raw_profile)
            await friday_agent.update_instructions(render_instructions(profile))
        except json.JSONDecodeError:
            logger.warning("get_profile returned non-JSON: %r", raw_profile)
        except Exception:
            logger.exception("failed to apply initial profile")

    if os.getenv("FRIDAY_TEST_MODE") == "1":
        logger.info("FRIDAY_TEST_MODE=1 - mic enabled, greeting on connect")
        await session.generate_reply(
            instructions="Briefly greet the user and say you are ready."
        )
    else:
        session.input.set_audio_enabled(False)


if __name__ == "__main__":
    agents.cli.run_app(server)
