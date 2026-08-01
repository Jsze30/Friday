# Friday - Vision

Friday should feel less like a voice-controlled tool menu and more like a trusted intelligence layer for Jason's life.

The north star is simple:

> Friday understands what is happening, knows what Jason is referring to, remembers what matters, and can act quickly across his digital life.

That experience requires more than integrations or a larger model.
It requires a personal context engine that connects current activity, recent events, people, projects, files, plans, and preferences.
The existing action, capability, and primitive layers then give that understanding the power to do something useful.

Five goals drive the project:

1. Friday should understand Jason's life and current context.
2. Activation and conversation should feel seamless and fast.
3. Friday should gain new integrations without new central routing logic.
4. Friday should act reliably through APIs and native controls before using UI automation.
5. Friday should have a visible, correctable interface that makes its understanding and actions clear.

This document describes the desired product and architectural direction.
The [README](README.md) documents the system that exists today, while [agent-plan.md](agent-plan.md) contains older implementation notes that may not reflect the current architecture.

---

## 1. Personal understanding is the product

Friday will not feel intelligent merely because it can call Spotify, Calendar, Messages, and a browser.
It will feel intelligent when those sources form one coherent understanding of Jason's life.

Friday should understand:

- Who Jason knows and how those people relate to his work and life.
- Which projects, goals, and open tasks currently matter.
- What happened recently across conversations, apps, files, and events.
- What Jason is doing on the Mac right now.
- What Jason means by phrases such as "this," "that file," "the project," "her," or "continue what I was doing."
- Which habits and preferences should shape how Friday responds and acts.
- Which facts are current, which are historical, and which are uncertain.

### The personal context engine

The personal context engine is the foundation beneath actions and capabilities.
It turns disconnected app data into useful context.

```text
Calendar, Reminders, Contacts, Notes, Email, Messages
Files, browser, VS Code, apps, location, music
                         |
                         v
               Personal context engine
                         |
          +--------------+---------------+
          |              |               |
          v              v               v
   Working context   Personal timeline   Knowledge graph
          |              |               |
          +--------------+---------------+
                         |
                         v
               Relevant context retrieval
                         |
                         v
             Actions, capabilities, primitives
```

The engine should normalize information into shared concepts instead of leaving it trapped inside provider-specific formats.
The important concepts include people, projects, tasks, events, files, messages, places, apps, topics, goals, and decisions.

Every stored fact or relationship should retain its source, timestamp, freshness, and confidence.
Friday should be able to distinguish a confirmed calendar event from an inference based on a recent conversation.

### Four kinds of memory

Friday needs four different forms of memory because they solve different problems.

| Memory | Purpose | Example |
| --- | --- | --- |
| Working context | Understand what is happening now. | VS Code is focused, the Friday project is open, and `action_catalog.py` is selected. |
| Episodic memory | Recall what happened and when. | Jason discussed the action architecture with Sarah yesterday afternoon. |
| Semantic memory | Understand stable entities and relationships. | Sarah works on design, and the Friday project lives in `Documents/Coding/friday`. |
| Preferences and routines | Adapt behavior to Jason. | Use Arc for links, Spotify for music, and concise spoken responses. |

Conversation history alone is not memory.
Important information must be extracted, connected, updated, and made independently retrievable.

### Working context

Working context should be a small, fast snapshot of the present moment.

It can include:

- Current app, window, and selected content.
- Current browser page and recent tabs.
- Current VS Code workspace, file, selection, and terminal state.
- Current conversation topic and recently mentioned entities.
- Current music, location, time, upcoming event, and active task.
- Recently opened, downloaded, or edited files.

Working context enables requests such as:

- "Explain this."
- "Run it."
- "Send this to Sarah."
- "Open the documentation for this."
- "Continue what I was doing."

This snapshot should be available locally without a network or model round trip.

### Personal timeline

The timeline records meaningful events rather than every raw interaction.

```text
9:00 AM - Opened the Friday project in VS Code.
9:20 AM - Discussed the personal context architecture.
10:00 AM - Met with Sarah.
10:45 AM - Edited architecture-notes.md.
11:00 AM - Sarah sent a related message.
```

The timeline enables requests such as:

- "Open the file I worked on this morning."
- "What did Sarah say about Friday?"
- "Continue what we were doing yesterday."
- "Play that playlist again."

Friday should summarize and compact old events while preserving important decisions, commitments, and relationships.

### Personal knowledge graph

The knowledge graph connects entities that appear across different sources.

```text
Jason
|-- works on -> Friday
|-- knows -> Sarah
|-- prefers browser -> Arc
`-- uses music provider -> Spotify

Friday project
|-- stored at -> Documents/Coding/friday
|-- discussed with -> Sarah
|-- uses -> LiveKit
`-- related file -> architecture-notes.md
```

The graph should resolve duplicate references across Contacts, Calendar, Email, Messages, files, and conversation.
It should support corrections such as "when I say the project, I mean Friday" without requiring central prompt changes.

### Retrieval, not prompt dumping

Friday must not place Jason's entire life into every model prompt.
That would be slow, expensive, confusing, and unsafe.

For each request, Friday should retrieve only the context likely to matter.

For "when is my meeting with him," Friday might retrieve recently mentioned people and upcoming calendar events.
For "send her the file," Friday might retrieve the recently mentioned person, the current file selection, and the preferred messaging provider.
For "what should I focus on," Friday might retrieve today's calendar, overdue tasks, important messages, active projects, and recent commitments.

Retrieval should combine recency, relevance, relationship strength, source reliability, and current activity.
If a reference remains ambiguous, Friday should ask one short question instead of confidently choosing the wrong entity.

### Learning from corrections

Corrections are high-value memory events.

When Jason says "no, I meant the Friday project," Friday should update the relevant alias or relationship.
When Jason says "use Messages when I ask you to text someone," Friday should update a preference.
When Jason says "do not remember that," Friday should remove the memory and its derived relationships where possible.

Friday should make learned memories visible and editable rather than hiding them inside a model prompt.

---

## 2. Understanding must lead to action

Friday currently has the correct three-layer foundation for execution.

### Actions

Actions are fast, deterministic operations whose execution path is already known.

Examples include `music.pause`, `system.open_app`, and a future `calendar.create_event`.
Integrations declare actions, parameters, matching routes, permissions, latency, and reliability.
The central router compiles those declarations and does not contain provider-specific command branches.

A clear action should skip the first model call, execute through the fastest available provider, and return a concise result.

### Capabilities

Capabilities handle goals that require interpretation, discovery, or several steps.

Examples include preparing for a meeting, organizing the day, researching a topic, or finding an appropriate playlist for the current task.
A capability can discover and combine actions from any installed integration.
Adding Calendar should make scheduling, meeting preparation, and daily planning more powerful without rebuilding those capabilities around Calendar-specific code.

### Primitives

Primitives are the small, stable powers underneath the system.

Examples include reading a file, opening an app, opening a URL, making a web request, running a process, or inspecting an unsupported UI.
New primitives should be added only when Friday gains a genuinely new kind of access.
Most growth should come from integration actions and reusable capabilities rather than a constantly expanding primitive set.

### Preferred execution order

Friday should use the strongest and least fragile available path.

```text
Ambient context
      |
      v
Deterministic action through a native API
      |
      v
Capability using available actions
      |
      v
Command-line or specialist-agent control
      |
      v
Generic primitive
      |
      v
UI inspection and interaction as the final fallback
```

UI automation is valuable for reach, but it should not be the normal path for apps with reliable APIs, commands, extensions, or agent protocols.

---

## 3. Integrations should scale by declaration

A new integration should require one adapter for authentication and provider-specific API behavior.
It should not require a new central router, a new voice-command system, or one tool per spoken phrase.

Each integration should declare:

- Its available actions and typed parameters.
- Voice routes and examples for deterministic matching.
- Its broader capabilities and searchable information.
- Authentication and availability requirements.
- Permissions, expected latency, and reliability.
- The events and entities it can contribute to the personal context engine.

A Calendar adapter might contribute events, attendees, and locations to context while declaring actions such as `calendar.list_events`, `calendar.create_event`, and `calendar.move_event`.
A Slack adapter might contribute people, channels, messages, projects, and decisions while declaring actions such as `messages.search` and `messages.send`.

The tenth integration should follow the same contract as the second.

---

## 4. Speed is an architectural requirement

Friday cannot feel present if every interaction waits for a large model and several network round trips.

The latency-critical path should stay local and deterministic whenever possible.

Speed has two parts:

1. Actual latency is the time before Friday hears, decides, acts, and speaks.
2. Perceived latency is whether Friday immediately shows that it heard Jason and is making progress.

Both parts are product requirements.

### Current low-latency foundation

The current architecture already includes several important pieces:

- The LiveKit session stays connected instead of starting after every wake.
- The microphone capture stays warm while sleeping and sends silence until activation.
- Wake detection uses the same echo-cancelled capture path as the conversation.
- A rolling pre-roll preserves speech spoken across the wake phrase.
- Speech endpointing uses a shorter timeout than the original implementation.
- Preemptive model and speech generation can begin before the turn is fully committed.
- Clear registered actions are selected deterministically without a model call.
- Longer capabilities can run outside the latency-critical conversational path.

These changes create a strong foundation, but they do not prove that the experience is fast enough.
Friday needs end-to-end measurements from the final word of a request to the first visible response, first action, and first spoken audio.

Target budgets:

| Operation | Target |
| --- | --- |
| Read working context | Under 50ms |
| Retrieve relevant memory | Under 200ms |
| Select a registered action | Under 10ms and no model call |
| Native Mac action | About 50-300ms |
| External API action | About 300ms-2s |
| Acknowledge a longer capability | Under 500ms |
| First spoken reply after a simple question | Under 1s after end of speech |

### Required interaction behavior

- Jason can say "Friday, what is the weather?" continuously without pausing after the wake phrase.
- Friday shows the live transcript as soon as speech is recognized.
- The HUD changes to thinking or acting as soon as Jason finishes speaking.
- Simple local questions and registered actions avoid unnecessary model and network round trips.
- Long work receives an immediate acknowledgement and visible progress instead of blocking the conversation.
- Friday can be interrupted naturally while speaking.
- The first turn does not pay a perceptible cold-start cost.
- Audio played through the Mac's speakers does not wake Friday.
- Failed providers and tools have deadlines, fallbacks, and visible error states instead of hanging the turn.

### Latency measurement

Every turn should record a local timing trace for:

- Wake detection.
- First and final transcription.
- End-of-turn detection.
- Route or action selection.
- Context retrieval.
- Provider and tool execution.
- First model token.
- First text shown in the HUD.
- First synthesized audio.
- First audio heard by Jason.

The timing trace should identify regressions by route and provider without storing raw microphone audio.
Latency targets should be enforced with repeatable tests and observed percentiles rather than occasional manual impressions.

Long research, coding, or planning work should run as a background capability with immediate spoken and visual progress.
The fast conversational model should not be forced to carry every difficult task, and the strongest model should not sit on every simple path.

Continuous speech across the wake phrase, pre-roll audio, warm sessions, reliable endpointing, and barge-in remain required parts of the experience.

---

## 5. Privacy and user control are part of intelligence

Understanding Jason's life requires access to sensitive information.
The context system should therefore be local-first, inspectable, and reversible.

The desired rules are:

- Store durable personal context locally by default.
- Keep raw credentials in the operating system keychain.
- Request the narrowest practical app scopes.
- Preserve provenance so Jason can see why Friday believes something.
- Support viewing, correcting, exporting, and deleting memories.
- Use retention rules so low-value activity does not accumulate forever.
- Avoid storing raw microphone audio as personal memory.
- Send only the context needed for the current request to cloud models.
- Keep the cloud agent behind the existing LiveKit RPC boundary rather than exposing the Mac directly.

Friday should be powerful without becoming an invisible surveillance system.

---

## 6. A visible and correctable interface

The UI is not cosmetic.
It is how Jason sees what Friday heard, what context it used, what it is doing, and what it learned.
It is also a core part of making Friday feel fast.

A short wait with no feedback feels broken.
The same wait feels responsive when Jason can see his words appear, the state change immediately, and the answer begin streaming before speech playback finishes.

The current menu bar state icon is only the foundation.
The product requires a real visual surface.

The target is a non-activating HUD that appears on wake without stealing focus.

It should show:

- Live transcription while Jason speaks.
- Friday's streaming response in sync with speech.
- Clear listening, thinking, acting, and speaking states.
- Rich cards for weather, calendar, tasks, messages, files, research, and music.
- Progress for long-running capabilities.
- The person, project, file, or event Friday resolved from an ambiguous reference.
- A lightweight way to correct a reference or remove a learned memory.
- Recent conversation and action history.

The HUD should fade when Friday sleeps while keeping deeper history and memory controls available from the menu bar app.

### HUD behavior

The HUD should follow the conversation lifecycle:

```text
sleeping
   |
   v
wake detected -> listening -> thinking -> acting -> speaking
                    ^                         |
                    +------ interruption ----+
```

- On wake, the HUD appears immediately near the active workspace without activating itself.
- While listening, it shows a live waveform and partial transcription.
- While thinking, it preserves the final transcript and shows responsive motion rather than a static spinner.
- While acting, it names the action or capability and shows useful progress.
- While speaking, it streams Friday's response in sync with the audio.
- On interruption, it stops the response cleanly and returns to listening.
- On completion, it remains briefly for review and then fades without stealing keyboard focus.

### HUD delivery stages

The first HUD version should include:

- A non-activating floating panel.
- Listening, thinking, acting, speaking, follow-up, and error states.
- Partial and final user transcripts.
- Streaming assistant text.
- Smooth appearance, state transitions, resizing, and dismissal.
- A compact history of the current exchange.

The second version should add:

- A shared card protocol for structured action and capability results.
- Weather, calendar, task, file, research, and Spotify cards.
- Progress and cancellation for long-running capabilities.
- Resolved-reference indicators such as the selected person, project, event, or file.

The context-aware version should add:

- Provenance showing why Friday used a memory or relationship.
- One-step correction of an incorrectly resolved reference.
- Memory review, editing, retention, and deletion controls.
- A deeper searchable history available from the menu bar app.

The agent should send structured UI events to the Mac instead of making the Swift app infer meaning from spoken text.
Those events should cover transcripts, response text, state, action metadata, progress, structured results, resolved context, errors, and completion.

---

## 7. Proactive intelligence comes after reliable context

Friday should eventually notice useful situations without waiting for an exact command.

Examples include:

- Preparing relevant files and recent conversations before a meeting.
- Warning that travel time conflicts with the next calendar event.
- Surfacing a forgotten commitment from a message.
- Suggesting the next task when a focus block begins.
- Noticing that a repeated manual workflow could become a routine.

Proactivity should be based on strong context, confidence, and expected value.
Friday should prefer quiet preparation and optional suggestions over frequent interruptions.
It should be easy to mute a class of suggestions or explain why one appeared.

---

## 8. Example experiences

### "Send her the latest Friday architecture"

Friday resolves the recently mentioned person, identifies the Friday project, finds the most recent relevant architecture document, chooses Jason's preferred communication channel, sends it, and records the event in the timeline.

### "Continue what I was doing yesterday"

Friday retrieves yesterday's recent work sessions, identifies the unfinished Friday task, opens the project and relevant files, and briefly explains where Jason left off.

### "Prepare me for my next meeting"

Friday finds the next calendar event, resolves its attendees, retrieves related email, messages, notes, files, prior decisions, and open commitments, then produces a short briefing with links and suggested questions.

### "What should I focus on?"

Friday combines the current time and location with today's calendar, overdue tasks, active projects, important messages, deadlines, and recent commitments, then recommends one next action and explains why.

### "Move it to Friday"

Friday understands that "it" refers to the event discussed in the current conversation, verifies that Friday means the upcoming date rather than the project, and uses the Calendar action to reschedule it.

---

## 9. Roadmap

### Phase 0: Instant interaction and HUD foundation

- Add end-to-end timing events and local latency traces for every voice turn.
- Measure wake-to-listening, end-of-speech-to-first-visual, end-of-speech-to-action, and end-of-speech-to-first-audio latency.
- Remove unnecessary waits and round trips discovered by those traces.
- Build the non-activating HUD with animated conversation states.
- Stream partial transcripts and assistant text from the agent to the Mac.
- Preserve continuous speech, pre-roll, warm audio, echo cancellation, barge-in, and fast deterministic routing.
- Add timeouts and visible failure states for every latency-critical boundary.

This phase should begin before the personal context work because the HUD makes every later capability easier to understand, test, and debug.

### Phase 1: Personal context foundation

- Define shared entity, event, relationship, memory, and provenance schemas.
- Add a local durable store with migrations, retention, and deletion.
- Build the working-context snapshot and relevance-based retrieval API.
- Add reference resolution for recently mentioned people, projects, files, events, and apps.
- Feed action and capability results back into the timeline.

### Phase 2: High-value life integrations

- Add Calendar, Reminders, Contacts, and Notes adapters.
- Publish their actions through the existing action catalog.
- Normalize their people, tasks, events, notes, locations, and relationships into the context engine.

### Phase 3: Computer awareness

- Add current Mac app, window, selection, and recent-file context.
- Add Arc page and tab context through the strongest available browser interface.
- Add VS Code workspace, file, selection, diagnostics, terminal, and task context through an extension or agent protocol.

### Phase 4: Timeline and knowledge graph

- Connect entities across apps and conversations.
- Add aliases, confidence, source tracking, corrections, and conflict handling.
- Add compaction that preserves important decisions, commitments, and routines.

### Phase 5: Communication context

- Add Email and Messages providers.
- Add search, read, send, reply, and archive actions where supported.
- Extract commitments, decisions, people, projects, and follow-ups into context.

### Phase 6: Context-aware capabilities

- Daily briefing.
- Meeting preparation.
- Focus and priority recommendation.
- Cross-app search and recall.
- Continue previous work.
- Personal routines that adapt to current context.

### Phase 7: Proactivity and mature context UI

- Surface quiet, high-confidence suggestions.
- Show resolved context, memory provenance, rich results, and capability progress.
- Add memory review, correction, retention, and deletion controls.

The basic HUD begins in Phase 0 and gains richer cards throughout the roadmap.
Only proactive behavior and the deepest memory controls wait until context quality is measurable and trustworthy.

---

## 10. Definition of done

Friday is approaching the vision when:

- Jason can speak continuously across the wake phrase without losing words.
- A simple response begins speaking within one second of the end of speech under normal conditions.
- The HUD visibly responds within 200ms of each known state transition.
- Every turn has a timing trace that makes latency regressions diagnosable.
- The HUD shows what Friday heard, what it is doing, what it answered, and what structured result it produced.
- Jason can speak naturally without translating requests into tool names.
- Friday correctly resolves common references from current and recent context.
- Friday can recall important events, decisions, files, people, and commitments across sessions.
- Corrections improve future behavior and remain visible and editable.
- New integrations enrich existing capabilities without central router changes.
- Clear actions remain fast and deterministic as the integration count grows.
- UI automation is a fallback rather than the foundation.
- Friday explains uncertainty instead of inventing memories or relationships.
- Personal context remains local-first and controllable.
- Friday provides useful help before being explicitly instructed, without becoming distracting.

---

## Non-goals

- Storing every raw interaction forever.
- Recording passive microphone audio as memory.
- Dumping the full personal database into every model prompt.
- Adding one custom model-facing tool or routing branch per app command.
- Treating fragile UI automation as the primary integration strategy.
- Letting the model silently create durable facts without provenance or confidence.
- Becoming proactive before context quality and user controls are reliable.

The central idea is:

```text
Integrations give Friday information.
Memory connects the information.
Context determines what matters now.
Reasoning decides what should happen.
Actions make it happen quickly.
The UI makes the entire process visible and correctable.
```
