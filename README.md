# Friday

Friday is a personal voice assistant for macOS. It runs as a menu bar app, wakes
on a local wake word, joins a LiveKit Cloud room, and talks through a deployed
LiveKit agent that uses speech-to-text, an LLM, text-to-speech, and a local tool
registry.

The important design choice is that the cloud agent never connects directly to
the user's machine. Local capabilities stay on the Mac. The cloud agent can only
reach them through LiveKit RPC calls that are brokered by the Swift app and
served by a localhost-only Python service.

## What It Does

- Listens locally for the wake phrase, currently `friday`.
- Connects the Mac app to a LiveKit Cloud room when the app starts.
- Dispatches the `friday-agent` LiveKit worker into that room.
- Keeps one echo-cancelled microphone capture warm, sends silence while sleeping, and forwards a two-second pre-roll when Friday wakes.
- Runs a spoken assistant powered by Deepgram STT/TTS, routed OpenAI models, and Silero VAD.
- Gives the assistant one fast `run_action` tool backed by integration-declared action manifests.
- Gives the assistant one high-level capability runner backed by ranked providers.
- Keeps the reusable primitive kernel behind two fallback discovery tools instead of showing every primitive to the model.
- Runs slow capabilities in cancellable background tasks so the live voice loop can keep responding.
- Uses macOS Accessibility as a final fallback for unsupported app controls.
- Executes requested actions immediately without confirmation prompts while keeping path and network safeguards.
- Supplies a live human-readable Core Location place and a fresh local clock as ambient context.
- Stores stable profile facts locally and injects them into the agent prompt.
- Shows a non-activating HUD with live user text, streamed Friday text, state, resolved references, action progress, and end-to-end latency.
- Reads a small local working-context snapshot containing the frontmost app, focused window or document, active URL when available, and upcoming calendar events.
- Stores explicit reference memories such as "the project means Friday" in a local SQLite database.
- Returns to sleep after the agent answers and a short follow-up window expires.

## Repository Layout

```text
.
|-- agent/                 # LiveKit Cloud agent, Python
|   |-- src/agent.py       # Agent entrypoint, model pipeline, RPC/tool bridge
|   |-- livekit.toml       # LiveKit Cloud project/agent deployment metadata
|   |-- Dockerfile         # Deployment image used by `lk agent deploy`
|   |-- pyproject.toml     # Python dependencies for the cloud agent
|   `-- uv.lock
|
|-- local_service/         # Local FastAPI helper, Python
|   |-- src/main.py        # Uvicorn entrypoint, port file, wake detector startup
|   |-- src/routes.py      # HTTP and WebSocket API used by the Swift app
|   |-- src/wake.py        # openWakeWord scoring for echo-cancelled PCM from Swift
|   |-- src/context_store.py # SQLite reference memory and context resolution
|   |-- src/tokens.py      # LiveKit AccessToken minting with agent dispatch
|   |-- src/profile.py     # Local profile storage and update events
|   |-- src/capabilities/  # Provider broker and background task runtime
|   |-- src/tools/         # Tool registry and concrete local tools
|   |-- scripts/           # Setup helpers
|   |-- pyproject.toml
|   `-- uv.lock
|
|-- mac/                   # macOS menu bar app, Swift
|   |-- project.yml        # XcodeGen source of truth
|   `-- Friday/            # App sources
|
|-- AGENTS.md              # Agent/coding instructions for this repository
|-- CLAUDE.md              # Same operational notes for Claude Code
|-- todos.md               # Local roadmap notes
`-- README.md              # This file
```

Generated and local-only directories such as `mac/build/`, Python virtualenvs, and env files are not part of the source architecture.

## Components

### `mac/`: Menu Bar App

The macOS app is the user-facing process. It is built from `mac/project.yml`
with XcodeGen and targets macOS 14 or newer. It is configured as a menu-bar-only
app (`LSUIElement=true`) and uses the LiveKit Swift SDK 2.x.

Main files:

- `mac/Friday/FridayApp.swift` creates the SwiftUI app shell.
- `mac/Friday/AppDelegate.swift` starts the menu bar UI and boot coordinator,
  and stops the Python child process on quit.
- `mac/Friday/MenuBarController.swift` renders status icons and the quit menu.
- `mac/Friday/AppState.swift` tracks assistant states such as `sleeping`,
  `listening`, `thinking`, `acting`, `speaking`, and `error` together with the current HUD content.
- `mac/Friday/HUDPanelController.swift` owns the non-activating floating panel and its lifecycle.
- `mac/Friday/HUDView.swift` renders the compact animated conversation surface.
- `mac/Friday/WorkingContextProvider.swift` captures the active app, focused window, document, URL, and calendar context.
- `mac/Friday/CalendarContextProvider.swift` reads the next 24 hours of calendar events when permission is granted.
- `mac/Friday/BootCoordinator.swift` owns startup orchestration.
- `mac/Friday/LocalServiceProcess.swift` launches `local_service` as a child
  process and reads its selected port.
- `mac/Friday/LocalServiceClient.swift` calls the local FastAPI API and opens
  the local event WebSocket.
- `mac/Friday/LiveKitController.swift` connects to LiveKit, registers RPC handlers, controls the microphone, and routes tool calls.
- `mac/Friday/MacPrimitiveProvider.swift` discovers applications and exposes generic macOS Accessibility inspection and interaction.
- `mac/Friday/LocationProvider.swift` requests Core Location permission, reverse geocodes coordinates, and provides fresh location snapshots to the cloud agent.

Important implementation detail: `LocalServiceProcess.swift` currently contains
hard-coded paths pointing at `/Users/jas/Documents/Coding/friday/local_service`.
If this repository is checked out elsewhere, update `workingDir` and
`pythonPath` before running the app.

### `local_service/`: Local Python Helper

The local service runs only on the user's machine and binds to `127.0.0.1` with
no authentication. The Swift app starts it as a child process; it can also be run
standalone for debugging.

Responsibilities:

- Pick a free localhost port.
- Write the selected port to
  `~/Library/Application Support/Friday/port`.
- Score Swift's echo-cancelled wake audio with openWakeWord.
- Mint LiveKit tokens for the Mac participant.
- Serve profile data from local disk.
- Store and resolve durable personal reference aliases in SQLite.
- Register and execute local tools.
- Rank, execute, verify, retry, and cancel high-level capability providers.
- Publish wake/profile events over a WebSocket consumed by Swift.

Local service API:

| Route | Purpose |
| --- | --- |
| `GET /health` | Returns `{ ok, wakePaused }`. |
| `POST /token` | Mints a LiveKit room token and dispatches the configured agent. |
| `POST /wake/resume` | Resumes wake-word processing after the turn ends. |
| `GET /profile` | Returns the local profile JSON. |
| `PUT /profile` | Replaces/saves the local profile and emits `profile_updated`. |
| `POST /tools/execute` | Executes a tool by name with JSON arguments. |
| `POST /capabilities/execute` | Lists, starts, polls, or cancels a capability task. |
| `POST /context/resolve` | Combines the current Mac snapshot with relevant saved reference memories. |
| `GET /context/references` | Lists saved reference memories. |
| `WS /events` | Streams local events such as `wake_detected` and `profile_updated`. |

### `agent/`: LiveKit Cloud Agent

The cloud agent is a Python LiveKit Agents worker deployed as `friday-agent`.
It owns the speech and reasoning pipeline, but not local machine access.

Current model pipeline in `agent/src/agent.py`:

- STT: Deepgram `flux-general-en`
- LLM: fast and complex OpenAI models selected by the local model router
- TTS: Deepgram `aura-2-athena-en`
- VAD: Silero, loaded in `prewarm`

The agent starts with `BASE_INSTRUCTIONS`, then appends profile facts and a live Core Location snapshot when available.
It injects the current local time on every LLM turn.
It retrieves a bounded local working-context snapshot before every completed user turn.
It injects the snapshot only when the request contains a resolved reference or context-dependent language, preserving preemptive generation for ordinary commands.
It exposes `run_action` for fast deterministic operations and `run_capability` for intelligent multi-step work.
It places dynamically discovered primitives behind LiveKit's fixed `tool_search` and `call_tool` interface.
It publishes structured HUD events over the private `friday.hud` LiveKit text-stream topic.

Deployment metadata:

- LiveKit project subdomain: `friday-kttgg8ym`
- LiveKit agent id: `CA_cQ7pAy9VcZmt`
- Agent name used for dispatch: `friday-agent`

## Runtime Architecture

```text
Wake audio on Mac
      |
      v
local_service WakeDetector
      |
      | WS /events: wake_detected
      v
Swift menu bar app
      |
      | LiveKit RPC: activate_turn
      v
LiveKit Cloud room <---- deployed friday-agent worker
      ^
      |
      | LiveKit RPC: capability_call / tool_call / get_context / get_turn_context
      |
Swift menu bar app
      |
      | HTTP localhost
      v
local_service tools/profile/context/token API

Agent structured HUD events
      |
      | LiveKit text stream: friday.hud
      v
Non-activating Swift HUD
```

The data/control boundary is intentional:

- `local_service` is localhost-only and unauthenticated.
- The cloud agent cannot call `local_service` over the network.
- Swift is the only bridge between LiveKit RPC and localhost HTTP.
- Local tools are defined by `local_service`, not by the cloud agent.

## Boot Sequence

Startup is coordinated by `mac/Friday/BootCoordinator.swift`:

1. The menu bar app launches.
2. `LocalServiceProcess.start()` kills orphaned `python -m src.main` processes,
   clears any stale port file, and starts the local service from its venv.
3. `local_service` picks a free localhost port, writes it to `~/Library/Application Support/Friday/port`, loads openWakeWord, and starts FastAPI.
4. Swift polls the port file, creates a `LocalServiceClient`, and calls
   `GET /health`.
5. Swift calls `POST /token`.
6. `local_service` returns a LiveKit AccessToken with:
   - a fresh room name like `friday-<timestamp>-<hex>`;
   - a Mac participant identity like `mac-<hex>`;
   - publish/subscribe/data permissions;
   - room config dispatching the `friday-agent` agent.
7. Swift connects to LiveKit, publishes one warm microphone track, and replaces outgoing audio with silence while Friday sleeps.
8. Swift registers RPC methods:
   - `return_to_sleep`
   - `set_assistant_state`
   - `get_context`
   - `get_turn_context`
   - `capability_call`
   - `tool_call`
9. LiveKit dispatches the cloud agent into the room.
10. The agent waits for the Mac participant, fetches profile and location together, fetches the action, capability, and primitive manifests, compiles deterministic action routes, wraps primitives in a proxy toolset, and starts the session with audio input disabled.
11. Swift opens `WS /events` to receive wake/profile events from
    `local_service`.

## Turn Lifecycle

1. Swift copies echo-cancelled wake audio to `local_service`, where openWakeWord detects the wake phrase.
2. `local_service` publishes:

   ```json
   {
     "type": "wake_detected",
     "phrase": "friday",
     "timestamp": "...",
     "confidence": 0.95
   }
   ```

3. Swift receives the event over `WS /events`.
4. The HUD appears immediately and Swift sends the two-second pre-roll before calling the agent RPC `activate_turn`.
5. The agent enables its audio gate and listens while the published microphone track remains warm.
6. Partial and final user transcripts stream to the HUD.
7. Before the reply, the agent calls `get_turn_context`, and Swift combines the current Mac and calendar snapshot with locally saved reference memories.
8. For a context-dependent request, the agent injects that bounded context only into the current inference and publishes any resolved references to the HUD.
9. Agent state changes are sent back to Swift with `set_assistant_state`.
10. For a clear registered command without a contextual reference, the local router selects the exact action without another model decision.
11. The agent emits a `run_action` call directly with the exact catalog-produced arguments, skipping the first model call, and the action runs synchronously through its primitive or fastest available provider.
12. If a command contains a resolved phrase such as `the project`, the fast model receives the canonical target and selects the correct action instead of running a misleading raw text route.
13. Action start and completion events appear in the HUD.
14. For intelligent or multi-step work, the LLM calls `run_capability`.
15. The agent starts a local capability task through `capability_call`.
16. Fast tasks return immediately, while slow tasks continue in the background and publish progress to the HUD.
17. For direct primitives, the LLM uses `tool_search` and `call_tool`, then the cloud agent performs a `tool_call` RPC to Swift.
18. Swift forwards action, capability, and primitive calls to their corresponding localhost APIs.
19. `local_service` returns structured results with provider traces or primitive data.
20. Friday's response text streams to the HUD while the agent speaks it.
21. LiveKit's per-turn metrics publish the measured end-of-speech-to-first-response latency to the HUD.
22. After speaking, the agent enters a 5 second follow-up window.
23. If no follow-up input arrives, the agent disables its audio gate, calls `return_to_sleep`, and the HUD fades away.

## HUD And Personal Context Proof Of Concept

The proof of concept is one vertical slice across the Mac app, local service, and cloud agent.
It is intentionally smaller than the full personal context vision.

The HUD is a 440-point non-activating panel positioned below the menu bar on the display containing the pointer.
It remains hidden while Friday sleeps, appears on wake, never becomes the key window, ignores pointer events, and fades after the follow-up window.
Its compact states show listening motion, partial user text, thinking or action detail, streamed assistant text, the highest-value resolved reference, a result summary, and measured end-to-end latency.

The working-context snapshot currently contains:

- Frontmost application name and bundle identifier.
- Focused window title.
- Focused document path or URL when the app exposes it through macOS Accessibility.
- The active browser URL when it is exposed as the focused document.
- Up to five calendar events in the next 24 hours when Calendar access is granted.
- A project inferred from the current document's nearest Git repository.
- Explicit saved reference memories retrieved from local SQLite.

Saved aliases are generic rather than app-specific.
For example, saying `Friday, when I say the project, I mean Friday` stores the mapping through the deterministic `context.remember_reference` action.
Later uses of `the project` retrieve that saved meaning before the model answers.
An explicit saved mapping wins over a conflicting live project guess.

To preview the HUD without speaking, open Friday's menu bar menu and choose `Preview HUD`.
The preview walks through listening, context resolution, acting, speaking, and latency states without running a real action.

After rebuilding the Mac app, restarting the local service, and deploying the agent, an end-to-end smoke test is:

1. Open a file in the Friday repository.
2. Say `Friday, explain this file` and confirm the HUD shows the active document resolution.
3. Say `Friday, when I say the project, I mean Friday` and confirm the memory action completes without confirmation.
4. Restart Friday and say `Friday, open the project` to confirm the saved reference survives.
5. Ask about an upcoming event and confirm the response uses Calendar only after permission is granted.
6. Confirm the HUD displays the measured first-response latency after Friday speaks.

## Action System

The action layer is the fast path for requests whose execution path is already known.
The cloud agent exposes one stable `run_action` tool instead of one model-facing tool per integration or operation.

Each integration declares its actions beside its provider adapter.
An action manifest contains:

- A stable action ID such as `music.pause` or `system.open_app`
- A description and permission
- Typed parameters with optional ranges or choices
- Declarative voice routes with named parameter captures
- Expected latency and routing priority
- An execution target, either a capability provider or a primitive

At room startup, Friday merges provider actions with native Mac primitive actions and compiles the routes in memory.
A clear phrase such as `pause the music` becomes `music.pause` locally, without a separate classification model call.
The shared executor validates every argument, runs the action synchronously, and preserves provider attempts for debugging and fallback.

Spotify currently declares track, playback, queue, shuffle, repeat, volume, and playlist actions.
The native Mac layer declares app, browser URL, and Core Audio actions.
A future Calendar or Slack integration only needs an adapter and action manifests.
It does not need changes to the central agent router.

## Capability System

The capability layer is the normal path for multi-step work.
The model calls one stable tool instead of selecting from every low-level action.

Current capabilities:

| Capability | Default provider | What it does |
| --- | --- | --- |
| `files` | `files-direct` | Lists, reads, or searches allowed local files. |
| `research` | `research-direct` | Searches the public web and reads up to five sources in parallel. |
| `web` | `research-direct` | Reads one exact public URL or performs a web search. |
| `coding` | `codex-readonly` | Runs an ephemeral Codex specialist in a read-only sandbox. |
| `music` | `spotify-web-api` | Connects Spotify and controls playback, tracks, playlists, queue, shuffle, repeat, and volume. |

The broker filters providers by capability and permission, checks availability, then ranks them by priority, reliability, and latency.
It verifies every result and automatically falls back to the next provider when execution or verification fails.
Every result includes the selected provider, elapsed time, and all attempts.

Capability tasks are stored in memory for a bounded time.
The operations are `list`, `action`, `start`, `status`, and `cancel`.
Read-only work can run in the background, and LiveKit exposes running-task and cancellation controls automatically.

Additional API, MCP, or specialist-agent providers can use the same contract without adding another model-facing tool.
Set `FRIDAY_CAPABILITY_PROVIDERS_JSON` to a JSON array of configured command providers.
Each command receives one JSON request on standard input and must return `{"summary":"...","data":{...}}` on standard output.
For example:

```json
[
  {
    "id": "calendar-agent",
    "name": "calendar specialist",
    "capabilities": ["calendar"],
    "permission": "low_risk_write",
    "command": ["/absolute/path/to/calendar-agent"],
    "priority": 90,
    "timeout_seconds": 120,
    "actions": [
      {
        "id": "calendar.create_event",
        "capability": "calendar",
        "operation": "create_event",
        "description": "Create a calendar event.",
        "parameters": [
          {
            "name": "title",
            "type": "string",
            "required": true
          }
        ],
        "routes": [
          {
            "pattern": "create\\s+(?P<title>.+?)\\s+on\\s+my\\s+calendar"
          }
        ]
      }
    ]
  }
]
```

Configured command providers are trusted local extensions.
They can declare read-only or write capabilities and actions in the same configuration, so adding their routes does not require an agent code change.
The built-in Spotify provider uses a locally enforced `low_risk_write` policy for playback controls.
Requested writes execute immediately through bounded primitives.

## Primitive System

Most primitives live in `local_service/src/tools/`.
Each module is imported automatically by `tools.load_all()` during FastAPI startup.
Registration happens through the `@tool` decorator, so there is no central Python list to edit.
The app and Accessibility primitives live in `mac/Friday/MacPrimitiveProvider.swift` because they must execute inside the signed Mac app.

The cloud agent discovers primitives once per room session by calling the special tool name `__list__`.
Swift merges the local service and native Mac manifests before returning them.
The model sees only `tool_search` and `call_tool` until it searches for a fallback primitive.
Each manifest includes:

- `name`
- `description`
- `permission`
- `parameters`
- Optional `actions` that make selected primitive operations available through the deterministic action layer

Parameter types must be one of:

- `string`
- `integer`
- `number`
- `boolean`
- `array`

The agent maps these to Python type annotations before passing wrappers to
LiveKit `function_tool`.

Tool result envelope:

```json
{
  "ok": true,
  "spoken": "The text the agent can speak.",
  "data": {},
  "error": null
}
```

Current primitive kernel:

| Tool | Permission | What it does |
| --- | --- | --- |
| `inspect_path` | `read_only` | Reads an allowed text file or lists Desktop, Documents, Downloads, or the project. |
| `search_files` | `read_only` | Recursively searches file and folder names below an allowed root. |
| `create_directory` | `low_risk_write` | Creates a folder inside an allowed root. |
| `write_file` | `sensitive` | Atomically replaces an allowed text file immediately. |
| `move_path` | `sensitive` | Moves or renames a file or folder immediately. |
| `trash_path` | `sensitive` | Moves an item immediately to the recoverable macOS Trash. |
| `run_process` | `sensitive` | Runs one executable directly without a shell. |
| `run_applescript` | `sensitive` | Controls scriptable Mac apps immediately. |
| `web_search` | `read_only` | Searches the public web and returns structured results. |
| `fetch_url` | `read_only` | Fetches a public HTTP or HTTPS URL while blocking local and private addresses. |
| `list_apps` | `read_only` | Lists installed or running Mac applications. |
| `open_app` | `low_risk_write` | Launches or activates any installed Mac application. |
| `open_path` | `low_risk_write` | Opens a local file, folder, or project in its default app or a named app. |
| `open_url` | `low_risk_write` | Opens an HTTP or HTTPS URL in Arc or another installed browser. |
| `quit_app` | `low_risk_write` | Gracefully asks a running application to quit immediately. |
| `get_volume` | `read_only` | Reads native Core Audio output volume and mute state. |
| `set_volume` | `low_risk_write` | Sets native Core Audio output volume from 0 to 100. |
| `mute_audio` | `low_risk_write` | Mutes or unmutes the default Core Audio output device. |
| `inspect_ui` | `read_only` | Discovers the accessible controls in any running Mac application. |
| `interact_ui` | `low_risk_write` | Performs a discovered Accessibility action immediately. |

Requested actions execute immediately without a second confirmation turn.
Tool responses are bounded below LiveKit's RPC payload limit.
Friday blocks credential paths and server-side requests to local or private network addresses.

### Adding A Tool

Create a new module in `local_service/src/tools/`:

```python
from .base import ToolParam, ToolResult, tool


@tool(
    name="my_tool",
    description="Precise description the LLM sees, including when to call it.",
    parameters=[
        ToolParam(
            name="arg1",
            type="string",
            description="Argument description.",
            required=True,
        ),
    ],
    permission="read_only",
)
async def my_tool(arg1: str) -> ToolResult:
    return ToolResult(spoken="Done.", data={"arg1": arg1})
```

Restart the menu bar app or `local_service` after adding a tool. The cloud agent
only fetches the manifest once when the room session starts.

## Profile And Memory

Profile data is stored locally at:

```text
~/Library/Application Support/Friday/profile.json
```

If the file does not exist, `local_service` creates it from
`local_service/profile.seed.json` if present, otherwise it creates:

```json
{
  "version": 1,
  "facts": {},
  "updated_at": null
}
```

Profile writes emit a `profile_updated` event over `WS /events`.
Swift forwards that event to the agent with the `profile_updated` RPC, and the agent rebuilds its instructions with the current facts.
Legacy saved location keys are excluded because live Core Location is authoritative.

## Configuration

### Agent Configuration

For local development, `agent/src/agent.py` loads the top-level `.env.local`.
For deployed LiveKit agents, secrets are supplied through the LiveKit deployment
environment, commonly via `agent/secrets.env`.

Required for the cloud agent:

| Variable | Purpose |
| --- | --- |
| `LIVEKIT_URL` | LiveKit Cloud URL. |
| `LIVEKIT_API_KEY` | LiveKit API key. |
| `LIVEKIT_API_SECRET` | LiveKit API secret. |
| `ANTHROPIC_API_KEY` | Anthropic LLM access. |
| `OPENAI_API_KEY` | OpenAI LLM access for the default fast and complex models. |
| `DEEPGRAM_API_KEY` | Deepgram STT/TTS access. |

Optional:

| Variable | Purpose |
| --- | --- |
| `FRIDAY_TEST_MODE=1` | In dev mode, greets on connect and leaves mic input enabled for smoke testing. |

### Local Service Configuration

`local_service/src/config.py` loads `local_service/.env` by default and can also
read process environment variables.

Required:

| Variable | Purpose |
| --- | --- |
| `LIVEKIT_URL` | LiveKit Cloud URL returned to Swift. |
| `LIVEKIT_API_KEY` | Used to mint AccessTokens. |
| `LIVEKIT_API_SECRET` | Used to sign AccessTokens. |

Optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPOTIFY_CLIENT_ID` | none | Enables the Spotify provider using PKCE authentication. |
| `SPOTIFY_REDIRECT_URI` | `http://127.0.0.1:43821/spotify/callback` | Exact loopback callback registered in the Spotify dashboard. |
| `FRIDAY_AGENT_NAME` | `friday-agent` | Agent dispatch name embedded in room tokens. |
| `FRIDAY_ROOM_PREFIX` | `friday` | Prefix for generated LiveKit room names. |
| `FRIDAY_TOKEN_TTL_SECONDS` | `600` | Token lifetime. |
| `FRIDAY_WAKE_MODEL` | `models/hey_friday.onnx` | A pretrained openWakeWord model name or a custom `.onnx` or `.tflite` path. |
| `FRIDAY_WAKE_THRESHOLD` | `0.5` | Minimum openWakeWord confidence required to wake. |
| `FRIDAY_WAKE_DEBOUNCE_MS` | `1500` | Minimum time between wake events. |
| `FRIDAY_ALLOWED_PATHS` | empty | Extra allowed file roots separated by the platform path separator. |
| `FRIDAY_CODE_AGENT` | auto-detected | Executable path or command name for the read-only coding specialist. |
| `FRIDAY_CAPABILITY_PROVIDERS_JSON` | `[]` | JSON array of trusted external capability providers. |

Spotify access and refresh tokens are stored in macOS Keychain under the service name `com.friday.spotify.oauth`.
The Spotify Client Secret is not used.
The provider requests playback-state, playback-control, private-playlist, and collaborative-playlist scopes.

Do not commit real `.env`, `.env.local`, or `secrets.env` files.

## Setup

Prerequisites:

- macOS 14 or newer.
- Xcode with macOS SDK support.
- XcodeGen.
- Python 3.11 or newer.
- `uv`.
- LiveKit CLI (`lk`) for agent deployment.
- A LiveKit Cloud project and API key/secret.
- Deepgram and OpenAI API keys for the default cloud agent configuration.
- An Anthropic API key only when `FRIDAY_COMPLEX_MODEL` selects a Claude model.
- A microphone and macOS microphone permission for the menu bar app.
- macOS location permission for location-aware requests.
- macOS Accessibility permission for working context and generic UI controls.
- Optional macOS Calendar permission for upcoming-event context.

Install the local service:

```bash
cd local_service
uv sync
```

Create `local_service/.env` with at least:

```bash
LIVEKIT_URL=...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
```

Install the cloud agent dependencies:

```bash
cd agent
uv sync
```

Create top-level `.env.local` for local agent development:

```bash
LIVEKIT_URL=...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
ANTHROPIC_API_KEY=...
DEEPGRAM_API_KEY=...
```

Generate the Xcode project:

```bash
cd mac
xcodegen
```

Before running the Mac app, update the hard-coded paths in
`mac/Friday/LocalServiceProcess.swift` if needed.

## Running Locally

Run the local service by itself:

```bash
cd local_service
uv run python -m src.main
```

Check health after it writes the port file:

```bash
curl "http://127.0.0.1:$(cat ~/Library/Application\ Support/Friday/port)/health"
```

Run the cloud agent as a development worker:

```bash
cd agent
uv run python src/agent.py dev
```

Run the agent in console mode:

```bash
cd agent
uv run python src/agent.py console
```

Smoke-test mode:

```bash
cd agent
FRIDAY_TEST_MODE=1 uv run python src/agent.py dev
```

Build and run the Mac app:

```bash
cd mac
xcodebuild -project Friday.xcodeproj -scheme Friday -configuration Release -derivedDataPath build build
open build/Build/Products/Release/Friday.app
```

For day-to-day Swift work, open `mac/Friday.xcodeproj` in Xcode and run the
app from there.

## Deploying The Agent

Deploy from `agent/`:

```bash
cd agent
lk agent deploy
```

Check status and logs:

```bash
lk agent status
lk agent logs
```

The deployment uses `agent/livekit.toml` and `agent/Dockerfile`. The Dockerfile
installs dependencies with `uv sync --locked`, runs the agent download step, and
starts the worker with:

```bash
uv run src/agent.py start
```

After deployment, new room sessions use the new agent build. Existing sessions
must reconnect to pick it up.

## Restart Rules While Developing

- Local tool or `local_service` changes: quit and relaunch the menu bar app, or
  restart `uv run python -m src.main` if debugging standalone.
- Cloud agent changes: run a dev worker with `uv run python src/agent.py dev`,
  or deploy with `lk agent deploy`.
- Mac app changes: rebuild/re-run from Xcode or `xcodebuild`.
- New tools require a fresh room session because the agent fetches the manifest
  only once at startup.

## Logs And Local Files

| Path | Meaning |
| --- | --- |
| `~/Library/Application Support/Friday/port` | Current local service port file. |
| `~/Library/Application Support/Friday/profile.json` | Local profile facts. |
| `~/Library/Application Support/Friday/context.sqlite3` | Durable personal reference memories. |
| `~/Library/Logs/Friday/local_service.log` | Rotating local service log. |
| `~/Library/Logs/Friday/latency.jsonl` | Privacy-filtered per-turn HUD and latency timing events without transcript text. |
| `mac/build/` | Xcode build output when using the documented build command. |
| `local_service/models/` | Local openWakeWord model files. |

The local service logger writes rotating logs with 2 MB files and 3 backups.

## Security Model

- `local_service` binds to `127.0.0.1` only.
- It has no HTTP authentication and trusts local callers.
- Do not bind it to a non-loopback interface.
- LiveKit tokens are short-lived; default TTL is 600 seconds.
- Local profile data is stored in the user's Application Support directory.
- Requested primitives execute immediately without confirmation prompts.
- File access is restricted to the project and ordinary Desktop, Documents, and Downloads roots by default.
- Public HTTP reads block local, private, link-local, and reserved network addresses.

## Troubleshooting

### The Mac App Cannot Start `local_service`

Check `mac/Friday/LocalServiceProcess.swift`. The `workingDir` and `pythonPath`
constants must point at the actual checkout and `local_service/.venv/bin/python`.
Then run:

```bash
cd local_service
uv sync
```

### Wake Detection Fails On Startup

If `FRIDAY_WAKE_MODEL` points to a custom model, verify that the `.onnx` or `.tflite` file exists.
The bundled Friday model is `local_service/models/hey_friday.onnx`.

### The Orange Microphone Indicator Stays On

The Swift LiveKit capture owns the microphone stream while Friday is running.
Quit the menu bar app cleanly so LiveKit releases that capture and `LocalServiceProcess.stop()` terminates the Python helper.
On next launch, the app also runs `pkill -9 -f '-m src\.main'` to clean up orphaned service processes.

### The Agent Does Not See A New Tool

Restart the room session. The agent fetches `__list__` once when it joins the
room; it does not hot-reload the manifest during a session.

### The Agent Is Not In The Room Yet

`LiveKitController.activateTurnWithRetry()` retries once for cold starts. If it
still fails, inspect LiveKit agent status/logs:

```bash
cd agent
lk agent status
lk agent logs
```

### Local Service Health Check

```bash
curl "http://127.0.0.1:$(cat ~/Library/Application\ Support/Friday/port)/health"
```

Expected shape:

```json
{
  "ok": true,
  "wakePaused": false
}
```

## Current Limitations

- Tool manifests are loaded once per session.
- Action and capability catalogs are loaded once per session.
- The Swift app path to `local_service` is hard-coded.
- `local_service` has no local auth because it is intended to be loopback-only.
- Wake-word quality depends on the configured openWakeWord model and local microphone conditions.
- macOS Accessibility permission must be granted by the user before `inspect_ui` and `interact_ui` can work.
- Working document and browser URL context depends on what each app exposes through macOS Accessibility.
- Calendar context is read-only and empty until the user grants full Calendar access.
- The POC reference resolver supports explicit aliases plus current file, page, app, and project references, not a complete personal knowledge graph.
- Some apps expose incomplete Accessibility trees, so AppleScript or a direct process command can be a fallback.
- Weather and other unsupported services use reusable web and app primitives until a dedicated provider is justified.

## Development Notes

- Add a provider adapter and declarative action manifests for a new integration.
- Add a capability only when the work requires reasoning or several steps.
- Add a primitive only when Friday needs a genuinely new low-level power.
- Keep direct network access to the Mac behind the existing Swift RPC boundary.
- Keep tool descriptions precise. The LLM sees those descriptions and uses them
  to decide when to call the tool.
- Keep `ToolResult.spoken` concise because it is usually spoken aloud.
- Include structured details in `ToolResult.data` so the HUD can render useful results without parsing spoken text.
- Restart the right component for the kind of change you made; most iteration
  does not require redeploying the cloud agent.
