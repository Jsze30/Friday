# Friday - Vision

Long term direction for the project.
This document describes *what Friday should become* and *why*, grounded in what the code does today.
It is deliberately not an implementation plan - see `agent-plan.md` for phased execution and `tool-plan.md` for the tool layer design.

Three goals drive everything:

1. Activation and conversation should feel seamless and fast.
2. Friday should be able to do far more, without a hand-written function per capability.
3. Friday should have a real UI that shows state and displays useful information.

These read as separate goals but share one root cause: **Friday currently has no channel for anything except audio.**
It cannot show you what it heard, cannot show you a result, and cannot do anything it was not hand-coded for one function at a time.

---

## 1. Seamless activation and speech

What currently reads as "slow" or "flaky" is several distinct problems with a shared root cause.

### 1A. Root cause: the microphone is captured twice

Friday opens the input device twice, over two independent paths, and only one of them is processed.

| Path | Capture | Echo cancellation |
|---|---|---|
| Wake detection | `wake.py:73`, PortAudio via `sounddevice`, in the Python `local_service` | **none** |
| Conversation | LiveKit Swift SDK, WebRTC | yes, built in |

WebRTC ships acoustic echo cancellation as standard equipment.
The wake detector bypasses all of it and reads the raw device.

This single fact causes two separate user-visible bugs.

**Audio from your own speakers triggers the wake word.**
Playing a video, or a call in which someone says the phrase, wakes Friday.
The model is behaving correctly here - it genuinely heard the phrase.
It simply has no way to know the sound came out of the speakers rather than your mouth, because nothing is cancelling the echo on that path.
No amount of threshold tuning or model retraining fixes this, because the input really is the wake phrase.

**The pre-roll buffer needed for 1B cannot be built.**
A buffer in `local_service` holds Python-captured audio, while the audio LiveKit actually transmits is captured separately in Swift.
They are different streams, so one cannot be prepended to the other.

**The fix for both is the same change: move wake detection into the Swift app, fed from the same AEC-processed audio LiveKit already captures.**
Echo cancellation comes for free, the pre-roll buffer becomes trivial because it is the same stream, and the two processes stop contending for the device.
The cost is running the ONNX wake model from Swift rather than Python.

Short of that change there is no cheap fix for arbitrary system audio.
Headphones make the problem disappear, which is a workaround rather than an answer.

### 1B. It clips the front of your sentence

`LiveKitController.swift:44` enables the microphone *after* wake fires, then performs an RPC round trip to the cloud agent before audio is accepted.

| Step | Cost |
|---|---|
| openWakeWord scores the phrase | ~200-400ms after you finish saying it |
| WebSocket `wake_detected` to Mac | ~5ms |
| `setMicrophone(enabled: true)` - WebRTC track publish + renegotiation | ~200-500ms |
| `activate_turn` RPC to cloud agent, then `set_audio_enabled(True)` | ~100-200ms |
| **Total speech lost** | **~0.5-1.1s** |

This is why you instinctively pause after the wake word.

**Target state:** you say "hey friday what's the weather" as one continuous sentence and all of it lands.

This requires two changes.
A rolling pre-roll buffer keeps the last ~2 seconds of audio, and on wake that buffer is prepended to the stream rather than discarded.
This depends on the unified capture path described in 1A.
The microphone track also stays published and locally muted between turns, rather than being torn down and republished on every activation.

### 1C. Wake word accuracy

Measured 2026-07-28 with `scripts/wake_monitor.py`, pretrained `hey_jarvis` model, built-in MacBook Pro microphone.

| Condition | Result |
|---|---|
| Real phrase, 6-8 utterances, varied distance and volume | peaks 0.768 to 0.998, every utterance detected |
| Ambient background, ~90s continuous | peak 0.442, typically under 0.3 |
| Phonetic neighbours - "hey jabby", "hey jarvy", "hey jav" | **~0.99, indistinguishable from the real phrase** |

Detection reliability and ambient noise rejection are both good.
The problem is phonetic neighbours, and it is not a threshold problem.
No threshold separates a true accept at 0.99 from a false accept at 0.99.

This is inherent to openWakeWord's pretrained models.
They are trained on synthetic TTS data without much hard-negative mining, so they are phonetically loose.
Anything with the right stress pattern and a similar consonant onset lands in the same basin.

Two fixes, which compose:

**A custom verifier model**, which openWakeWord ships built in (`custom_verifier_model.py:114`, `train_custom_verifier`).
It trains a logistic regression over the wake model's embedding features using clips of one specific speaker.
Positives are you saying the real phrase; negatives are you saying the confusable phrases.
It only runs on frames where the base model already scored above `custom_verifier_threshold`, so it costs nothing while idle.
Two properties make it the right fit: it is trained on your voice specifically, and you choose exactly which confusables to reject.

**A custom-trained wake model for "hey friday"**, with the same confusables supplied as hard negatives during training.
This replaces the pretrained base model rather than filtering it, and also fixes the fact that the product is named Friday but answers to "hey jarvis".

Note that switching the phrase alone does not fix this.
"hey friday" has its own phonetic neighbours - "hey frida", "hey fry day".
The fix is hard negatives during training, not the choice of words.

### 1D. Dead air after you stop talking

| Step | Cost | Notes |
|---|---|---|
| Deepgram Flux endpointing | **1500ms** | `agent.py:65`, `eot_timeout_ms` - hard floor, fires every turn |
| Haiku 4.5 first token | ~400-700ms | |
| Tool round trip: agent -> Mac RPC -> localhost HTTP -> back | ~300-600ms | tool turns only |
| **Second** LLM call to phrase the tool result | ~400-700ms | tool turns only |
| Aura-2 TTS first audio | ~200-400ms | |
| LiveKit transport | ~50-100ms | |
| **Total, no tool** | **~2.2-2.7s** | |
| **Total, with tool** | **~3.5-4.5s** | |

The single largest line item is the 1500ms endpointing timeout.
That is roughly 40% of the dead air on a simple turn, and it is one configuration value.
The second largest is that any tool call costs *two* LLM round trips rather than one.

### Definition of done

- Continuous speech across the wake word, nothing clipped.
- Under 1 second from when you stop talking to the first syllable of the reply.
- Barge-in: talking over Friday interrupts it.
- No perceptible cold start - the session is warm and connected, not established on demand.
- Audio played through the machine's own speakers never triggers the wake word.
- Phonetic near-misses of the wake phrase do not trigger it.
- Friday answers to its own name.

---

## 2. Many more capabilities

The constraint is not that there are only 7 tools today.
It is that **the architecture costs one hand-written Python function per capability.**

`run_mac_action` supports exactly three verbs - `open_app`, `media_play_pause`, `set_volume` - because someone typed them out.
"Close an app" is not missing because it is hard.
It is missing because nobody added a fourth branch.

That does not scale to the target.
Two changes are needed.

### 2A. A primitive kernel instead of a tool catalog

A small set of general, composable tools rather than many narrow ones.

- Run AppleScript
- Run a shell command
- HTTP fetch
- Read and write files

"Close Safari", "set display brightness", "empty the trash" then become the *same* tool with different arguments.
Capability grows without code changes.

### 2B. A second, slower brain for hard questions

Web search and open-ended questions do not belong on the latency-critical path.

Haiku 4.5 answers "what time is it" instantly.
A real research question should escalate to Opus 4.8 with server-side web search, run in the background, and be spoken when ready.
Friday says "let me look into that" and comes back, rather than every request being equally slow.

### Constraints to respect as this grows

**More tools makes Friday slower and less accurate.**
Every tool definition is tokens in the prompt on every single turn, and a small model picks wrong more often as the menu grows.
Past roughly 15 tools this needs grouping, progressive disclosure, or a router - not a longer flat list.

**Permission tiers are currently inert.**
`agent.py` calls every tool unconditionally.
The `read_only` / `low_risk_write` / `sensitive` tiers exist in the manifest and enforce nothing.
That is tolerable when the worst case is `set_volume`.
It is not tolerable the moment Friday can run shell commands.
Enforcement must land in the same change as the primitive kernel, not after it.

---

## 3. A real, responsive UI

Today the entire UI is one menu bar icon swapping between 8 SF Symbols (`MenuBarController.swift:41`).
There is no window.

**The data needed for the UI already exists and is being discarded.**

- `ToolResult.data` - every tool already returns a structured payload.
  `get_weather` computes the full forecast object.
  It travels to the cloud agent, is logged, and is thrown away.
  Nothing ever sends it back to the Mac.
  The weather card is blocked on plumbing, not on new capability.
- `user_input_transcribed` (`agent.py:256`) - your transcribed speech arrives at the agent and is used only to cancel a timer.
  The text is already there and unused.

### Target: a HUD

A floating panel that appears on wake and shows:

- State as *motion* rather than a static glyph - a live audio waveform while listening, a pulse while thinking.
- Your words appearing as you speak them, streamed from STT partials.
- Friday's reply as streaming text, in sync with the audio.
- Rich cards when a tool returns structured data - weather, calendar agenda, timers.
- Scrollback for the last few exchanges.

It should be a non-activating panel so it never steals focus from the app you are working in.
It should fade out when Friday returns to sleep.

---

## Why the UI matters more than it looks

**The UI is not cosmetic. It is the fix for goal 1 and the debugger for goal 2.**

A 2.5 second wait spent staring at a static moon icon feels broken.
The same 2.5 seconds, where your words appear as you speak them, the panel switches to a thinking pulse the instant you stop, and the text answer renders before the audio finishes, feels instant.
Perceived latency is at least half a UI problem.

There is also no visibility today into what Friday actually heard.
Every wake-word misfire, every misheard command, and every wrong tool selection is currently diagnosed by guessing.
The HUD makes goals 1 and 2 debuggable.

---

## Suggested order

1. **Custom verifier model.**
   Highest value per hour of work, and independent of everything else.
   Makes the current pretrained model usable today by rejecting phonetic neighbours.
2. **Endpointing timeout.**
   One config value, roughly 40% of the dead air on a simple turn.
3. **Unified audio capture (1A).**
   Move wake detection into Swift on the AEC-processed stream.
   Fixes speaker-triggered wakes and unblocks the pre-roll buffer, which lands in the same change.
4. **The HUD.**
   Transcript and state first, rich cards second.
   Requires plumbing `ToolResult.data` back to the Mac, which is the prerequisite for everything visual.
5. **Primitive kernel.**
   Replace `run_mac_action`'s three verbs with general primitives.
   Ship permission enforcement in the same change.
6. **Escalation path.**
   Background Opus 4.8 with web search for hard questions.

Custom wake word training for "hey friday" is independent of all of the above and slots in anywhere.
It is worth doing regardless, since the product currently answers to another assistant's name, but the verifier model in step 1 addresses the accuracy problem sooner and more cheaply.

---

## Open items carried over

- `call_tool` in `agent/src/agent.py:105` has no timeout.
  A hung tool freezes the entire turn.
- There are no tests anywhere in the project.
- The tool manifest is fetched once per session (`agent.py:186`).
  A tool added while connected stays invisible until reconnect.
- `FRIDAY_WAKE_THRESHOLD` is left at the default 0.5.
  Measured data in 1C shows neither failure mode is threshold-sensitive, so there is nothing to tune until the verifier model lands.
