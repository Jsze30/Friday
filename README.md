# Friday

Friday is a personal voice assistant for macOS.
It runs as a Dock and menu bar app, wakes on a local wake word, joins a LiveKit Cloud room, and talks through a deployed LiveKit agent that uses speech-to-text, an LLM, text-to-speech, and a local tool registry.

The important design choice is that the cloud agent never connects directly to the user's machine.
Local capabilities stay on the Mac.
The cloud agent can only reach them through LiveKit RPC calls that are brokered by the Swift app and served by a localhost-only Python service.

## What It Does

- Listens locally for the wake phrase, currently `friday`.
- Runs a spoken assistant powered by Deepgram STT/TTS, routed OpenAI models, and Silero VAD.
- Executes requested actions immediately without confirmation prompts while keeping path and network safeguards.
- Runs multi-step native computer goals through a bounded observe, act, wait, and verify loop.
- Uses macOS Accessibility for structured native app controls, with visual grounding only when those controls are unavailable.
- Runs OCR locally with Apple Vision and sends one compressed active-window image to the cloud only for questions that require visual reasoning.
- Supplies a live location, a fresh local clock, and a working-context snapshot (frontmost app, focused window or document, active URL, upcoming calendar events) as ambient context.
- Stores stable profile facts and explicit reference memories such as "the project means Friday" locally.
- Shows a non-activating visual HUD whose animated orb reflects Friday's current state.
- Returns to sleep after the agent answers and a short follow-up window expires.

## High-Level Architecture

Three coordinated components:

- **`mac/`** - the user-facing macOS Dock and menu bar app (Swift, LiveKit Swift SDK 2.x). It spawns the local service, connects to LiveKit, drives the microphone and HUD, and is the only bridge between LiveKit RPC and localhost HTTP.
- **`local_service/`** - a localhost-only Python FastAPI helper started as a child of the Mac app. It owns wake-word detection, LiveKit token minting, profile and memory storage, and the tool/action/capability registry the cloud agent calls back into.
- **`agent/`** - the LiveKit Cloud agent (Python `livekit-agents`) deployed as `friday-agent`. It owns the speech and reasoning pipeline but has no direct access to the user's machine.

```text
Wake audio on Mac
      |
      v
local_service WakeDetector  --(WS /events: wake_detected)-->  Swift app
                                                                  |
                                          (LiveKit RPC: activate_turn)
                                                                  v
                              LiveKit Cloud room <---- deployed friday-agent worker
                                                                  ^
                          (LiveKit RPC: tool_call / capability_call / get_context)
                                                                  |
                                                              Swift app
                                                                  |
                                                       (HTTP localhost)
                                                                  v
                                    local_service tools / profile / context / token API
```

The data and control boundary is intentional:

- `local_service` is localhost-only and unauthenticated.
- The cloud agent cannot call `local_service` over the network.
- Swift is the only bridge between LiveKit RPC and localhost HTTP.
- Local tools are defined by `local_service`, not by the cloud agent.

The cloud agent exposes a small, stable tool surface to the model - `run_action` for fast known operations, `run_capability` for multi-step work, and `tool_search` / `call_tool` for fallback primitives - so adding new integrations does not change the agent's model-facing tools.

## Capabilities

The capability layer is the normal path for multi-step work. The model calls one stable tool instead of choosing from every low-level action.

| Capability | Default provider | What it does |
| --- | --- | --- |
| `files` | `files-direct` | Lists, reads, or searches allowed local files. |
| `research` | `research-direct` | Searches the public web and reads up to five sources in parallel. |
| `web` | `research-direct` | Reads one exact public URL or performs a web search. |
| `coding` | `codex-readonly` | Runs an ephemeral Codex specialist in a read-only sandbox. |
| `computer` | `computer-native` | Operates native Mac applications through a bounded observe, act, wait, and verify loop. |
| `music` | `spotify-web-api` | Connects Spotify and controls playback, tracks, playlists, queue, shuffle, repeat, and volume. |

The broker filters providers by capability and permission, checks availability, then ranks them by priority, reliability, and latency.
It verifies every result and automatically falls back to the next provider when execution or verification fails.
Additional API, MCP, or specialist-agent providers can use the same contract without adding another model-facing tool.
