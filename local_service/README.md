# local_service

Friday's local Python helper. Owns wake-word detection (Vosk), LiveKit token signing, and the local events API. Launched as a child process by the Swift menu bar app.

## Setup

```bash
cd local_service
uv sync
./scripts/fetch_vosk_model.sh
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
3. Starts the Vosk wake detector in a background thread.
4. Logs to `~/Library/Logs/Friday/local_service.log`.

## API

- `GET /health` — `{ ok, wakePaused }`
- `POST /token` — `{ url, token, roomName, participantIdentity, agentName }`
- `POST /wake/pause` / `POST /wake/resume`
- `WS /events` — emits `{ type: "wake_detected", phrase, timestamp, confidence }`

The API is bound to localhost only and has no auth. Anything running as the same user can call it.

## Layout

```
src/
  main.py            uvicorn entrypoint, port file, lifespan
  config.py          pydantic-settings
  tokens.py          LiveKit AccessToken with agent dispatch
  wake.py            Vosk + sounddevice background detector
  events.py          in-process pub/sub
  routes.py          FastAPI routes + WS
  logging_setup.py   ~/Library/Logs/Friday/
  runtime.py         shared singletons
scripts/
  fetch_vosk_model.sh
```
