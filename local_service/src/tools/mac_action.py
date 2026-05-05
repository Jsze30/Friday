from __future__ import annotations

import asyncio
import shutil

from .base import ToolParam, ToolResult, tool

ACTIONS = {"open_app", "media_play_pause", "set_volume"}


@tool(
    name="run_mac_action",
    description=(
        "Run a safe, allowlisted Mac action. Supported actions: "
        "'open_app' (target = app name), 'media_play_pause' (no target), "
        "'set_volume' (target = 0-100 percent)."
    ),
    parameters=[
        ToolParam(
            name="action",
            type="string",
            description="One of: open_app, media_play_pause, set_volume.",
        ),
        ToolParam(
            name="target",
            type="string",
            description="Action argument (app name, volume percent, etc.). Omit for media_play_pause.",
            required=False,
        ),
    ],
    permission="low_risk_write",
)
async def run_mac_action(action: str, target: str | None = None) -> ToolResult:
    if action not in ACTIONS:
        return ToolResult(
            spoken=f"I can't run that action.",
            data={"error": "unknown_action", "action": action},
        )

    if action == "open_app":
        if not target:
            return ToolResult(spoken="Which app?", data={"error": "missing_target"})
        rc, err = await _run("open", "-a", target)
        if rc != 0:
            return ToolResult(
                spoken=f"I couldn't open {target}.",
                data={"error": "open_failed", "stderr": err},
            )
        return ToolResult(spoken=f"Opening {target}.", data={"app": target})

    if action == "media_play_pause":
        app = await _running_media_app()
        if app is None:
            return ToolResult(
                spoken="No media app is running.",
                data={"error": "no_media_app"},
            )
        rc, err = await _osascript(f'tell application "{app}" to playpause')
        if rc != 0:
            return ToolResult(
                spoken="I couldn't control playback.",
                data={"error": "media_failed", "stderr": err, "app": app},
            )
        return ToolResult(spoken="Done.", data={"action": "play_pause", "app": app})

    if action == "set_volume":
        pct = _parse_percent(target)
        if pct is None:
            return ToolResult(
                spoken="Volume should be a number from 0 to 100.",
                data={"error": "bad_volume", "target": target},
            )
        rc, err = await _osascript(f"set volume output volume {pct}")
        if rc != 0:
            return ToolResult(
                spoken="I couldn't change the volume.",
                data={"error": "volume_failed", "stderr": err},
            )
        return ToolResult(spoken=f"Volume set to {pct} percent.", data={"volume": pct})

    return ToolResult(spoken="I can't run that action.", data={"error": "unhandled"})


async def _run(*argv: str) -> tuple[int, str]:
    exe = shutil.which(argv[0])
    if exe is None:
        return 127, f"{argv[0]} not found"
    proc = await asyncio.create_subprocess_exec(
        exe,
        *argv[1:],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return proc.returncode or 0, stderr.decode(errors="replace").strip()


async def _osascript(script: str) -> tuple[int, str]:
    return await _run("osascript", "-e", script)


_MEDIA_APPS = ("Spotify", "Music")


async def _running_media_app() -> str | None:
    for app in _MEDIA_APPS:
        rc, _ = await _run("pgrep", "-x", app)
        if rc == 0:
            return app
    return None


def _parse_percent(value: str | None) -> int | None:
    if value is None:
        return None
    s = value.strip().rstrip("%").strip()
    try:
        n = int(float(s))
    except ValueError:
        return None
    if not 0 <= n <= 100:
        return None
    return n
