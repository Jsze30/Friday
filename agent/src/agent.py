from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, function_tool
from livekit.plugins import anthropic, deepgram, silero

FOLLOWUP_SECONDS = 5.0

load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")

logger = logging.getLogger("friday-agent")

AGENT_NAME = "friday-agent"

BASE_INSTRUCTIONS = """You are Friday, a personal voice assistant on the user's Mac.
Speak naturally and concisely. Avoid markdown, lists, or special characters —
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


server = AgentServer()


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(
        stt=deepgram.STTv2(
            model="flux-general-en",
            eot_threshold=0.5,
            eot_timeout_ms=1500,
        ),
        llm=anthropic.LLM(model="claude-haiku-4-5-20251001"),
        tts=deepgram.TTS(model="aura-2-athena-en"),
        vad=ctx.proc.userdata["vad"],
    )

    await ctx.connect()

    followup_task: asyncio.Task | None = None

    def mac_identity() -> str | None:
        for p in ctx.room.remote_participants.values():
            if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                ident = p.identity
                return ident if isinstance(ident, str) else getattr(ident, "stringValue", None) or str(ident)
        return None

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

    async def wait_for_mac(timeout: float = 5.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if mac_identity():
                return
            await asyncio.sleep(0.1)

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
    profile_task = asyncio.create_task(rpc_to_mac("get_profile"))
    tools_list = await fetch_tools()

    friday_agent = Agent(
        instructions=render_instructions(profile),
        tools=tools_list,
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

    async def run_followup_window() -> None:
        await rpc_to_mac("set_assistant_state", "followupWindow")
        try:
            await asyncio.sleep(FOLLOWUP_SECONDS)
        except asyncio.CancelledError:
            return
        session.input.set_audio_enabled(False)
        await rpc_to_mac("return_to_sleep")

    @ctx.room.local_participant.register_rpc_method("activate_turn")
    async def on_activate_turn(data: rtc.RpcInvocationData) -> str:
        logger.info("activate_turn from %s", data.caller_identity)
        cancel_followup()
        session.input.set_audio_enabled(True)
        return "ok"

    @ctx.room.local_participant.register_rpc_method("cancel_turn")
    async def on_cancel_turn(data: rtc.RpcInvocationData) -> str:
        logger.info("cancel_turn from %s", data.caller_identity)
        cancel_followup()
        session.interrupt()
        session.input.set_audio_enabled(False)
        return "ok"

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        nonlocal followup_task
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
            else:
                asyncio.create_task(rpc_to_mac("set_assistant_state", "listening"))

    @session.on("user_input_transcribed")
    def _on_user_input(_ev) -> None:
        cancel_followup()

    await session.start(room=ctx.room, agent=friday_agent)

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
        logger.info("FRIDAY_TEST_MODE=1 — mic enabled, greeting on connect")
        await session.generate_reply(
            instructions="Briefly greet the user and say you are ready."
        )
    else:
        session.input.set_audio_enabled(False)


if __name__ == "__main__":
    agents.cli.run_app(server)
