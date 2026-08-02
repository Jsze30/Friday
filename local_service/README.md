# local_service

Friday's local Python helper owns openWakeWord scoring, LiveKit token signing, personal context storage, the tool registry, capability providers, and the local events API.
The Swift menu bar app launches it as a child process.

## Setup

```bash
cd local_service
uv sync
cp .env.example .env
# fill in LIVEKIT_API_KEY / LIVEKIT_API_SECRET
```

## Run

```bash
uv run python -m src.main
```

On startup the helper:

1. Picks a free port on `127.0.0.1`.
2. Writes it to `~/Library/Application Support/Friday/port` so Swift can read it.
3. Loads the configured openWakeWord model and accepts echo-cancelled PCM from Swift.
4. Logs to `~/Library/Logs/Friday/local_service.log`.

## API

- `GET /health` - `{ ok, wakePaused }`
- `POST /token` - `{ url, token, roomName, participantIdentity, agentName }`
- `POST /wake/pause` / `POST /wake/resume`
- `POST /tools/execute` - executes a registered local primitive.
- `POST /capabilities/execute` - lists, starts, polls, or cancels a capability.
- `POST /context/resolve` - resolves working context and saved reference phrases.
- `GET /context/references` - lists saved reference phrases.
- `WS /wake/audio` - receives wake-word audio frames from Swift.
- `WS /events` - emits events such as `wake_detected` and `profile_updated`.

The API is bound to localhost only and has no auth. Anything running as the same user can call it.

## Layout

```
src/
  main.py            uvicorn entrypoint, port file, lifespan
  config.py          pydantic-settings
  tokens.py          LiveKit AccessToken with agent dispatch
  wake.py            openWakeWord scoring for PCM supplied by Swift
  context_store.py   SQLite reference memory and context resolution
  capabilities/      capability broker, providers, and task runtime
  tools/             auto-loaded local primitive registry
  events.py          in-process pub/sub
  routes.py          FastAPI routes + WS
  logging_setup.py   ~/Library/Logs/Friday/
  runtime.py         shared singletons
scripts/
  wake_monitor.py    standalone microphone monitor for model tuning
```
