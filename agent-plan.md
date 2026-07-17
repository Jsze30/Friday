# Agent Plan — Making Friday Truly Autonomous

Goal: turn Friday from a Siri-style intent matcher into an **autonomous voice agent** that can reason across multiple steps, control the Mac through general-purpose primitives, and feel fast in live conversation.

This document captures what we're changing and why. It is the source of truth — when scope shifts, update this file rather than relying on chat history.

---

## Guiding principles

1. **Primitives over tasks.** A handful of broad tools (shell, AppleScript, browser, filesystem) lets the LLM compose any workflow. Avoid one-tool-per-feature.
2. **Latency is the product.** Every design choice is weighed against time-to-first-audio. Sub-second perceived latency > marginally smarter response.
3. **Speak while thinking.** Filler audio during tool execution masks real latency. The user should never hear silence longer than ~600 ms.
4. **Trust requires gates.** Powerful tools need confirmation, logging, and a kill switch. `local_service` already trusts localhost blindly — we cannot ship `shell_exec` without permission flow.
5. **Profile as world model.** The agent's "common sense" about *this user's machine* lives in the profile and gets injected into the system prompt — not into tool calls.

---

## Architecture changes

No structural changes to the three-process model (cloud agent ↔ Mac ↔ local_service). The existing `tool` decorator + dynamic manifest + RPC proxy in `agent/src/agent.py:117-176` is the right shape — we extend it, we don't replace it.

### What stays
- LiveKit Cloud agent dispatch + RPC pattern.
- `local_service` tool registry with auto-discovery via `tools/__init__.py:load_all()`.
- The cloud agent fetching the manifest once at session start.
- `ToolResult` envelope with `spoken` / `data` / `needs_confirmation` / `confirmation_id`.
- **All current tools (`get_time`, `remember`, etc.).** Specific tools and primitives coexist. Keep a specific tool whenever (a) there's a clean API or (b) the result is structured data the LLM should not have to parse from text. Reach for a primitive when the surface is open-ended (launching apps, driving GUI apps, arbitrary shell). The LLM will pick correctly as long as primitive descriptions explicitly say *"use only when no more specific tool fits."*

### What changes
- Tool surface (new primitives).
- Schema (add `array` type, output truncation convention).
- Permission flow (wire up the existing `needs_confirmation` field end-to-end).
- System prompt (license planning, mandate filler speech, render richer profile).
- Model + caching strategy (keep Haiku live, add `deep_think` escalation, cache prefix).
- Profile schema (projects, preferences, shortcuts).

---

## Phase 1 — Schema groundwork

Small, safe changes that unblock everything else.

### 1.0 Reorganize the tools folder

Split `local_service/src/tools/` into two subpackages so the distinction between primitive and specific tools is visible in the codebase:

```
local_service/src/tools/
  base.py
  __init__.py            # auto-discovery, now recursive
  primitives/
    __init__.py
    mac_action.py        # existing — candidate to be superseded by Phase 2 primitives
    (open_app.py, applescript_run.py, etc. land here in Phase 2)
  specific/
    __init__.py
    get_time.py
    get_weather.py
    google_calendar.py
    remember.py
```

**This is purely a code-organization change — the LLM never sees file paths, only tool names from the manifest.** No effect on the agent or RPC layer.

Mechanical steps:
- Create `primitives/__init__.py` and `specific/__init__.py` (empty files — they just make the dirs real packages).
- `git mv` the existing tool files into the appropriate subfolder.
- Update relative imports in the moved files:
  - `from .base import ...` → `from ..base import ...`
  - `from .. import profile` → `from ... import profile`
  - `from ..google_auth import ...` → `from ...google_auth import ...`
- Update `tools/__init__.py:load_all()` to walk subpackages: replace `pkgutil.iter_modules(__path__)` with `pkgutil.walk_packages(__path__, prefix=__name__ + ".")`. Skip modules whose final name segment starts with `_` or equals `base`.
- Verify: `REGISTRY` still has the same tool names after restart.

Caveat: tool names live in a single flat `REGISTRY` keyed by `name` (`base.py:75`). Folders do not namespace. If `primitives/foo.py` and `specific/foo.py` both register `name="foo"`, registration fails at startup — keep names unique across subfolders.

### 1.1 Add `array` parameter type
- `local_service/src/tools/base.py` — extend `ParamType` literal to include `"array"`.
- `agent/src/agent.py:33` — extend `PARAM_TYPE_MAP` to map `"array"` → `list[str]` (we'll only use string arrays for now: `key_combo(["cmd","shift","p"])`).
- Manifest needs an optional `items` field for typed arrays in the future; defer until needed.

### 1.2 Output truncation convention
- Tools that can return large output (`shell_exec`, `read_file`) truncate stdout/stderr to ~4 KB and set `data.truncated: true` with `data.full_size`.
- Add a small `truncate_for_llm(text, limit=4096)` helper in `local_service/src/tools/_util.py`.

### 1.3 Per-tool timeout in the agent proxy
- `agent/src/agent.py:143` — wrap `call_tool` in `asyncio.wait_for` with a default of 15 s. A hung RPC currently stalls the entire turn.

---

## Phase 2 — Primitive tool kernel

Build these in `local_service/src/tools/primitives/`. Each is a single file using the existing `@tool` decorator.

| Tool | Permission | Purpose |
|---|---|---|
| `open_app` | low_risk_write | `open -a <name>` — launch any Mac app |
| `quit_app` | low_risk_write | Quit a running app via AppleScript (`tell app "X" to quit`). Falls back to `osascript`-driven `System Events` if the app ignores the quit event. |
| `open_path` | low_risk_write | `open <path>` — open file / folder in default handler |
| `browser_open` | low_risk_write | `open <url>` — open URL in default browser |
| `browser_close_tab` | low_risk_write | Close a browser tab. Default: frontmost tab of the default browser. Optional `match` arg closes the first tab whose URL or title contains the substring. Implemented per-browser via AppleScript (Safari, Chrome, Arc — each has slightly different scripting dictionaries). |
| `applescript_run` | low_risk_write | Run an AppleScript string, return its output |
| `read_file` | read_only | Read a file (with truncation) |
| `list_dir` | read_only | Directory listing with file types |
| `web_search` | read_only | Hit a search API, return top results — for grounding before action |
| `shell_exec` | sensitive | Run an arbitrary zsh command — gated by confirmation |
| `keystroke` (later) | sensitive | AppleScript `System Events` keystrokes — fallback for GUI-only apps |

### Why this set?
With `applescript_run` + `open_app` + `shell_exec`, the running examples are trivial:
- "Open Friday in VS Code" → `shell_exec("code /Users/jas/Documents/Coding/friday")`
- "Play lo-fi" → `applescript_run('tell application "Spotify" to play track "spotify:playlist:..."')` (or search-then-play sequence)
- "Open the LiveKit dashboard" → `browser_open("https://cloud.livekit.io")`

The LLM composes; we do not write a `play_lofi` tool.

### Build order in this phase
1. `open_app`, `quit_app`, `open_path`, `browser_open` — trivial wrappers, low risk, immediate value.
2. `applescript_run` — unlocks Spotify / Music / Messages / Finder without GUI fragility.
3. `browser_close_tab` — depends on `applescript_run` patterns; per-browser dispatch lives here.
4. `read_file`, `list_dir` — let the agent reason about the filesystem.
5. `web_search` — only after we pick a provider (Brave / Perplexity / Tavily).
6. `shell_exec` — last, after Phase 4's permission gates exist.

### Caveats for quit / close
- `quit_app` should accept a `force: bool = false` arg. Default sends a graceful AppleScript `quit`; `force=true` upgrades to `kill` via `shell_exec` (so it inherits the `sensitive` permission gate when used).
- `browser_close_tab` needs to know which browser is frontmost. Read `preferences.browser` from the profile when set; otherwise detect the frontmost browser via `System Events`. Closing tabs in a browser the user isn't focused on is surprising — avoid it.
- Both tools should refuse to act on a small denylist of apps (e.g. `Friday` itself, `Finder`, `loginwindow`) to prevent the agent from quitting its own host.

---

## Phase 3 — System prompt + profile rewrite

### 3.1 New `BASE_INSTRUCTIONS` (in `agent/src/agent.py:24`)
The new prompt does three things:
- Licenses multi-step planning ("you can chain tools to accomplish a goal").
- Mandates pre-tool filler ("before any tool that takes more than a moment, briefly acknowledge so the user hears you working").
- Encourages tool selection by specificity ("prefer `applescript_run` or `open_app` over `shell_exec` when both work").
- Reinforces the existing "speech, not text" + "concise" guidance.

### 3.2 Expanded profile schema
```json
{
  "facts": { "name": "Jas", "timezone": "America/Los_Angeles" },
  "projects": { "friday": "/Users/jas/Documents/Coding/friday" },
  "preferences": {
    "editor": "code",
    "music_app": "Spotify",
    "browser": "Arc",
    "shell": "zsh"
  },
  "shortcuts": {
    "dev mode": "open Friday in VS Code and start the local service"
  }
}
```

`render_instructions` in `agent.py:41` is rewritten to render projects, preferences, and shortcuts as a structured `<profile>` block. This means "open Friday" resolves to a path **without a tool call** — pure prompt-time substitution.

### 3.3 Profile editing tools
- `set_preference(key, value)` — low_risk_write, updates the preferences map.
- `add_project(name, path)` — low_risk_write, registers a project alias.
- `add_shortcut(phrase, expansion)` — low_risk_write, custom multi-step phrases.
- `forget(key)` — low_risk_write, removes any of the above.

`profile_updated` RPC already exists — these tools fire it after writing, so the system prompt updates mid-session.

---

## Phase 4 — Permission gates

Wire up the `needs_confirmation` / `confirmation_id` fields that already exist on `ToolResult` but are inert today.

### 4.1 Flow
1. `shell_exec` (or any `sensitive` tool) returns `ToolResult(needs_confirmation=True, confirmation_id=<uuid>, data={"command": "...", "summary": "..."})`.
2. `local_service` stores `(uuid, callable, args)` in an in-memory `pending_confirmations` map with a 60 s TTL.
3. The envelope flows through Mac → agent. The agent sees `needsConfirmation: true` and verbalizes: *"About to run `code ~/Documents/Coding/friday` — okay?"*
4. User says yes/no. The agent calls a new `confirm_pending(id, approve)` tool.
5. `local_service` looks up the id, runs the queued action (or discards), returns the real result.

### 4.2 Mac UI surface
- Menu bar item shows current pending confirmations with command preview.
- Hardware kill switch: a global hotkey that calls `cancel_turn` RPC + disables sensitive tools for the session.

### 4.3 Session allowlist
- `confirm_pending(id, approve, remember=true)` adds the command pattern to a per-session allowlist.
- Subsequent matching commands skip the prompt for the duration of the room session only.
- Never persisted to disk — every new session starts fresh.

### 4.4 Tool log
- Every tool call (name, args, result, latency) appended to a ring buffer in `local_service`.
- Mac menu bar exposes "Recent activity" — last 50 calls. Trust requires visibility.

---

## Phase 5 — Latency optimizations

### 5.1 Prompt caching
- The Anthropic LiveKit plugin supports `cache_control` markers. Mark `BASE_INSTRUCTIONS` + profile block + tool manifest as cacheable — they don't change mid-session.
- Verify via the Anthropic API response's `cache_read_input_tokens` once integrated.

### 5.2 Pre-tool filler
- Encoded in `BASE_INSTRUCTIONS` (Phase 3.1).
- We will measure whether Haiku reliably emits filler. If not, fall back to a deterministic filler injected by the agent layer when a tool call is detected before TTS finishes the previous sentence.

### 5.3 Parallel tool execution
- Verify `function_tool` runs concurrent `tool_use` blocks in parallel. If it serializes, patch `call_tool` to use `asyncio.gather`.
- Make sure `local_service` `/tools/execute` doesn't hold a global lock.

### 5.4 `deep_think` escalation tool
- A `read_only` tool implemented in `local_service` that calls Sonnet 4.6 with extended thinking via the Anthropic SDK directly.
- Signature: `deep_think(question: str, context: str) -> ToolResult`.
- Returns a structured plan as `data.plan` plus a one-line `spoken` summary.
- Haiku speaks filler ("hmm, let me think about that") while it runs.
- Use sparingly — only when Haiku visibly struggles. We will not put Opus in the live path.

---

## Phase 6 — Hardening

Things we'll discover we need; capture now so they don't get forgotten.

- **Error-as-data convention.** When `shell_exec` exits non-zero, return `ok=True` with `data.exit_code` and `data.stderr`. Let the LLM decide whether to retry or surface. Reserve `ok=False` for "tool itself broke."
- **Confirmation TTL cleanup.** Background task in `local_service` that evicts expired pending confirmations.
- **AppleScript safety.** `applescript_run` should reject scripts that target obviously sensitive surfaces (Keychain, System Events when no app is targeted, etc.) — or upgrade them to `sensitive` tier.
- **Filesystem allowlist for `read_file`.** Default allow: `~/Documents`, `~/Desktop`, `~/Downloads`, the project itself. Reject anything outside without elevation.
- **Rate limit `shell_exec`.** Max N commands per minute per session; prevents runaway loops.

---

## Build order (recommended)

1. **Phase 1** — schema groundwork (1 sitting).
2. **Phase 2.1–2.2** — `open_app`, `open_path`, `browser_open`, `applescript_run` (1 sitting). Test live.
3. **Phase 3** — system prompt + profile rewrite (1 sitting). At this point the agent feels meaningfully more capable.
4. **Phase 5.1, 5.3** — prompt caching + parallel tools. Quick latency wins.
5. **Phase 4** — full permission flow. **Must complete before Phase 2.5 ships.**
6. **Phase 2.3–2.5** — `read_file`, `list_dir`, `web_search`, finally `shell_exec`.
7. **Phase 5.4** — `deep_think`, only after measuring Haiku gaps.
8. **Phase 6** — hardening, ongoing.

---

## What we are explicitly NOT doing (yet)

- **Computer use / vision-based GUI control.** Slower, costlier, less reliable than AppleScript + CLI for the apps we care about. Revisit only if a target app has no scriptable / CLI surface.
- **Persistent cross-session allowlists.** Too easy to footgun. Session-scoped only.
- **Putting Opus in the live conversational loop.** Latency unacceptable for voice. `deep_think` is the escape hatch.
- **A planner/executor split agent.** Possibly later, but Haiku + extended thinking via `deep_think` likely covers it.
- **Authentication on `local_service`.** Still localhost-only. If we ever bind off-loopback, we'll need a token scheme — but that's not on the roadmap.

---

## Open questions

- Which web search provider? (Brave is cheapest, Tavily is LLM-shaped, Perplexity is highest quality.)
- AppleScript: support raw scripts, or a curated set of named recipes (`spotify_play`, `messages_send`)? Raw is more powerful, recipes are safer.
- Confirmation UX: voice-only, menu bar, or both? Voice is faster mid-conversation; menu bar is better when ambient.
- Tool log retention: in-memory ring (current proposal) or a SQLite file in Application Support for cross-session debugging?
