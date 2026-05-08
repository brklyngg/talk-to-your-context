# Architecture

A live, interruptible voice line to your own agent. Three pieces talk:

- **Browser PWA** (`web/`, vanilla JS) — UI + WebRTC peer.
- **OpenAI Realtime API** — voice (gpt-realtime) + native asynchronous tool calling. The browser holds a direct WebRTC peer with OpenAI; audio never transits the sidecar.
- **Sidecar** (`server.py`, aiohttp) — mints OpenAI ephemeral sessions, brokers the `ask_agent` tool, owns conversation state.
- **Your agent backend** — anything exposing an OpenAI-compatible `/v1/chat/completions` SSE endpoint. Receives a per-conversation `X-Session-Id: voice-{conv_id}` header so it can track its own memory across resumes.
- **Optional messaging adapter** — Slack today; see [ADAPTERS.md](ADAPTERS.md).

## The durable conversation

The system of record is `CONVERSATIONS[conv_id]` on the sidecar. WebRTC peers and OpenAI Realtime sessions are **disposable** — they get rebuilt across iOS PWA backgrounding and 5G ↔ wifi handoff. Two values flow through every flow:

- **`conv_id`** (durable, sidecar-owned) — survives backgrounding, network drops, and one user-initiated session resume after another.
- **`task_id`** (per agent call) — server-side `asyncio.Task` keyed reference. The agent call survives client disconnect; on resume the answer is replayed.

Backgrounding is a **pause**, not an end. Only the explicit End button or `pagehide` close the conversation. A 20-minute idle reaper guards against forgotten calls.

## Lifecycle

```
┌── browser PWA ──┐                 ┌── sidecar (server.py) ─────────┐
│ WebRTC peer    ─┼── ephemeral ────│ /api/session   mint conv_id    │
│ (disposable)   │     key per      │ /api/ask-agent spawn task      │
│                │     session      │ /api/agent-task long-poll      │
│                │                  │ /api/text-turn (typing path)   │
│                │                  │ /api/resume    fresh peer key  │
│                │                  │ /api/end       teardown        │
└─────────┬──────┘                  │                                │
          │ conv_id (durable)       │ CONVERSATIONS[conv_id] = {     │
          │                         │   started_at, last_activity_ts,│
          │                         │   entries[], agent_tasks{}     │
          │                         │ }                              │
          │                         │ logs/calls/<conv>.ndjson       │
          │                         │ 20-min idle reaper             │
          ▼                         └────────────────────────────────┘
   visibilitychange→hidden : do nothing (just mark timeline)
   visibilitychange→visible: if peer dead → POST /api/resume → mint new key
   pc.connectionState=failed for >3s : same resume flow
   pagehide / End button   : POST /api/end (true close)
```

## Flow: substantive turn (`ask_agent`)

1. User speaks. Realtime model decides to call `ask_agent` (per system prompt).
2. Browser POSTs `/api/ask-agent` with `{conv_id, question, call_id}`.
3. Sidecar spawns an `asyncio.Task` → `task_id`. Fast-path: if it completes within 1.5 s, the answer is returned inline. Otherwise the response is `{task_id, status: "running"}`.
4. Browser long-polls `/api/agent-task/<task_id>?conv_id=<conv_id>` (≤25 s per call, retries until done).
5. When the task finishes, browser feeds the answer to the realtime peer via `function_call_output`. The realtime model speaks it.
6. Server records both turns into `conv["entries"]` via the local conv reference — survives concurrent `/api/end` pop.

## Flow: backgrounding (the killer feature)

1. User backgrounds the iPhone mid-`ask_agent`. The WebRTC peer dies within ~5 s (iOS PWA mic stop). The `asyncio.Task` keeps running on the sidecar.
2. `visibilitychange→hidden` writes a `paused` marker into `clientEntries`. Server is NOT notified.
3. Task completes. Answer cached on `conv["agent_tasks"][task_id]`. NDJSON `ask_agent_done` event written. `last_activity_ts` touched.
4. User returns. `visibilitychange→visible` triggers `/api/resume`.
5. Server mints a fresh OpenAI Realtime session, returns `{session, completed_while_away[], still_working[]}`.
6. Browser rebuilds the WebRTC peer (`attachPeer`). On data-channel open, it injects `completed_while_away` items as user/assistant message pairs (NOT `function_call_output` — the prior `call_id` is dead). Then a one-line system primer asks the model to greet and walk through.
7. For any `still_working` task, the browser re-attaches the long-poll. When it completes, it's injected as a synthetic assistant message.

## Flow: 5G ↔ wifi handover

Same as backgrounding, triggered by `pc.onconnectionstatechange === "failed"|"disconnected"` debounced 3 s. We don't attempt ICE restart — full reconnect is ~1-2 s end-to-end and one recovery path covers both backgrounding and network handover. A 5-second-cadence inbound-bytes watchdog catches "silent freezes" where state still says connected but audio has stopped.

## Diagnostic log

Per-call NDJSON at `logs/calls/<conv_id>.ndjson`. One line per event. `cat | jq` queryable.

Events:

- `session_minted`, `resumed`, `paused` (client-side via transcript)
- `ask_agent_spawned`, `ask_agent_done`, `ask_agent_cancelled`, `ask_agent_delivered_to_realtime`, `ask_agent_carried_over_on_resume`
- `text_turn`
- `tool_error`, `late_turn`, `legacy_background_end_ignored`
- `reaped`, `ended`

For cross-call queries, run `python events.py` to derive an index on demand.

## Reaper

Polls every 60 s. Conversations with no activity for 20 min are reaped: in-flight tasks cancelled, transcript written, Slack thread posted with `reason=idle_timeout`, NDJSON `reaped` event. The dominant cost (mic-active minutes on `gpt-realtime`) stops within ~5 s of backgrounding anyway; the 20-min cap protects against truly-forgotten calls.

## End / archive

`/api/end` (idempotent) cancels in-flight tasks, writes the transcript to `TRANSCRIPT_DIR`, fires-and-forgets `ingest_into_agent` so the backend can absorb the call into its memory layer, posts a Slack thread.

## Tradeoffs

- **No ICE restart.** Full reconnect (mint a fresh ephemeral, build a new peer) is ~1-2 s and works for both backgrounding and network handover. ICE restart is an optimization that requires SDP renegotiation surface OpenAI hasn't documented — not worth the complexity.
- **Realtime model doesn't sit silent during `ask_agent`.** `gpt-realtime` GA (May 2026) handles asynchronous function calls natively — the model can converse during the wait. The system prompt's rule #2 lets it say one short bridging line ("looking now") without inventing the answer.
- **Realtime + separate brain rather than monolithic.** Voice latency and persona stay tight on `gpt-realtime`; substantive cognition stays with whichever model the agent backend uses (often a frontier model with full tool access). The two are bridged by the `ask_agent` proxy. OpenAI's MCP-as-tool support is a future direction that could eliminate the proxy hop.
- **Inline fast-path on `/api/ask-agent`.** Chitchat-grade calls (<1.5 s) skip the long-poll round-trip. Anything slower spawns a server-side task that survives client disconnect.
- **No mid-call Slack updates.** Slack thread is a single post at end-of-call (or reap). Per-turn pings are noisy and the user can read the transcript locally.
