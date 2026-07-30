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
- Enables the microphone only after local wake detection.
- Runs a spoken assistant powered by Deepgram STT/TTS, routed OpenAI models, and Silero VAD.
- Gives the assistant one high-level capability runner backed by ranked providers.
- Gives common app and audio actions one direct native `control_mac` path.
- Keeps the reusable primitive kernel behind two fallback discovery tools instead of showing every primitive to the model.
- Runs slow capabilities in cancellable background tasks so the live voice loop can keep responding.
- Discovers application controls at runtime through macOS Accessibility instead of requiring a custom Spotify, VS Code, Arc, or Finder integration.
- Requires explicit confirmation before destructive file changes, processes, AppleScript, or UI interactions execute.
- Supplies a live human-readable Core Location place and a fresh local clock as ambient context.
- Stores stable profile facts locally and injects them into the agent prompt.
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
|   |-- src/wake.py        # Vosk wake-word detector over sounddevice
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

Generated and local-only directories such as `mac/build/`, Python virtualenvs,
Vosk models, and env files are not part of the source architecture.

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
  `listening`, `thinking`, `speaking`, and `error`.
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
- Start the Vosk wake-word detector.
- Mint LiveKit tokens for the Mac participant.
- Serve profile data from local disk.
- Register and execute local tools.
- Rank, execute, verify, retry, and cancel high-level capability providers.
- Publish wake/profile events over a WebSocket consumed by Swift.

Local service API:

| Route | Purpose |
| --- | --- |
| `GET /health` | Returns `{ ok, wakePaused }`. |
| `POST /token` | Mints a LiveKit room token and dispatches the configured agent. |
| `POST /wake/pause` | Pauses wake-word processing while LiveKit owns the mic. |
| `POST /wake/resume` | Resumes wake-word processing after the turn ends. |
| `GET /profile` | Returns the local profile JSON. |
| `PUT /profile` | Replaces/saves the local profile and emits `profile_updated`. |
| `POST /tools/execute` | Executes a tool by name with JSON arguments. |
| `POST /capabilities/execute` | Lists, starts, polls, or cancels a capability task. |
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
It exposes `run_capability` directly and places dynamically discovered primitives behind LiveKit's fixed `tool_search` and `call_tool` interface.

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
      | LiveKit RPC: capability_call / tool_call / get_context
      |
Swift menu bar app
      |
      | HTTP localhost
      v
local_service tools/profile/token API
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
3. `local_service` picks a free localhost port, writes it to
   `~/Library/Application Support/Friday/port`, starts Vosk, and starts FastAPI.
4. Swift polls the port file, creates a `LocalServiceClient`, and calls
   `GET /health`.
5. Swift calls `POST /token`.
6. `local_service` returns a LiveKit AccessToken with:
   - a fresh room name like `friday-<timestamp>-<hex>`;
   - a Mac participant identity like `mac-<hex>`;
   - publish/subscribe/data permissions;
   - room config dispatching the `friday-agent` agent.
7. Swift connects to LiveKit with the microphone disabled.
8. Swift registers RPC methods:
   - `return_to_sleep`
   - `set_assistant_state`
   - `get_context`
   - `capability_call`
   - `tool_call`
9. LiveKit dispatches the cloud agent into the room.
10. The agent waits for the Mac participant, fetches profile and location together, fetches the combined primitive manifest, wraps those primitives in a proxy toolset, and starts the session with audio input disabled.
11. Swift opens `WS /events` to receive wake/profile events from
    `local_service`.

## Turn Lifecycle

1. Vosk detects the wake phrase in `local_service/src/wake.py`.
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
4. Swift pauses local wake processing with `POST /wake/pause`.
5. Swift enables the LiveKit microphone and calls the agent RPC
   `activate_turn`.
6. The agent enables audio input and listens.
7. Agent state changes are sent back to Swift with `set_assistant_state`.
8. For broad read-only work, the LLM calls `run_capability`.
9. The agent starts a local capability task through `capability_call`.
10. Fast tasks return immediately, while slow tasks release the voice loop and continue in the background.
11. The agent polls small status messages and cancels local work if the user cancels the LiveKit task.
12. For direct primitives, the LLM uses `tool_search` and `call_tool`, then the cloud agent performs a `tool_call` RPC to Swift.
13. Swift forwards capability and primitive calls to their corresponding localhost APIs.
14. `local_service` returns structured results with provider traces or primitive confirmation fields.
15. The agent speaks the result or incorporates the tool result into its answer.
16. After speaking, the agent enters a 5 second follow-up window.
17. If no follow-up input arrives, the agent disables audio and calls
    `return_to_sleep`.
18. Swift disables the microphone, resumes local wake processing, and sets the
    app state to `sleeping`.

## Capability System

The capability layer is the normal path for multi-step work.
The model calls one stable tool instead of selecting from every low-level action.

Common Mac system actions use the separate `control_mac` tool because they should complete immediately instead of becoming background tasks.
It routes directly to native Swift primitives for apps and Core Audio.
The supported actions are `list_apps`, `open_app`, `open_url`, `quit_app`, `get_volume`, `set_volume`, and `mute_audio`.
`open_url` opens HTTP or HTTPS addresses through native macOS APIs and defaults to Arc.

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
The operations are `list`, `start`, `status`, and `cancel`.
Read-only work can run in the background, and LiveKit exposes running-task and cancellation controls automatically.

Additional API, MCP, or specialist-agent providers can use the same contract without adding another model-facing tool.
Set `FRIDAY_CAPABILITY_PROVIDERS_JSON` to a JSON array of configured command providers.
Each command receives one JSON request on standard input and must return `{"summary":"...","data":{...}}` on standard output.
For example:

```json
[
  {
    "id": "browser-agent",
    "name": "browser specialist",
    "capabilities": ["browser"],
    "command": ["/absolute/path/to/browser-agent"],
    "priority": 90,
    "timeout_seconds": 120
  }
]
```

Configured command providers are trusted local extensions and are currently limited to the read-only capability path.
The built-in Spotify provider uses a locally enforced `low_risk_write` policy for playback controls.
Sensitive writes continue through confirmed primitives.

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
  "needsConfirmation": false,
  "confirmationId": null,
  "error": null
}
```

Current primitive kernel:

| Tool | Permission | What it does |
| --- | --- | --- |
| `inspect_path` | `read_only` | Reads an allowed text file or lists Desktop, Documents, Downloads, or the project. |
| `search_files` | `read_only` | Recursively searches file and folder names below an allowed root. |
| `create_directory` | `low_risk_write` | Creates a folder inside an allowed root. |
| `write_file` | `sensitive` | Atomically replaces an allowed text file after confirmation. |
| `move_path` | `sensitive` | Moves or renames a file or folder after confirmation. |
| `trash_path` | `sensitive` | Moves an item to the recoverable macOS Trash after confirmation. |
| `run_process` | `sensitive` | Runs one executable directly without a shell after confirmation. |
| `run_applescript` | `sensitive` | Controls scriptable Mac apps after confirmation. |
| `web_search` | `read_only` | Searches the public web and returns structured results. |
| `fetch_url` | `read_only` | Fetches a public HTTP or HTTPS URL while blocking local and private addresses. |
| `list_apps` | `read_only` | Lists installed or running Mac applications. |
| `open_app` | `low_risk_write` | Launches or activates any installed Mac application. |
| `open_url` | `low_risk_write` | Opens an HTTP or HTTPS URL in Arc or another installed browser. |
| `quit_app` | `sensitive` | Gracefully asks a running application to quit after confirmation. |
| `get_volume` | `read_only` | Reads native Core Audio output volume and mute state. |
| `set_volume` | `low_risk_write` | Sets native Core Audio output volume from 0 to 100. |
| `mute_audio` | `low_risk_write` | Mutes or unmutes the default Core Audio output device. |
| `inspect_ui` | `read_only` | Discovers the accessible controls in any running Mac application. |
| `interact_ui` | `sensitive` | Performs a discovered Accessibility action after confirmation. |
| `confirm_action` | `low_risk_write` | Approves or rejects one pending sensitive primitive. |

Sensitive actions are staged for 60 seconds and do not execute until the user explicitly approves the exact pending action.
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

Spotify access and refresh tokens are stored in macOS Keychain under the service name `com.friday.spotify.oauth`.
The Spotify Client Secret is not used.
The provider requests playback-state, playback-control, private-playlist, and collaborative-playlist scopes.
| `FRIDAY_AGENT_NAME` | `friday-agent` | Agent dispatch name embedded in room tokens. |
| `FRIDAY_ROOM_PREFIX` | `friday` | Prefix for generated LiveKit room names. |
| `FRIDAY_TOKEN_TTL_SECONDS` | `600` | Token lifetime. |
| `FRIDAY_VOSK_MODEL_PATH` | `models/vosk-model-small-en-us-0.15` | Wake model path, relative to `local_service/` unless absolute. |
| `FRIDAY_WAKE_PHRASE` | `friday` | Wake phrase recognized by Vosk grammar. |
| `FRIDAY_WAKE_DEBOUNCE_MS` | `1500` | Minimum time between wake events. |
| `FRIDAY_ALLOWED_PATHS` | empty | Extra allowed file roots separated by the platform path separator. |
| `FRIDAY_CODE_AGENT` | auto-detected | Executable path or command name for the read-only coding specialist. |
| `FRIDAY_CAPABILITY_PROVIDERS_JSON` | `[]` | JSON array of trusted external capability providers. |

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
- Deepgram and Anthropic API keys for the cloud agent.
- A microphone and macOS microphone permission for the menu bar app.
- macOS location permission for location-aware requests.

Install the local service:

```bash
cd local_service
uv sync
./scripts/fetch_vosk_model.sh
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
| `~/Library/Logs/Friday/local_service.log` | Rotating local service log. |
| `mac/build/` | Xcode build output when using the documented build command. |
| `local_service/models/` | Downloaded Vosk wake-word model. |

The local service logger writes rotating logs with 2 MB files and 3 backups.

## Security Model

- `local_service` binds to `127.0.0.1` only.
- It has no HTTP authentication and trusts local callers.
- Do not bind it to a non-loopback interface.
- LiveKit tokens are short-lived; default TTL is 600 seconds.
- Local profile data is stored in the user's Application Support directory.
- Sensitive primitives require a short-lived explicit confirmation before execution.
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

Make sure the Vosk model exists:

```bash
cd local_service
./scripts/fetch_vosk_model.sh
```

If you changed `FRIDAY_VOSK_MODEL_PATH`, verify that it resolves to an existing
model directory.

### The Orange Microphone Indicator Stays On

The local wake detector owns a microphone stream while the service is running.
Quit the menu bar app cleanly so `LocalServiceProcess.stop()` can send SIGTERM
and wait for Python to release the stream. On next launch, the app also runs
`pkill -9 -f '-m src\.main'` to clean up orphaned service processes.

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
- The Swift app path to `local_service` is hard-coded.
- `local_service` has no local auth because it is intended to be loopback-only.
- Wake-word quality depends on the configured openWakeWord model and local microphone conditions.
- macOS Accessibility permission must be granted by the user before `inspect_ui` and `interact_ui` can work.
- Some apps expose incomplete Accessibility trees, so AppleScript or a direct process command can be a fallback.
- Weather and other unsupported services use reusable web and app primitives until a dedicated provider is justified.

## Development Notes

- Prefer adding local capabilities as `local_service` tools instead of giving
  the cloud agent direct network access to the Mac.
- Keep tool descriptions precise. The LLM sees those descriptions and uses them
  to decide when to call the tool.
- Keep `ToolResult.spoken` concise because it is usually spoken aloud.
- Include structured details in `ToolResult.data` for debugging and possible
  future UI use.
- Restart the right component for the kind of change you made; most iteration
  does not require redeploying the cloud agent.
